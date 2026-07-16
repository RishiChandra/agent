"""Vosk speech-to-text.

`preload_vosk_model()` is called from main's lifespan startup so the first STT call
doesn't pay the 5-10s cold-load tax. `transcribe_pcm16(pcm, sr)` accepts int16 mono PCM
at the given sample rate and returns the recognized text (empty string on no-speech).
Model path is read from `VOSK_MODEL_PATH` in the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

log = logging.getLogger("developer_ws")

_model = None
_model_path: str | None = None
_load_lock = threading.Lock()


def _load_model_sync():
    """Load (or return cached) Vosk model from VOSK_MODEL_PATH. Returns None if unset/unavailable."""
    global _model, _model_path
    path = os.environ.get("VOSK_MODEL_PATH", "").strip()
    if not path or not os.path.isdir(path):
        return None
    with _load_lock:
        if _model is not None and _model_path == path:
            return _model
        try:
            from vosk import Model
        except ImportError:
            log.warning(
                "vosk not installed. pip install vosk and set VOSK_MODEL_PATH "
                "(see https://alphacephei.com/vosk/models)"
            )
            return None
        _model = Model(path)
        _model_path = path
        return _model


async def preload_vosk_model() -> None:
    """Warm the model cache during FastAPI startup so the first STT call doesn't stall.

    Called by: `lifespan()` in `app/main.py`. Safe to no-op (logs a warning) if the
    model path is unset/missing — STT will then return empty strings.
    """
    await asyncio.to_thread(_load_model_sync)


class StreamingTranscriber:
    """Incremental Vosk recognizer for one utterance.

    Feed audio as it arrives with `feed(pcm)` (decode work happens while the
    user is still talking), then call `finalize()` at end-of-utterance for the
    text. This replaces transcribing the whole buffer after the user stops —
    on a slow CPU that single-shot decode ran at ~0.5x real-time, adding
    seconds of dead air per turn.

    One instance per utterance; not reusable after `finalize()`. All decoder
    calls are offloaded to worker threads; callers must await them one at a
    time (the pipeline's single-task frame loop already guarantees this).
    """

    def __init__(self, sample_rate: int) -> None:
        self._sr = int(sample_rate)
        self._rec = None
        self._failed = False

    def _ensure_rec_sync(self):
        if self._rec is None and not self._failed:
            model = _load_model_sync()
            if model is None:
                self._failed = True
                log.warning(
                    "VOSK_MODEL_PATH not set or invalid; STT disabled. "
                    "Point it at an unpacked Vosk model directory."
                )
                return None
            try:
                from vosk import KaldiRecognizer
            except ImportError:
                self._failed = True
                return None
            self._rec = KaldiRecognizer(model, self._sr)
        return self._rec

    def _feed_sync(self, pcm: bytes) -> None:
        rec = self._ensure_rec_sync()
        if rec is None or not pcm:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        rec.AcceptWaveform(pcm)

    async def feed(self, pcm: bytes) -> None:
        """Push one audio chunk into the recognizer (worker thread)."""
        await asyncio.to_thread(self._feed_sync, pcm)

    def _finalize_sync(self) -> str:
        rec = self._rec
        if rec is None:
            return ""
        # Trailing zeros help the decoder finalize the last word.
        pad_ms = int(os.environ.get("VOSK_END_PAD_MS", "400"))
        rec.AcceptWaveform(b"\x00" * (int(self._sr * pad_ms / 1000) * 2))
        try:
            return (json.loads(rec.FinalResult()).get("text") or "").strip()
        except json.JSONDecodeError:
            return ""

    async def finalize(self) -> str:
        """Flush the decoder and return the utterance text (worker thread)."""
        return await asyncio.to_thread(self._finalize_sync)


def _transcribe_sync(pcm: bytes, sample_rate: int) -> str:
    sr = int(sample_rate)
    # Below ~80 ms the recognizer can't produce a stable result.
    if len(pcm) < int(sr * 0.08) * 2:
        return ""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    model = _load_model_sync()
    if model is None:
        log.warning(
            "VOSK_MODEL_PATH not set or invalid; STT disabled. "
            "Point it at an unpacked Vosk model directory."
        )
        return ""
    try:
        from vosk import KaldiRecognizer
    except ImportError:
        return ""

    # Trailing zeros help the decoder finalize the last word.
    pad_ms = int(os.environ.get("VOSK_END_PAD_MS", "400"))
    pcm_padded = pcm + b"\x00" * (int(sr * pad_ms / 1000) * 2)

    rec = KaldiRecognizer(model, sr)
    if len(pcm_padded) <= 256000:
        rec.AcceptWaveform(pcm_padded)
    else:
        # Chunked feed avoids large single-shot decoder allocations.
        step = 8000
        for i in range(0, len(pcm_padded), step):
            rec.AcceptWaveform(pcm_padded[i : i + step])
    try:
        return (json.loads(rec.FinalResult()).get("text") or "").strip()
    except json.JSONDecodeError:
        return ""


async def transcribe_pcm16(pcm: bytes, sample_rate: int) -> str:
    """Transcribe mono int16 PCM. Offloaded to a worker thread.

    Called by: `VoskUtteranceSTTProcessor.process_frame` in `pipecat_bits.py`
    on each `UserStoppedSpeakingFrame`, with the audio accumulated since the
    matching start frame. Returns "" on no-speech.
    """
    return await asyncio.to_thread(_transcribe_sync, pcm, sample_rate)
