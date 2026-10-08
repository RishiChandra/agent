"""Silero VAD endpointing for one developer-WS connection.

Replaces the RMS gate + fixed silence timer as the *primary* end-of-turn
signal. The Silero ONNX model (bundled with pipecat-ai, run via onnxruntime)
classifies each 32 ms chunk as speech / not-speech, so the turn can end after
`DEVELOPER_WS_SILERO_STOP_SECS` (default 0.8 s) of confirmed non-speech
instead of `DEVELOPER_WS_END_SILENCE_SEC` (default 2.0 s) of low RMS.

The legacy silence timer in `utterance.py` stays armed as a fallback: if the
VAD never confirms speech (e.g. sustained non-speech noise opened the capture
window), the timer still closes the turn at its old cadence. Whichever fires
first calls `pipeline.signal_user_stopped`; the loser is invalidated via
`bump_arm_id` / the pipeline's `_speaking` guard.

Set `DEVELOPER_WS_USE_SILERO_VAD=0` (or leave onnxruntime uninstalled) to
disable — `create_silero_vad` returns None and the endpoint falls back to the
timer-only behaviour.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
from typing import Optional

from audio_codec import UPLINK_SAMPLE_RATE

log = logging.getLogger("developer_ws")


class VADEvent(enum.Enum):
    """Turn-taking transition detected while processing one audio batch."""

    NONE = "none"
    STARTED = "started"
    STOPPED = "stopped"


# None = not probed yet; True/False = result of the first real import attempt.
# Import failure (onnxruntime missing) is remembered so we don't re-raise the
# same ImportError once per session.
_deps_available: Optional[bool] = None


def _load_analyzer_deps():
    """Import the pipecat Silero pieces lazily.

    Returns (SileroVADAnalyzer, VADParams) or None if unavailable. Kept out of
    module import so `endpoint.py` (and unit tests without pipecat/onnxruntime
    installed) can import this module unconditionally.
    """
    global _deps_available
    if _deps_available is False:
        return None
    try:
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
    except Exception as e:
        _deps_available = False
        log.warning(
            "Silero VAD unavailable (%s); falling back to silence-timer endpointing", e
        )
        return None
    _deps_available = True
    return SileroVADAnalyzer, VADParams


def _enabled() -> bool:
    return os.environ.get("DEVELOPER_WS_USE_SILERO_VAD", "1").strip() not in ("0", "false", "no")


class SileroVAD:
    """Per-session speech/turn tracker on top of pipecat's SileroVADAnalyzer.

    The analyzer owns the hysteresis (QUIET → STARTING → SPEAKING → STOPPING →
    QUIET, tuned by start/stop secs + confidence + min volume); this wrapper
    reduces its state stream to the two transitions the endpoint acts on:
    STARTED (confirmed speech began) and STOPPED (confirmed speech ended —
    the fast end-of-turn signal).

    `analyzer` is injectable for tests; anything with async
    `analyze_audio(bytes) -> state` (state exposing `.name`), `set_params(p)`,
    and `.params` works.
    """

    def __init__(self, *, user_id: str = "", analyzer=None) -> None:
        self._user_id = user_id
        self._speaking = False   # reached SPEAKING since last reset
        self._dirty = False      # audio processed since last reset
        if analyzer is not None:
            self._analyzer = analyzer
            return
        deps = _load_analyzer_deps()
        if deps is None:
            raise RuntimeError("Silero VAD dependencies unavailable")
        analyzer_cls, params_cls = deps
        params = params_cls(
            confidence=float(os.environ.get("DEVELOPER_WS_SILERO_CONFIDENCE", "0.7")),
            start_secs=float(os.environ.get("DEVELOPER_WS_SILERO_START_SECS", "0.2")),
            stop_secs=float(os.environ.get("DEVELOPER_WS_SILERO_STOP_SECS", "0.8")),
            min_volume=float(os.environ.get("DEVELOPER_WS_SILERO_MIN_VOLUME", "0.6")),
        )
        self._analyzer = analyzer_cls(sample_rate=UPLINK_SAMPLE_RATE, params=params)
        # VADAnalyzer defers state-machine init (frame sizes, counters) to
        # set_sample_rate — without this call analyze_audio raises.
        self._analyzer.set_sample_rate(UPLINK_SAMPLE_RATE)

    @property
    def speaking(self) -> bool:
        """True between a STARTED and the matching STOPPED event."""
        return self._speaking

    async def process(self, pcm: bytes) -> VADEvent:
        """Feed one uplink batch; return the transition it caused, if any.

        Called by `_handle_audio` in endpoint.py for every non-bridge batch.
        Batches of any size are fine — the analyzer buffers internally and
        evaluates fixed 512-sample chunks.
        """
        if not pcm:
            return VADEvent.NONE
        self._dirty = True
        state = await self._analyzer.analyze_audio(pcm)
        name = getattr(state, "name", str(state))
        if name == "SPEAKING" and not self._speaking:
            self._speaking = True
            return VADEvent.STARTED
        if name == "QUIET" and self._speaking:
            self._speaking = False
            return VADEvent.STOPPED
        return VADEvent.NONE

    def reset(self) -> None:
        """Drop any half-tracked utterance (interrupt, turn_complete, bridge open).

        Re-arms the analyzer's hysteresis at QUIET so stale STARTING/STOPPING
        counts can't leak into the next utterance. No-op when nothing was
        processed since the last reset, so per-batch calls in bridge mode are
        cheap and don't spam the analyzer's set_params debug log.
        """
        if not self._dirty:
            return
        self._dirty = False
        self._speaking = False
        try:
            self._analyzer.set_params(self._analyzer.params)
        except Exception:
            log.exception("user_id=%s silero reset failed", self._user_id)


def create_silero_vad(user_id: str = "") -> Optional[SileroVAD]:
    """Build a per-session SileroVAD, or None when disabled/unavailable.

    Called once per session by `developer_websocket_endpoint`. Returning None
    keeps the endpoint on the legacy silence-timer-only path.
    """
    if not _enabled():
        return None
    try:
        return SileroVAD(user_id=user_id)
    except Exception as e:
        log.warning("user_id=%s silero vad init failed (%s); timer-only endpointing", user_id, e)
        return None


async def preload_silero_vad() -> None:
    """Warm the ONNX session during FastAPI startup so the first session doesn't stall.

    Called by `lifespan()` in `app/main.py`, mirroring `preload_vosk_model` /
    `preload_piper_voice`. Builds and discards one analyzer: validates that
    onnxruntime + the bundled model load, and leaves the model file hot in the
    page cache for the real per-session constructions. Logs and no-ops when
    disabled or unavailable.
    """
    if not _enabled():
        log.info("silero vad disabled via DEVELOPER_WS_USE_SILERO_VAD")
        return

    def _build() -> bool:
        deps = _load_analyzer_deps()
        if deps is None:
            return False
        analyzer_cls, _ = deps
        analyzer = analyzer_cls(sample_rate=UPLINK_SAMPLE_RATE)
        analyzer.set_sample_rate(UPLINK_SAMPLE_RATE)
        return True

    ok = await asyncio.to_thread(_build)
    if ok:
        log.info("silero vad preloaded")
