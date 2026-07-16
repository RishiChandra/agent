"""Pipecat FrameProcessors that wire the existing developer_ws components into
a Pipecat Pipeline.

Five custom processors:

  - `SessionSource`            : passthrough at the head of the pipeline; gives the
                                 endpoint a stable name to queue frames against.
  - `BridgeGateProcessor`      : when `bridge.active`, forwards inbound audio to the
                                 remote bridge and swallows the frame so STT/LLM/TTS
                                 don't see it.
  - `VoskUtteranceSTTProcessor`: accumulates `InputAudioRawFrame`s between
                                 `UserStartedSpeakingFrame` and `UserStoppedSpeakingFrame`,
                                 then runs Vosk once and emits a `TranscriptionFrame`.
                                 Utterance-level (not streaming) to match how Vosk works.
  - `PiperTTSProcessor`        : accepts `TextFrame` / `TTSSpeakFrame`, runs Piper, emits
                                 `TTSAudioRawFrame`s. Pure FrameProcessor (not TTSService)
                                 so we control framing without buying into the heavy base.
  - `AudioIOSinkProcessor`     : sinks `TTSAudioRawFrame`s into the existing `AudioIO`,
                                 preserving Opus encode + the legacy wire protocol so the
                                 test client keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from audio_codec import DOWNLINK_SAMPLE_RATE, UPLINK_SAMPLE_RATE

from .audio_io import AudioIO
from .bridge import RemoteAudioBridge
from .stt import StreamingTranscriber
from .tts import synthesize_speech_pcm24_stream

log = logging.getLogger("developer_ws")


class SessionSource(FrameProcessor):
    """Head-of-pipeline passthrough.

    Exists so the endpoint can `task.queue_frame(...)` frames and they enter the
    pipeline at a known point. No transformation; everything is pushed downstream.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class BridgeGateProcessor(FrameProcessor):
    """Gates audio based on `bridge.active`.

    When the bridge is open, the assistant pipeline (STT/LLM/TTS) is bypassed —
    uplink audio is forwarded directly to the remote service via the bridge.
    When closed, audio flows through unchanged.

    Constructed once per session. Reads `bridge.active` per-frame so it tracks
    state without needing explicit start/stop frames.
    """

    def __init__(self, bridge: RemoteAudioBridge, **kwargs) -> None:
        super().__init__(**kwargs)
        self._bridge = bridge

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame) and self._bridge.active:
            # Forward to remote, swallow the frame so STT never sees it.
            if frame.audio:
                try:
                    await self._bridge.send_uplink_pcm(frame.audio)
                except Exception:
                    log.exception("bridge forward failed")
            return

        await self.push_frame(frame, direction)


class VoskUtteranceSTTProcessor(FrameProcessor):
    """Vosk-backed utterance STT, streaming.

    Feeds `InputAudioRawFrame` audio into an incremental Vosk recognizer between
    `UserStartedSpeakingFrame` and `UserStoppedSpeakingFrame`, so decode work
    happens *while the user talks*. On stop, only the finalize step remains
    (~pad + FinalResult), which turns the old multi-second post-utterance STT
    stall into tens of milliseconds.
    """

    def __init__(self, *, user_id: str = "", sample_rate: int = UPLINK_SAMPLE_RATE, **kwargs) -> None:
        super().__init__(**kwargs)
        self._user_id = user_id
        self._sample_rate = sample_rate
        self._stt: StreamingTranscriber | None = None
        self._fed_bytes = 0
        self._capturing = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._stt = StreamingTranscriber(self._sample_rate)
            self._fed_bytes = 0
            self._capturing = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._capturing = False
            stt, self._stt = self._stt, None
            fed = self._fed_bytes
            # Push the stop frame downstream first so any aggregator sees it.
            await self.push_frame(frame, direction)
            if stt is None or not fed:
                return
            t_stt = time.monotonic()
            try:
                text = await stt.finalize()
            except Exception:
                log.exception("vosk finalize failed user_id=%s", self._user_id)
                return
            stt_ms = int((time.monotonic() - t_stt) * 1000)
            log.info(
                "user_id=%s transcript=%r (pcm=%dB ~%.2fs) finalize_ms=%d",
                self._user_id, text, fed, fed / (2 * self._sample_rate),
                stt_ms,
            )
            if text and text.strip():
                await self.push_frame(
                    TranscriptionFrame(
                        text=text.strip(),
                        user_id=self._user_id,
                        timestamp=str(time.time()),
                    ),
                    direction,
                )
            return

        if isinstance(frame, InputAudioRawFrame):
            if self._capturing and frame.audio and self._stt is not None:
                self._fed_bytes += len(frame.audio)
                try:
                    await self._stt.feed(frame.audio)
                except Exception:
                    log.exception("vosk feed failed user_id=%s", self._user_id)
            # Don't push raw audio downstream — Vosk has consumed it. Keeps
            # the pipeline clean of frames the LLM/TTS don't care about.
            return

        await self.push_frame(frame, direction)


class PiperTTSProcessor(FrameProcessor):
    """Piper TTS as a FrameProcessor — streaming.

    Accepts:
      - `TTSSpeakFrame` — synthesized immediately (used by tool handlers for
        deterministic acks; bypasses the LLM).
      - `TextFrame`     — synthesized when produced by the LLM.

    Emits one `TTSAudioRawFrame` per sentence (Piper's natural chunking unit).
    `BotStartedSpeakingFrame` fires when the first chunk is ready;
    `BotStoppedSpeakingFrame` fires after the last chunk so `AudioIOSinkProcessor`
    can flush Opus residual at the turn boundary.

    Streaming wins ~500–1000 ms of perceived latency for multi-sentence replies:
    the first sentence reaches the user while Piper is still synthesizing the
    second.
    """

    def __init__(self, *, sample_rate: int = DOWNLINK_SAMPLE_RATE, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sample_rate = sample_rate

    async def _speak(self, text: str, direction: FrameDirection) -> None:
        t = (text or "").strip()
        if not t:
            return
        started = False
        t_start = time.monotonic()
        ttfc_ms: int | None = None
        chunks = 0
        try:
            async for pcm in synthesize_speech_pcm24_stream(t):
                if not pcm:
                    continue
                chunks += 1
                if not started:
                    # Defer BotStarted until we actually have audio — if Piper
                    # fails before producing anything, we don't open a turn
                    # that never closes.
                    ttfc_ms = int((time.monotonic() - t_start) * 1000)
                    await self.push_frame(BotStartedSpeakingFrame(), direction)
                    started = True
                await self.push_frame(
                    TTSAudioRawFrame(
                        audio=pcm,
                        sample_rate=self._sample_rate,
                        num_channels=1,
                    ),
                    direction,
                )
        except Exception:
            log.exception("piper stream failed text=%r", t[:80])
        finally:
            if started:
                total_ms = int((time.monotonic() - t_start) * 1000)
                log.info(
                    "tts text=%r ttfc_ms=%s total_ms=%d chunks=%d",
                    t[:60], ttfc_ms, total_ms, chunks,
                )
                await self.push_frame(BotStoppedSpeakingFrame(), direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSSpeakFrame):
            # Deterministic ack path used by tool handlers (bypasses LLM).
            await self._speak(frame.text, direction)
            return

        if isinstance(frame, TextFrame) and not isinstance(frame, TranscriptionFrame):
            # LLM-produced text. Synthesize.
            await self._speak(frame.text, direction)
            return

        await self.push_frame(frame, direction)


class AudioIOSinkProcessor(FrameProcessor):
    """Bridge between Pipecat frames and the existing `AudioIO` downlink.

    Keeps the legacy wire protocol intact (Opus encode, coalescing, seq numbers,
    timing fields the test client expects). Without this, we'd have to write a
    custom `FrameSerializer` and replicate every detail in AudioIO — a much
    riskier change.

    On `TTSAudioRawFrame`: push PCM to `audio.add_playback_pcm`.
    On `BotStoppedSpeakingFrame`: call `audio.mark_turn_complete()` to flush
    any Opus residual so the tail of the assistant turn reaches the client.
    On `StartInterruptionFrame`: clear pending downlink playback.
    """

    def __init__(self, audio: AudioIO, **kwargs) -> None:
        super().__init__(**kwargs)
        self._audio = audio

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSAudioRawFrame):
            if frame.audio:
                self._audio.add_playback_pcm(frame.audio)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._audio.mark_turn_complete()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            try:
                await self._audio.interrupt()
            except Exception:
                log.exception("audio interrupt failed")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, CancelFrame)):
            try:
                await self._audio.shutdown_playback()
            except Exception:
                log.exception("audio shutdown failed")
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
