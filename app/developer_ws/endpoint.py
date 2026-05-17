"""FastAPI WebSocket endpoint at /ws/developer/{user_id}.

Owns the lifecycle of one voice session: accept → wire up AudioIO + UtteranceBuffer +
Scratchpad + Bridge + Pipeline → register with the in-process registry (so HTTP pings
can reach this session) → loop on incoming JSON frames → drain on close. Hands raw
audio frames to the utterance buffer and trigger points (turn_complete, silence timer)
to the pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import traceback

from fastapi import WebSocket, WebSocketDisconnect

from audio_codec import UPLINK_SAMPLE_RATE

from . import registry
from .audio_io import AudioIO
from .bridge import RemoteAudioBridge
from .pipeline import SpeechPipeline
from .queries import fetch_user_agents
from .scratchpad import Scratchpad
from .utterance import UtteranceBuffer

log = logging.getLogger("developer_ws")


def _format_agent_context(agents: list[dict]) -> str:
    """Render queried agent rows as a system-prompt block for Gemini."""
    if not agents:
        return "Registered agents for this user: (none)."
    lines = ["Registered agents for this user (from Agent_Registry → Agents):"]
    for a in agents:
        parts = [f"{k}={v!r}" for k, v in a.items() if v is not None]
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


async def _load_agents(user_id: str) -> list[dict]:
    """Query Agent_Registry + Agents on the worker thread (psycopg2 is blocking).

    Returns the raw agent rows; the endpoint also formats them into a system-prompt
    block for Gemini. On failure, returns [] so the session still comes up (the model
    just won't have any agents to route to).
    """
    try:
        agents = await asyncio.to_thread(fetch_user_agents, user_id)
    except Exception as e:
        log.warning("agent context load failed user_id=%s: %s", user_id, e)
        return []
    log.info("loaded %d agent(s) for user_id=%s", len(agents), user_id)
    return agents


async def developer_websocket_endpoint(websocket: WebSocket, user_id: str) -> None:
    """Entry point for one voice session.

    Called by: FastAPI router in `app/main.py` (`app.websocket("/ws/developer/{user_id}")`).
    Owns: AudioIO, UtteranceBuffer, Scratchpad, RemoteAudioBridge, SpeechPipeline.
    Registers the pipeline with `registry` so HTTP pings can reach this session.
    """
    await websocket.accept()
    log.info("connected user_id=%s", user_id)

    agents = await _load_agents(user_id)
    agent_context = _format_agent_context(agents)

    audio = AudioIO(websocket)
    utterance = UtteranceBuffer()
    scratchpad = Scratchpad(user_id=user_id)
    bridge = RemoteAudioBridge(audio, user_id=user_id)
    pipeline = SpeechPipeline(
        websocket,
        user_id,
        utterance,
        audio,
        scratchpad,
        bridge,
        agent_context,
        agents,
    )
    registry.register(user_id, pipeline)

    try:
        await _receive_loop(websocket, user_id, audio, utterance, pipeline, bridge)
    except WebSocketDisconnect:
        log.info("disconnected user_id=%s", user_id)
    except Exception as e:
        log.exception("error user_id=%s: %s", user_id, e)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        registry.unregister(user_id)
        await _drain_on_close(user_id, utterance, pipeline, audio, bridge)
        scratchpad.dump()


async def _receive_loop(
    websocket: WebSocket,
    user_id: str,
    audio: AudioIO,
    utterance: UtteranceBuffer,
    pipeline: SpeechPipeline,
    bridge: RemoteAudioBridge,
) -> None:
    """Read incoming JSON frames forever; dispatch by frame type.

    Called by: `developer_websocket_endpoint` only.
    Dispatches to: `_handle_interrupt`, `_handle_audio`, `pipeline.schedule_flush`.
    Terminates on WebSocketDisconnect (bubbles up to the endpoint's try/finally).
    """
    while True:
        try:
            msg = await websocket.receive_text()
        except RuntimeError as e:
            err = str(e).lower()
            if "accept" in err or "not connected" in err:
                log.info("receive ended (socket closed) user_id=%s", user_id)
                return
            raise
        data = json.loads(msg)

        if _is_interrupt(data):
            await _handle_interrupt(audio, utterance, bridge)
            continue

        if "audio" in data:
            await _handle_audio(data, user_id, audio, utterance, pipeline, bridge)
            continue

        if data.get("turn_complete") is True and not bridge.active:
            await utterance.bump_arm_id()
            pipeline.schedule_flush()


def _is_interrupt(data: dict) -> bool:
    if data.get("interrupt"):
        return True
    text = data.get("text")
    return bool(text and "stop" in str(text).lower())


async def _handle_interrupt(
    audio: AudioIO, utterance: UtteranceBuffer, bridge: RemoteAudioBridge
) -> None:
    # User interrupt tears down the bridge so they regain the local assistant.
    if bridge.active:
        await bridge.close()
    await utterance.snapshot_and_clear()
    await utterance.bump_arm_id()
    await audio.interrupt()


async def _handle_audio(
    data: dict,
    user_id: str,
    audio: AudioIO,
    utterance: UtteranceBuffer,
    pipeline: SpeechPipeline,
    bridge: RemoteAudioBridge,
) -> None:
    """Process one audio frame from the client.

    Called by: `_receive_loop` (one call per `{audio: ...}` frame).
    Two modes:
      - bridge.active → `bridge.send_uplink_pcm(pcm)`, skip local STT/LLM/TTS.
      - otherwise    → `utterance.extend(pcm)`, then either `pipeline.schedule_flush`
        (if `turn_complete:true`) or `utterance.arm_timer(pipeline.flush)` (if the
        batch's RMS clears the VAD threshold).
    """
    pcm = _decode_audio_payload(data, user_id, audio)

    # Bridge mode: skip local STT/LLM/TTS entirely; relay raw uplink to the remote.
    if bridge.active:
        if pcm:
            await bridge.send_uplink_pcm(pcm)
        return

    if pcm:
        await utterance.extend(pcm)

    if data.get("turn_complete") is True:
        await utterance.bump_arm_id()
        pipeline.schedule_flush()
        return

    # Only re-arm the silence timer for batches with speech energy. Silent
    # batches would otherwise keep the timer perpetually deferred.
    if pcm:
        from audio_codec import rms_int16_le
        _rms = rms_int16_le(pcm)
        _has = utterance.has_speech(pcm)
        log.info("audio batch user_id=%s bytes=%d rms=%d has_speech=%s", user_id, len(pcm), _rms, _has)
        if _has:
            await utterance.arm_timer(pipeline.flush)


def _decode_audio_payload(data: dict, user_id: str, audio: AudioIO) -> bytes | None:
    payload = base64.b64decode(data["audio"])
    if data.get("codec") != "opus":
        return payload
    sr = int(data.get("sr", UPLINK_SAMPLE_RATE))
    frame_ms = int(data.get("frame_ms", 20))
    frame_samples = sr * frame_ms // 1000
    try:
        return audio.decode_uplink_opus(payload, sample_rate=sr, frame_samples=frame_samples)
    except Exception as e:
        log.warning("uplink opus decode failed user_id=%s: %s", user_id, e)
        return None


async def _drain_on_close(
    user_id: str,
    utterance: UtteranceBuffer,
    pipeline: SpeechPipeline,
    audio: AudioIO,
    bridge: RemoteAudioBridge,
) -> None:
    """Best-effort cleanup after the socket closes.

    Called by: `developer_websocket_endpoint` finally block (always runs).
    Cancels any pending silence timer, closes the bridge (sends `bye`), flushes any
    leftover utterance audio through STT/LLM/TTS (45s hard timeout), and shuts down
    the downlink Opus encoder so the next session starts clean.
    """
    await utterance.bump_arm_id()
    await bridge.close()
    try:
        if await utterance.has_data():
            # Bound the final flush so a stuck STT/LLM/TTS can't hold the connection open.
            await asyncio.wait_for(pipeline.flush(), timeout=45.0)
    except asyncio.TimeoutError:
        log.warning("shutdown flush timed out user_id=%s", user_id)
    except Exception:
        traceback.print_exc()
    await audio.shutdown_playback()
