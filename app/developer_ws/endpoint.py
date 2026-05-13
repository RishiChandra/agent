"""FastAPI endpoint for /ws/developer/{user_id}: receive frames, dispatch to pipeline."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import traceback

from fastapi import WebSocket, WebSocketDisconnect

from audio_codec import UPLINK_SAMPLE_RATE

from .audio_io import AudioIO
from .pipeline import SpeechPipeline
from .scratchpad import Scratchpad
from .utterance import UtteranceBuffer

log = logging.getLogger("developer_ws")


async def developer_websocket_endpoint(websocket: WebSocket, user_id: str) -> None:
    await websocket.accept()
    log.info("connected user_id=%s", user_id)

    audio = AudioIO(websocket)
    utterance = UtteranceBuffer()
    scratchpad = Scratchpad(user_id=user_id)
    pipeline = SpeechPipeline(websocket, user_id, utterance, audio, scratchpad)

    try:
        await _receive_loop(websocket, user_id, audio, utterance, pipeline)
    except WebSocketDisconnect:
        log.info("disconnected user_id=%s", user_id)
    except Exception as e:
        log.exception("error user_id=%s: %s", user_id, e)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        await _drain_on_close(user_id, utterance, pipeline, audio)
        scratchpad.dump()


async def _receive_loop(
    websocket: WebSocket,
    user_id: str,
    audio: AudioIO,
    utterance: UtteranceBuffer,
    pipeline: SpeechPipeline,
) -> None:
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
            await _handle_interrupt(audio, utterance)
            continue

        if "audio" in data:
            await _handle_audio(data, user_id, audio, utterance, pipeline)
            continue

        if data.get("turn_complete") is True:
            await utterance.bump_arm_id()
            pipeline.schedule_flush()


def _is_interrupt(data: dict) -> bool:
    if data.get("interrupt"):
        return True
    text = data.get("text")
    return bool(text and "stop" in str(text).lower())


async def _handle_interrupt(audio: AudioIO, utterance: UtteranceBuffer) -> None:
    await utterance.snapshot_and_clear()
    await utterance.bump_arm_id()
    await audio.interrupt()


async def _handle_audio(
    data: dict,
    user_id: str,
    audio: AudioIO,
    utterance: UtteranceBuffer,
    pipeline: SpeechPipeline,
) -> None:
    pcm = _decode_audio_payload(data, user_id, audio)
    if pcm:
        await utterance.extend(pcm)

    if data.get("turn_complete") is True:
        await utterance.bump_arm_id()
        pipeline.schedule_flush()
        return

    # Only re-arm the silence timer for batches with speech energy. Silent
    # batches would otherwise keep the timer perpetually deferred.
    if pcm and utterance.has_speech(pcm):
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
) -> None:
    await utterance.bump_arm_id()
    try:
        if await utterance.has_data():
            # Bound the final flush so a stuck STT/LLM/TTS can't hold the connection open.
            await asyncio.wait_for(pipeline.flush(), timeout=45.0)
    except asyncio.TimeoutError:
        log.warning("shutdown flush timed out user_id=%s", user_id)
    except Exception:
        traceback.print_exc()
    await audio.shutdown_playback()
