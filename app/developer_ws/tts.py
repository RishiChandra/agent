"""Piper text-to-speech.

`synthesize_speech_pcm24(text)` returns int16 mono PCM at the downlink sample rate
(24 kHz), resampling from Piper's native rate. Returned bytes are queued on
`AudioIO` for Opus encoding and downlink delivery.

Piper is cross-platform (Windows / Linux / macOS) and uses the same `.onnx` voice
file everywhere, so local and deployed runs sound identical.

Voice path resolution:
  1. ``PIPER_MODEL_PATH`` env var (relative paths resolve from CWD).
  2. Default: ``piper_voices/en_US-amy-medium.onnx`` relative to repo root.
"""

from __future__ import annotations

import asyncio
import audioop
import io
import logging
import os
import threading
import wave

from audio_codec import DOWNLINK_SAMPLE_RATE

log = logging.getLogger("developer_ws")

_DEFAULT_MODEL = "piper_voices/en_US-amy-medium.onnx"

_voice = None
_voice_path: str | None = None
_load_lock = threading.Lock()


def _resolve_model_path() -> str | None:
    raw = os.environ.get("PIPER_MODEL_PATH", "").strip() or _DEFAULT_MODEL
    for candidate in (raw, os.path.join("..", raw)):
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_voice():
    """Load (or return cached) Piper voice. Returns None if model file is missing."""
    global _voice, _voice_path
    path = _resolve_model_path()
    if not path:
        return None
    with _load_lock:
        if _voice is not None and _voice_path == path:
            return _voice
        try:
            from piper import PiperVoice
        except ImportError:
            log.warning("piper-tts not installed. pip install piper-tts")
            return None
        try:
            _voice = PiperVoice.load(path)
            _voice_path = path
            log.info("piper voice loaded: %s", path)
            return _voice
        except Exception as e:
            log.warning("piper voice load failed (%s): %s", path, e)
            return None


def _synthesize_sync(text: str) -> bytes:
    """Render text via Piper, return mono int16 PCM at DOWNLINK_SAMPLE_RATE."""
    t = text.strip()
    if not t:
        return b""
    voice = _load_voice()
    if voice is None:
        log.warning("piper voice unavailable; TTS disabled.")
        return b""
    try:
        # Piper writes a WAV header + PCM into the wave.Wave_write. Sample rate
        # comes from the voice's config; we resample to DOWNLINK_SAMPLE_RATE.
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            voice.synthesize_wav(t, wav)
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            sample_width = wf.getsampwidth()
            pcm = wf.readframes(wf.getnframes())
    except Exception as e:
        log.warning("piper synthesize failed: %s", e)
        return b""

    if sample_width != 2:
        log.warning("piper produced %d-bit audio; expected 16-bit", sample_width * 8)
        return b""
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif channels != 1:
        return b""
    if rate == DOWNLINK_SAMPLE_RATE:
        return pcm
    resampled, _ = audioop.ratecv(pcm, 2, 1, rate, DOWNLINK_SAMPLE_RATE, None)
    return resampled


async def preload_piper_voice() -> None:
    """Warm the Piper voice cache so the first TTS call doesn't stall (~1-2s)."""
    await asyncio.to_thread(_load_voice)


async def synthesize_speech_pcm24(text: str) -> bytes:
    """Synthesize speech and return int16 mono PCM at the downlink sample rate.

    Called by: `pipeline._speak` after Gemini returns plain text, after a tool ack,
    after a service-ping announcement, and on bridge remote-close notification.
    Offloaded to a worker thread because Piper inference is CPU-bound.
    """
    return await asyncio.to_thread(_synthesize_sync, text)
