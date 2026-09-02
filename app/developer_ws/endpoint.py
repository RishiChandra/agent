"""FastAPI WebSocket endpoint at /ws/developer/{user_id}.

Owns the lifecycle of one voice session: accept → wire up AudioIO + UtteranceBuffer +
Scratchpad + Bridge + Pipeline → register with the in-process registry (so HTTP pings
can reach this session) → loop on incoming JSON frames → drain on close.

The Pipeline is now a Pipecat pipeline (see `pipeline.py`); this file pushes frames
into it via the `SpeechPipeline.feed_audio` / `signal_user_stopped` / `interrupt`
shims rather than calling STT/LLM/TTS directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from audio_codec import UPLINK_SAMPLE_RATE

from . import registry
from .audio_io import AudioIO
from .bridge import RemoteAudioBridge
from .pipeline import SpeechPipeline
from .scratchpad import Scratchpad
from .utterance import UtteranceBuffer
from .vad import SileroVAD, VADEvent, create_silero_vad

log = logging.getLogger("developer_ws")


async def developer_websocket_endpoint(websocket: WebSocket, user_id: str) -> None:
    """Entry point for one voice session.

    Called by: FastAPI router in `app/main.py` (`app.websocket("/ws/developer/{user_id}")`).
    Owns: AudioIO, UtteranceBuffer, Scratchpad, RemoteAudioBridge, SpeechPipeline.
    Registers the pipeline with `registry` so HTTP pings can reach this session.
    """
    await websocket.accept()
    log.info("connected user_id=%s", user_id)

    audio = AudioIO(websocket)
    utterance = UtteranceBuffer()
    # Primary endpointing: Silero VAD closes the turn ~DEVELOPER_WS_SILERO_STOP_SECS
    # after speech ends. None (disabled / onnxruntime missing) → timer-only.
    vad = create_silero_vad(user_id=user_id)
    log.info("endpointing user_id=%s mode=%s", user_id, "silero+timer" if vad else "timer")
    scratchpad = Scratchpad(user_id=user_id)
    bridge = RemoteAudioBridge(audio, user_id=user_id)
    pipeline = SpeechPipeline(websocket, user_id, utterance, audio, scratchpad, bridge)
    await pipeline.start()
    registry.register(user_id, pipeline)
    # Audible "you're connected to the orchestrator" cue as the session opens.
    await pipeline.play_connect_ding()

    try:
        await _receive_loop(websocket, user_id, audio, utterance, vad, pipeline, bridge)
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
    vad: SileroVAD | None,
    pipeline: SpeechPipeline,
    bridge: RemoteAudioBridge,
) -> None:
    """Read incoming JSON frames forever; dispatch by frame type.

    Audio frames flow through `pipeline.feed_audio(pcm)`; turn-complete /
    VAD-stop / silence-timer signals call `pipeline.signal_user_stopped()`;
    interrupts call `pipeline.interrupt()`.
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
            await _handle_interrupt(audio, utterance, vad, bridge, pipeline)
            continue

        if "audio" in data:
            await _handle_audio(data, user_id, audio, utterance, vad, pipeline, bridge)
            continue

        if data.get("turn_complete") is True and not bridge.active:
            if vad is not None:
                vad.reset()
            await utterance.bump_arm_id()
            await pipeline.signal_user_stopped()


def _is_interrupt(data: dict) -> bool:
    if data.get("interrupt"):
        return True
    text = data.get("text")
    return bool(text and "stop" in str(text).lower())


async def _handle_interrupt(
    audio: AudioIO,
    utterance: UtteranceBuffer,
    vad: SileroVAD | None,
    bridge: RemoteAudioBridge,
    pipeline: SpeechPipeline,
) -> None:
    if vad is not None:
        vad.reset()
    await utterance.bump_arm_id()
    await pipeline.interrupt()
    await audio.interrupt()


async def _handle_audio(
    data: dict,
    user_id: str,
    audio: AudioIO,
    utterance: UtteranceBuffer,
    vad: SileroVAD | None,
    pipeline: SpeechPipeline,
    bridge: RemoteAudioBridge,
) -> None:
    """Process one audio frame from the client.

    Bridge-active path: forward raw uplink to the remote, skip the pipeline.
    Normal path: push the audio into the Pipecat pipeline, then run Silero VAD
    (primary end-of-turn signal) and arm the silence timer (fallback; both call
    `pipeline.signal_user_stopped`, whichever fires first wins).
    """
    pcm = _decode_audio_payload(data, user_id, audio)

    if bridge.active:
        utterance.reset_barge_in()
        if vad is not None:
            vad.reset()
        if pcm:
            await bridge.send_uplink_pcm(pcm)
        return

    if pcm:
        # Barge-in: the user talking over the bot interrupts it. Runs before
        # feed so the cancel lands ahead of this batch; the pipeline then
        # treats the continuing speech as a fresh utterance.
        if audio.is_bot_audible():
            if utterance.barge_in_hit(pcm):
                log.info("barge-in user_id=%s — interrupting bot mid-speech", user_id)
                await pipeline.interrupt()
                await audio.interrupt()
        else:
            utterance.reset_barge_in()
        await pipeline.feed_audio(pcm)

    if data.get("turn_complete") is True:
        if vad is not None:
            vad.reset()
        await utterance.bump_arm_id()
        await pipeline.signal_user_stopped()
        return

    if pcm:
        if vad is not None:
            event = await vad.process(pcm)
            if event is VADEvent.STOPPED:
                # Confirmed end of speech — close the turn now instead of
                # waiting out the silence timer. Bump first so the fallback
                # timer can't double-fire on the same utterance.
                log.info("vad STOPPED user_id=%s — signaling user stopped", user_id)
                await utterance.bump_arm_id()
                await pipeline.signal_user_stopped()
                return
            if event is VADEvent.STARTED:
                log.info("vad STARTED user_id=%s", user_id)
        from audio_codec import rms_int16_le
        _rms = rms_int16_le(pcm)
        _has = utterance.has_speech(pcm)
        log.info(
            "audio batch user_id=%s bytes=%d rms=%d has_speech=%s",
            user_id, len(pcm), _rms, _has,
        )
        if _has:
            await utterance.arm_timer(pipeline.signal_user_stopped)


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

    Cancels any pending silence timer, lets the pipeline flush an in-flight
    utterance via its own `drain()`, then closes the bridge + pipeline.
    """
    await utterance.bump_arm_id()
    try:
        await pipeline.drain()
    except Exception:
        log.exception("pipeline drain failed user_id=%s", user_id)
    try:
        await pipeline.close()
    except Exception:
        log.exception("pipeline close failed user_id=%s", user_id)
    await audio.shutdown_playback()
