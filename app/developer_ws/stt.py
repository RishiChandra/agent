"""Vosk STT for the developer WebSocket path."""

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
    """Warm the model cache during FastAPI startup so first STT call avoids cold-load latency."""
    await asyncio.to_thread(_load_model_sync)


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
    """Transcribe mono int16 PCM (offloaded to thread)."""
    return await asyncio.to_thread(_transcribe_sync, pcm, sample_rate)
