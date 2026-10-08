"""Piper text-to-speech, streaming.

Two entry points share one underlying implementation:

  * ``synthesize_speech_pcm24_stream(text)`` — async generator yielding PCM
    chunks at the downlink sample rate (24 kHz, int16 mono) **as Piper
    produces them**. Piper emits one chunk per sentence, so the first audio
    arrives ~150–400 ms after the call starts instead of waiting for the
    entire synthesis to complete.
  * ``synthesize_speech_pcm24(text)`` — convenience that collects the stream
    into a single ``bytes``. Same audio, just buffered.

The streaming path is what ``PiperTTSProcessor`` uses post-refactor; the
batch wrapper exists for any caller that still wants a one-shot interface.

Piper inference is blocking C++/ONNX, so synthesis runs on a worker thread
via ``asyncio.to_thread`` and pushes finished chunks through an
``asyncio.Queue`` (via ``loop.call_soon_threadsafe``). Resampler state from
``audioop.ratecv`` is threaded across chunks so sentence boundaries don't
produce phase glitches.

Voice path resolution:
  1. ``PIPER_MODEL_PATH`` env var (relative paths resolve from CWD).
  2. Default: ``piper_voices/en_US-amy-medium.onnx`` relative to repo root.

Piper is cross-platform (Windows / Linux / macOS) and uses the same ``.onnx``
voice file everywhere, so local and deployed runs sound identical.
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import os
import threading
from typing import AsyncIterator

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


async def preload_piper_voice() -> None:
    """Warm the Piper voice cache so the first TTS call doesn't stall (~1-2s)."""
    await asyncio.to_thread(_load_voice)


# Sentinel for "stream ended" pushed onto the queue by the worker.
_END = object()


async def synthesize_speech_pcm24_stream(text: str) -> AsyncIterator[bytes]:
    """Synthesize speech, yielding 24 kHz int16 mono PCM chunks as Piper produces them.

    Piper emits one chunk per sentence (see ``PiperVoice.synthesize`` docstring).
    For a multi-sentence reply, this means the first sentence's audio arrives
    while the second is still being synthesized — true pipeline parallelism
    between TTS and downlink transmission.

    Called by: ``PiperTTSProcessor._speak`` in ``pipecat_bits.py`` for every
    ``TextFrame`` (LLM output) and ``TTSSpeakFrame`` (tool handlers, bridge
    ``say`` path, service-ping announcements).

    Yields nothing if the model is unavailable, the text is empty, or
    synthesis fails — callers should handle a 0-chunk stream as a no-op.
    """
    t = (text or "").strip()
    if not t:
        return
    voice = _load_voice()
    if voice is None:
        log.warning("piper voice unavailable; TTS disabled.")
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _worker() -> None:
        """Run Piper synthesis in a worker thread, push chunks onto the queue.

        Uses ``call_soon_threadsafe`` because ``asyncio.Queue`` is not
        thread-safe from outside the loop.
        """
        resample_state = None
        try:
            for chunk in voice.synthesize(t):
                pcm = chunk.audio_int16_bytes
                rate = chunk.sample_rate
                channels = chunk.sample_channels
                if channels == 2:
                    pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
                elif channels != 1:
                    log.warning("piper produced %d channels; skipping chunk", channels)
                    continue
                if rate != DOWNLINK_SAMPLE_RATE:
                    pcm, resample_state = audioop.ratecv(
                        pcm, 2, 1, rate, DOWNLINK_SAMPLE_RATE, resample_state
                    )
                if pcm:
                    loop.call_soon_threadsafe(queue.put_nowait, pcm)
        except BaseException as e:  # surface exceptions to the async side
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _END)

    worker_task = asyncio.create_task(asyncio.to_thread(_worker))

    try:
        while True:
            item = await queue.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                log.warning("piper synthesize failed: %s", item)
                return
            yield item
    finally:
        # Ensure the worker is reaped (the thread will exit on its own once
        # the generator is done; awaiting binds the task back into this loop).
        try:
            await worker_task
        except Exception:
            log.exception("piper worker task raised")


async def synthesize_speech_pcm24(text: str) -> bytes:
    """Batch convenience: collect the streaming synthesis into a single PCM blob.

    Kept for callers that want a one-shot interface; the streaming generator
    is the canonical entry point.
    """
    parts: list[bytes] = []
    async for chunk in synthesize_speech_pcm24_stream(text):
        parts.append(chunk)
    return b"".join(parts)
