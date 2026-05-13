"""Single-flight STT → Gemini → TTS orchestration for one connection."""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from audio_codec import UPLINK_SAMPLE_RATE, rms_int16_le

from .audio_io import AudioIO
from .llm import gemini_reply
from .scratchpad import Scratchpad
from .stt import transcribe_pcm16
from .tts import synthesize_speech_pcm24
from .utterance import UtteranceBuffer

log = logging.getLogger("developer_ws")


class SpeechPipeline:
    def __init__(
        self,
        websocket: WebSocket,
        user_id: str,
        utterance: UtteranceBuffer,
        audio: AudioIO,
        scratchpad: Scratchpad,
    ) -> None:
        self._ws = websocket
        self._user_id = user_id
        self._utterance = utterance
        self._audio = audio
        self._scratchpad = scratchpad
        # At most one STT→LLM→TTS turn at a time; new flushes wait their turn.
        self._lock = asyncio.Lock()
        self._min_rms = float(os.environ.get("DEVELOPER_WS_MIN_INPUT_RMS", "20"))

    def _alive(self) -> bool:
        try:
            return self._ws.client_state == WebSocketState.CONNECTED
        except Exception:
            return False

    async def _abort(self, label: str) -> None:
        log.info("client gone after %s; skipping rest user_id=%s", label, self._user_id)
        await self._audio.shutdown_playback()

    def _is_low_energy(self, pcm: bytes) -> bool:
        # Below ~50 ms there is nothing to transcribe; very short buffers are passed through.
        duration_s = len(pcm) / (2 * UPLINK_SAMPLE_RATE)
        return duration_s >= 0.05 and rms_int16_le(pcm) < self._min_rms

    async def flush(self) -> None:
        async with self._lock:
            pcm16 = await self._utterance.snapshot_and_clear()
            if not pcm16:
                return

            if self._is_low_energy(pcm16):
                log.info(
                    "skip flush (low energy) user_id=%s pcm=%dB rms=%d",
                    self._user_id, len(pcm16), rms_int16_le(pcm16),
                )
                self._audio.mark_turn_complete()
                return

            text = await transcribe_pcm16(pcm16, UPLINK_SAMPLE_RATE)
            log.info(
                "user_id=%s transcript=%r (pcm=%dB ~%.2fs)",
                self._user_id, text, len(pcm16), len(pcm16) / (2 * UPLINK_SAMPLE_RATE),
            )
            if not text.strip():
                self._audio.mark_turn_complete()
                return
            # Snapshot history BEFORE recording the new user turn — Gemini receives prior
            # turns as context, then the current transcript is appended inside gemini_reply.
            history = self._scratchpad.history_messages()
            self._scratchpad.add_user(text)
            if not self._alive():
                await self._abort("STT")
                return

            reply = await gemini_reply(text, history=history)
            log.info("user_id=%s gemini_reply=%r", self._user_id, reply)
            if not reply.strip():
                self._audio.mark_turn_complete()
                return
            self._scratchpad.add_assistant(reply)
            if not self._alive():
                await self._abort("Gemini")
                return

            pcm24 = await synthesize_speech_pcm24(reply)
            if not self._alive():
                await self._abort("TTS synth")
                return
            if pcm24:
                self._audio.add_playback_pcm(pcm24)
            self._audio.mark_turn_complete()

    def schedule_flush(self) -> None:
        """Fire-and-forget flush so callers (silence timer, msg handler) stay non-blocking."""

        async def _run() -> None:
            try:
                await self.flush()
            except Exception as e:
                log.exception("flush failed user_id=%s: %s", self._user_id, e)

        asyncio.create_task(_run())
