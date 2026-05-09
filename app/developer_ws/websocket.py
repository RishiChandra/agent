"""Developer WebSocket: uplink speech → Vosk STT → Gemini text → pyttsx3 TTS (no Live API)."""

import asyncio
import base64
import json
import os
import traceback

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from audio_codec import UPLINK_SAMPLE_RATE, rms_int16_le

from .gemini_text import gemini_reply
from .speech_client import synthesize_speech_pcm24, transcribe_pcm16
from .tts_audio_manager import DeveloperSpeechAudioManager


async def developer_websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    print(f"[developer_ws] connected user_id={user_id}")

    audio = DeveloperSpeechAudioManager(websocket)
    speech_buffer = bytearray()
    # Short critical section: extend / snapshot+clear only (never hold across STT/Gemini/TTS).
    buffer_lock = asyncio.Lock()
    # At most one STT→Gemini→TTS pipeline at a time (watcher flush is create_task, not awaited).
    pipeline_lock = asyncio.Lock()
    # Monotonic id: each new "arm silence" bumps it; a wake-up only flushes if its id is still current.
    pending_arm_id = 0
    silence_task: asyncio.Task | None = None
    # Default > mic batch period (~1.54s for 48×512 @ 16kHz) so speech coalesces across batches.
    end_silence_s = float(os.environ.get("DEVELOPER_WS_END_SILENCE_SEC", "2.0"))

    async def cancel_silence_task() -> None:
        nonlocal silence_task
        t = silence_task
        silence_task = None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    def schedule_flush() -> None:
        """Run STT→Gemini→TTS without tying the silence watcher to the pipeline (cancellable sleep only)."""

        async def _flush_runner() -> None:
            try:
                await run_flush_serial()
            except Exception as e:
                print(f"[developer_ws] flush task failed user_id={user_id}: {e}")
                traceback.print_exc()

        asyncio.create_task(_flush_runner())

    async def run_flush_serial() -> None:
        """Single-flight STT → Gemini → TTS; buffer snapshot is brief, awaits hold pipeline_lock only."""
        async with pipeline_lock:
            async with buffer_lock:
                if not speech_buffer:
                    return
                pcm16 = bytes(speech_buffer)
                speech_buffer.clear()
            rms = rms_int16_le(pcm16)
            dur_s = len(pcm16) / (2 * UPLINK_SAMPLE_RATE)
            min_rms = float(os.environ.get("DEVELOPER_WS_MIN_INPUT_RMS", "20"))
            if dur_s >= 0.05 and rms < min_rms:
                print(
                    f"[developer_ws] skip flush (low energy) user_id={user_id} "
                    f"pcm={len(pcm16)}B ~{dur_s:.2f}s rms={rms} (min_rms={min_rms})"
                )
                audio.mark_turn_complete()
                return
            text = await transcribe_pcm16(pcm16, UPLINK_SAMPLE_RATE)
            print(
                f"[developer_ws] user_id={user_id} transcript: {text!r} "
                f"(pcm={len(pcm16)}B ~{dur_s:.2f}s rms={rms})"
            )
            if not text.strip():
                audio.mark_turn_complete()
                return
            if websocket.client_state != WebSocketState.CONNECTED:
                print("[developer_ws] client gone after STT; skipping Gemini/TTS")
                await audio.shutdown_playback()
                return
            reply = await gemini_reply(text)
            print(f"[developer_ws] user_id={user_id} gemini_reply: {reply!r}")
            if not reply.strip():
                audio.mark_turn_complete()
                return
            if websocket.client_state != WebSocketState.CONNECTED:
                print("[developer_ws] client gone after Gemini; skipping TTS")
                await audio.shutdown_playback()
                return
            pcm24 = await synthesize_speech_pcm24(reply)
            if websocket.client_state != WebSocketState.CONNECTED:
                print("[developer_ws] client gone after TTS synth; skipping downlink")
                await audio.shutdown_playback()
                return
            if pcm24:
                audio.add_playback_pcm(pcm24)
            audio.mark_turn_complete()

    async def arm_silence_timer() -> None:
        """After ``end_silence_s`` with no newer audio, flush one utterance."""
        nonlocal silence_task, pending_arm_id
        await cancel_silence_task()
        pending_arm_id += 1
        arm_id = pending_arm_id

        async def watcher() -> None:
            try:
                await asyncio.sleep(end_silence_s)
            except asyncio.CancelledError:
                return
            if arm_id != pending_arm_id:
                return
            schedule_flush()

        silence_task = asyncio.create_task(watcher())

    try:
        while True:
            try:
                msg = await websocket.receive_text()
            except RuntimeError as e:
                err = str(e).lower()
                if "accept" in err or "not connected" in err:
                    print(f"[developer_ws] receive ended (socket closed) user_id={user_id}")
                    break
                raise
            data = json.loads(msg)

            if data.get("interrupt") or (
                data.get("text") and "stop" in str(data.get("text", "")).lower()
            ):
                async with buffer_lock:
                    speech_buffer.clear()
                pending_arm_id += 1
                await cancel_silence_task()
                await audio.interrupt()
                continue

            if "audio" in data:
                payload = base64.b64decode(data["audio"])
                audio_bytes = None
                if data.get("codec") == "opus":
                    sr = int(data.get("sr", UPLINK_SAMPLE_RATE))
                    frame_ms = int(data.get("frame_ms", 20))
                    frame_samples = sr * frame_ms // 1000
                    try:
                        audio_bytes = audio.decode_uplink_opus(
                            payload, sample_rate=sr, frame_samples=frame_samples
                        )
                    except Exception as decode_err:
                        print(
                            f"[developer_ws] uplink opus decode failed user_id={user_id}: {decode_err}"
                        )
                        audio_bytes = None
                else:
                    audio_bytes = payload

                if audio_bytes:
                    async with buffer_lock:
                        speech_buffer.extend(audio_bytes)

                if data.get("turn_complete") is True:
                    pending_arm_id += 1
                    await cancel_silence_task()
                    schedule_flush()
                elif audio_bytes:
                    await arm_silence_timer()
                continue

            if data.get("turn_complete") is True and not data.get("audio"):
                pending_arm_id += 1
                await cancel_silence_task()
                schedule_flush()

    except WebSocketDisconnect:
        print(f"[developer_ws] disconnected user_id={user_id}")
    except Exception as e:
        print(f"[developer_ws] error user_id={user_id}: {e}")
        traceback.print_exc()
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        pending_arm_id += 1
        await cancel_silence_task()
        try:
            async with buffer_lock:
                has_leftover = len(speech_buffer) > 0
            if has_leftover:
                await asyncio.wait_for(run_flush_serial(), timeout=45.0)
        except asyncio.TimeoutError:
            print(f"[developer_ws] shutdown flush timed out user_id={user_id}")
        except Exception:
            pass
        await audio.shutdown_playback()
