"""Vosk speech-to-text.

`preload_vosk_model()` is called from main's lifespan startup so the first STT call
doesn't pay the 5-10s cold-load tax. `transcribe_pcm16(pcm, sr)` accepts int16 mono PCM
at the given sample rate and returns the recognized text (empty string on no-speech).
Model path is read from `VOSK_MODEL_PATH` in the environment.

Recognizers are pooled. A fresh `KaldiRecognizer` on the lgraph model decodes its
first utterance far slower than real time (a 1.5-3.3 s stall on the first chunk,
measured 2026-10-06), while a reused one runs at ~0.15-0.19x real time.
`FinalResult()` resets a recognizer for the next utterance but keeps it warm, so
`StreamingTranscriber` borrows one from `_POOL` and hands it back after
`finalize()`. `warm_recognizer_pool(pcm)` pre-warms the pool at startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from typing import Callable

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


class RecognizerPool:
    """Process-wide pool of reusable recognizers, keyed by sample rate.

    `acquire()` hands out an idle recognizer (warm if it has been used before)
    or builds a new one; it never blocks, so a burst of concurrent talkers just
    gets cold recognizers. `release()` keeps up to `max_idle` per sample rate
    and retires a recognizer after `max_uses` utterances to bound the growth of
    its decoding cache. Each recognizer is used by one utterance at a time.
    """

    def __init__(
        self,
        factory: Callable[[int], object | None],
        *,
        max_idle: int,
        max_uses: int,
    ) -> None:
        self._factory = factory
        self._max_idle = max(0, max_idle)
        self._max_uses = max(1, max_uses)
        self._idle: dict[int, deque] = {}
        self._uses: dict[int, int] = {}  # id(rec) -> utterances decoded
        self._lock = threading.Lock()

    def acquire(self, sample_rate: int) -> tuple[object | None, bool]:
        """Return `(recognizer, warm)`. `recognizer` is None if STT is unavailable."""
        with self._lock:
            idle = self._idle.get(sample_rate)
            if idle:
                rec = idle.pop()
                return rec, self._uses.get(id(rec), 0) > 0
        rec = self._factory(sample_rate)
        if rec is not None:
            with self._lock:
                self._uses[id(rec)] = 0
        return rec, False

    def release(self, sample_rate: int, rec: object, *, decoded: bool) -> None:
        """Return a recognizer that has been reset (`FinalResult()` or `Reset()`)."""
        with self._lock:
            uses = self._uses.get(id(rec), 0) + (1 if decoded else 0)
            idle = self._idle.setdefault(sample_rate, deque())
            if uses >= self._max_uses or len(idle) >= self._max_idle:
                self._uses.pop(id(rec), None)
                return
            self._uses[id(rec)] = uses
            idle.append(rec)

    def idle_count(self, sample_rate: int) -> int:
        with self._lock:
            return len(self._idle.get(sample_rate, ()))


def _new_recognizer(sample_rate: int):
    model = _load_model_sync()
    if model is None:
        return None
    try:
        from vosk import KaldiRecognizer
    except ImportError:
        return None
    return KaldiRecognizer(model, int(sample_rate))


_POOL = RecognizerPool(
    _new_recognizer,
    max_idle=int(os.environ.get("VOSK_POOL_MAX_IDLE", "4")),
    max_uses=int(os.environ.get("VOSK_POOL_MAX_UTTERANCES", "500")),
)


def _reset_recognizer(rec) -> None:
    """Discard any partial utterance so the next borrower starts clean."""
    reset = getattr(rec, "Reset", None)
    if callable(reset):
        reset()
    else:
        rec.FinalResult()


async def warm_recognizer_pool(
    pcm: bytes,
    sample_rate: int = 16000,
    size: int | None = None,
    pool: RecognizerPool | None = None,
) -> int:
    """Decode `pcm` (int16 mono speech) once on each of `size` recognizers and pool them.

    Called by: `lifespan()` in `app/main.py`, in the background, with a short
    Piper-synthesised clip. Each warm-up costs a few seconds of CPU. All
    recognizers are borrowed before any is returned, so each one gets warmed
    rather than the same one `size` times. Returns how many were warmed.
    """
    pool = pool or _POOL
    if size is None:
        size = int(os.environ.get("VOSK_POOL_WARM", "2"))
    if size <= 0 or not pcm:
        return 0
    recs = []
    for _ in range(size):
        rec, _warm = await asyncio.to_thread(pool.acquire, sample_rate)
        if rec is None:
            break
        recs.append(rec)
    warmed = 0
    for rec in recs:
        try:
            await asyncio.to_thread(_decode_and_reset, rec, pcm)
        except Exception:
            log.exception("vosk warm-up failed; dropping recognizer")
            continue
        pool.release(sample_rate, rec, decoded=True)
        warmed += 1
    return warmed


def _decode_and_reset(rec, pcm: bytes) -> None:
    step = 8190  # same size as the live uplink batches
    for i in range(0, len(pcm) - len(pcm) % 2, step):
        rec.AcceptWaveform(pcm[i : i + step])
    rec.FinalResult()


_WARMUP_TEXT = (
    "Hey, how's it going? Can I speak to Kairos? Have Tabletop book a table for "
    "two at seven tonight. Remind me to call mom tomorrow morning. What's the "
    "weather like today? Okay, thanks, bye."
)


async def warm_recognizer_pool_with_tts() -> int:
    """Synthesize a short warm-up clip with Piper and warm the pool with it.

    Called by: `lifespan()` in `app/main.py` as a background task, after the
    Piper voice is preloaded. Returns how many recognizers were warmed.
    """
    import audioop

    from audio_codec import DOWNLINK_SAMPLE_RATE, UPLINK_SAMPLE_RATE

    from .tts import synthesize_speech_pcm24

    pcm24 = await synthesize_speech_pcm24(_WARMUP_TEXT)
    if not pcm24:
        return 0
    pcm16, _ = audioop.ratecv(pcm24, 2, 1, DOWNLINK_SAMPLE_RATE, UPLINK_SAMPLE_RATE, None)
    return await warm_recognizer_pool(pcm16, UPLINK_SAMPLE_RATE)


class StreamingTranscriber:
    """Incremental Vosk recognizer for one utterance.

    Feed audio as it arrives with `feed(pcm)` (decode work happens while the
    user is still talking), then call `finalize()` at end-of-utterance for the
    text. This replaces transcribing the whole buffer after the user stops —
    on a slow CPU that single-shot decode ran at ~0.5x real-time, adding
    seconds of dead air per turn.

    One instance per utterance. The recognizer is borrowed from `_POOL` on the
    first `feed()` and returned by `finalize()` (or `abandon()` if the
    utterance is dropped). All decoder calls are offloaded to worker threads;
    callers must await them one at a time (the pipeline's single-task frame
    loop already guarantees this).
    """

    def __init__(self, sample_rate: int, pool: RecognizerPool | None = None) -> None:
        self._sr = int(sample_rate)
        self._pool = pool or _POOL
        self._rec = None
        self._failed = False
        self._done = False
        self.warm: bool | None = None  # set on first feed; logged by the caller

    def _ensure_rec_sync(self):
        if self._rec is None and not self._failed and not self._done:
            rec, warm = self._pool.acquire(self._sr)
            if rec is None:
                self._failed = True
                log.warning(
                    "VOSK_MODEL_PATH not set or invalid; STT disabled. "
                    "Point it at an unpacked Vosk model directory."
                )
                return None
            self._rec, self.warm = rec, warm
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
        rec, self._rec = self._rec, None
        self._done = True
        if rec is None:
            return ""
        # Trailing zeros help the decoder finalize the last word.
        pad_ms = int(os.environ.get("VOSK_END_PAD_MS", "400"))
        rec.AcceptWaveform(b"\x00" * (int(self._sr * pad_ms / 1000) * 2))
        # FinalResult() also resets the recognizer for its next borrower. If
        # either call raises, the recognizer is simply not returned to the pool.
        raw = rec.FinalResult()
        self._pool.release(self._sr, rec, decoded=True)
        try:
            return (json.loads(raw).get("text") or "").strip()
        except json.JSONDecodeError:
            return ""

    async def finalize(self) -> str:
        """Flush the decoder and return the utterance text (worker thread)."""
        return await asyncio.to_thread(self._finalize_sync)

    def _abandon_sync(self) -> None:
        rec, self._rec = self._rec, None
        self._done = True
        if rec is None:
            return
        try:
            _reset_recognizer(rec)
        except Exception:
            log.exception("vosk reset failed; dropping recognizer")
            return
        self._pool.release(self._sr, rec, decoded=False)

    async def abandon(self) -> None:
        """Drop this utterance without a transcript and return the recognizer."""
        await asyncio.to_thread(self._abandon_sync)


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
