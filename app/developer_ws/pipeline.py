"""Pipecat-based voice session orchestration.

Replaces the previous hand-rolled `SpeechPipeline` (lock + STT/LLM/TTS loop) with
a Pipecat `Pipeline` of FrameProcessors:

    SessionSource → BridgeGate → VoskUtteranceSTT → CustomGeminiLLMService
                  → PiperTTS   → AudioIOSink

What stayed:
  - `AudioIO`         (downlink Opus + the exact wire protocol the test client expects)
  - `UtteranceBuffer` (silence timer + RMS gate; signals user-stopped to the pipeline)
  - `RemoteAudioBridge` (the WS bridge is unchanged — gated into/out of the pipeline)
  - `Scratchpad`       (still dumped on close; now mirrors the LLMContext via callback)

What changed:
  - Tool dispatch: `_handle_tool_call` is gone. We call `llm.register_function(name, handler)`
    and the handler pushes a `TTSSpeakFrame` for deterministic acks before returning
    `run_llm=False` via `result_callback` — exactly the workaround the design called for.
  - Conversation history is owned by the LLMContext inside `CustomGeminiLLMService`. The
    scratchpad mirrors it via `on_message_added`.
  - Turn lifecycle is frame-driven; the endpoint signals start/stop-speaking explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import WebSocket
from pipecat.frames.frames import (
    EndFrame,
    FunctionCallResultProperties,
    InputAudioRawFrame,
    InterruptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.llm_service import FunctionCallParams
from starlette.websockets import WebSocketState

from audio_codec import DOWNLINK_SAMPLE_RATE, UPLINK_SAMPLE_RATE, rms_int16_le

from .audio_io import AudioIO
from .bridge import (
    OUTCOME_NO_PICKUP,
    OUTCOME_OK,
    OUTCOME_REJECTED,
    REMOTE_BRIDGE_URL,
    BridgeStartResult,
    RemoteAudioBridge,
)
from .pipecat_bits import (
    AudioIOSinkProcessor,
    BridgeGateProcessor,
    PiperTTSProcessor,
    SessionSource,
    VoskUtteranceSTTProcessor,
)
from .pipecat_llm import CustomGeminiLLMService
from .scratchpad import Scratchpad
from .tools import ALL_TOOLS, START_REMOTE_AUDIO_BRIDGE
from .utterance import UtteranceBuffer

log = logging.getLogger("developer_ws")

_BRIDGE_ACK = "Connecting you to the remote service now."
_BRIDGE_FAIL_GENERIC = "Sorry, I couldn't open the remote connection."
_BRIDGE_FAIL_NO_PICKUP = "The remote service didn't pick up."
_BRIDGE_FAIL_REJECTED = "The remote service declined the call."
_BRIDGE_DISCONNECT = "The remote service disconnected. You're back with me now."
_SERVICE_PING_ANNOUNCE = "Your service wants to speak with you. Connecting you now."


def _fail_message(result: BridgeStartResult) -> str:
    if result.outcome == OUTCOME_NO_PICKUP:
        return _BRIDGE_FAIL_NO_PICKUP
    if result.outcome == OUTCOME_REJECTED:
        if result.detail:
            return f"{_BRIDGE_FAIL_REJECTED} Reason: {result.detail}."
        return _BRIDGE_FAIL_REJECTED
    return _BRIDGE_FAIL_GENERIC


class SpeechPipeline:
    """One per voice session.

    Constructed by `developer_websocket_endpoint` in endpoint.py.
    Driven by:
      - `feed_audio(pcm)`          : called for each non-bridge audio batch.
      - `signal_user_stopped()`    : silence-timer fire or `turn_complete:true`.
      - `interrupt()`              : user said stop or sent `{interrupt:true}`.
      - `on_service_ping(...)`     : HTTP ping route in app/main.py.
      - `close()`                  : final drain on socket close.
    """

    def __init__(
        self,
        websocket: WebSocket,
        user_id: str,
        utterance: UtteranceBuffer,
        audio: AudioIO,
        scratchpad: Scratchpad,
        bridge: RemoteAudioBridge,
    ) -> None:
        self._ws = websocket
        self._user_id = user_id
        self._utterance = utterance
        self._audio = audio
        self._scratchpad = scratchpad
        self._bridge = bridge
        self._min_rms = float(os.environ.get("DEVELOPER_WS_MIN_INPUT_RMS", "20"))

        # Track whether we've fired UserStartedSpeakingFrame for the current
        # utterance. Reset on stop so a new energetic batch re-arms the cycle.
        self._speaking = False
        # Per-turn lock: serialises announcements + tool flows that need to
        # happen atomically (e.g. service ping → speak → bridge.start).
        self._announce_lock = asyncio.Lock()

        self._bridge.set_on_remote_close(self._on_bridge_remote_close)
        # Remote `{"type":"say","text":...}` frames are turned into TTS just
        # like a Gemini reply — same Piper path, same AudioIO downlink.
        self._bridge.set_on_say_text(self.inject_assistant_text)

        # Build the LLM service first so we can wire tool handlers + scratchpad.
        self._llm = CustomGeminiLLMService(
            user_id=user_id,
            tools_schema=ALL_TOOLS,
            on_message_added=self._mirror_to_scratchpad,
        )
        self._register_tools()

        # Pipeline: SessionSource → BridgeGate → VoskSTT → LLM → TTS → AudioIOSink.
        self._source = SessionSource()
        self._pipeline = Pipeline([
            self._source,
            BridgeGateProcessor(self._bridge),
            VoskUtteranceSTTProcessor(user_id=user_id, sample_rate=UPLINK_SAMPLE_RATE),
            self._llm,
            PiperTTSProcessor(sample_rate=DOWNLINK_SAMPLE_RATE),
            AudioIOSinkProcessor(audio),
        ])
        # `allow_interruptions=True` is essential — without it Pipecat ignores
        # StartInterruptionFrame and our manual interrupt path becomes a no-op.
        self._task = PipelineTask(
            self._pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                audio_in_sample_rate=UPLINK_SAMPLE_RATE,
                audio_out_sample_rate=DOWNLINK_SAMPLE_RATE,
            ),
        )
        self._runner = PipelineRunner(handle_sigint=False)
        self._runner_task: Optional[asyncio.Task] = None

    # ----- public API used by endpoint.py / app/main.py ---------------------

    async def start(self) -> None:
        """Spawn the pipeline runner. Idempotent."""
        if self._runner_task is not None and not self._runner_task.done():
            return
        self._runner_task = asyncio.create_task(self._runner.run(self._task))

    async def feed_audio(self, pcm: bytes) -> None:
        """Push one audio batch into the pipeline.

        Fires `UserStartedSpeakingFrame` once per utterance (on first energetic
        batch) so the STT processor knows to start capturing.
        """
        if not pcm:
            return

        if not self._speaking:
            if rms_int16_le(pcm) >= self._min_rms:
                self._speaking = True
                await self._task.queue_frame(UserStartedSpeakingFrame())
            # Even silent leading frames are queued — the STT processor
            # captures audio bracketed by start/stop, so this preserves a small
            # pre-roll once `_speaking` flips.
        await self._task.queue_frame(
            InputAudioRawFrame(
                audio=pcm,
                sample_rate=UPLINK_SAMPLE_RATE,
                num_channels=1,
            )
        )

    async def signal_user_stopped(self) -> None:
        """End-of-utterance signal: silence timer fired or `turn_complete:true`."""
        if not self._speaking:
            # Nothing was captured this round; skip the spurious stop.
            return
        self._speaking = False
        await self._task.queue_frame(UserStoppedSpeakingFrame())

    async def interrupt(self) -> None:
        """User said stop / sent `{interrupt:true}`. Tear down bridge, clear playback."""
        if self._bridge.active:
            await self._bridge.close()
        self._speaking = False
        await self._task.queue_frame(InterruptionFrame())

    async def inject_assistant_text(self, text: str) -> bool:
        """Speak `text` to the user via TTS, bypassing STT and the LLM.

        Used by the agent WS endpoint (`/ws/developer/agent/{user_id}`) so an
        external service can push text into a live session and have the user
        hear it synthesized. Does NOT touch the LLMContext — agent text is a
        side channel; future Gemini turns won't see it. If the LLM is mid-
        response when this fires, the agent text queues behind it via Pipecat's
        normal frame ordering.

        Returns True if queued, False if the client is gone or `text` is empty.
        """
        if not text or not text.strip():
            return False
        if not self._alive():
            log.info("user_id=%s agent text dropped: client gone", self._user_id)
            return False
        await self._task.queue_frame(TTSSpeakFrame(text.strip()))
        return True

    async def on_service_ping(self, service_id: str = "") -> bool:
        """Service-initiated call: announce, dial bridge, ack or fail.

        Called by `developer_ping` HTTP route via `registry.get(user_id)`.
        """
        log.info(
            "user_id=%s service ping received service_id=%s",
            self._user_id, service_id or "unknown",
        )
        if self._bridge.active:
            log.info("user_id=%s bridge already active; ignoring ping", self._user_id)
            return True
        if not self._alive():
            log.info("user_id=%s client gone; dropping ping", self._user_id)
            return False
        async with self._announce_lock:
            self._llm.add_assistant_announcement(_SERVICE_PING_ANNOUNCE)
            await self._task.queue_frame(TTSSpeakFrame(_SERVICE_PING_ANNOUNCE))
            result = await self._bridge.start(REMOTE_BRIDGE_URL)
            log.info(
                "user_id=%s ping->bridge result ok=%s outcome=%s detail=%r service_id=%s",
                self._user_id, result.ok, result.outcome, result.detail,
                result.service_id or "?",
            )
            if result.ok and service_id and result.service_id and service_id != result.service_id:
                log.warning(
                    "user_id=%s service_id mismatch ping=%s ack=%s",
                    self._user_id, service_id, result.service_id,
                )
            if result.ok:
                self._llm.add_assistant_announcement(_BRIDGE_ACK)
                return True
            fail = _fail_message(result)
            self._llm.add_assistant_announcement(fail)
            await self._task.queue_frame(TTSSpeakFrame(fail))
            return False

    async def drain(self) -> None:
        """Final flush before close. Fires `signal_user_stopped` if there's an
        in-flight utterance, so any pending audio reaches Vosk before teardown.
        """
        try:
            if self._speaking:
                await asyncio.wait_for(self.signal_user_stopped(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        """Tear down the bridge + pipeline runner. Always called from finally."""
        try:
            await self._bridge.close()
        except Exception:
            log.exception("bridge close failed")
        try:
            await self._task.cancel(reason="session_close")
        except Exception:
            log.exception("pipeline task cancel failed")
        if self._runner_task is not None:
            try:
                await asyncio.wait_for(self._runner_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._runner_task.cancel()
            except Exception:
                pass
        try:
            await self._audio.shutdown_playback()
        except Exception:
            pass

    # ----- internals --------------------------------------------------------

    def _alive(self) -> bool:
        try:
            return self._ws.client_state == WebSocketState.CONNECTED
        except Exception:
            return False

    def _mirror_to_scratchpad(self, message: dict) -> None:
        """Tap on LLMContext message-added: mirror into the scratchpad for stdout dump."""
        role = (message.get("role") or "").lower()
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        if role == "user":
            self._scratchpad.add_user(content)
        elif role == "assistant":
            self._scratchpad.add_assistant(content)
        # "system" + "tool" roles are skipped — scratchpad only logged user/assistant before.

    def _register_tools(self) -> None:
        """Register tool handlers with the LLM service.

        Each handler implements the immediate-ack workaround:
          1. Push `TTSSpeakFrame` with our exact ack string (synthesised in
             parallel with the side effect).
          2. Run the side effect.
          3. Return via `result_callback(..., run_llm=False)` so the LLM does
             not generate a second response.
        """

        async def handle_start_bridge(params: FunctionCallParams) -> None:
            # 1. Deterministic ack starts speaking immediately.
            await params.llm.push_frame(TTSSpeakFrame(_BRIDGE_ACK))
            self._llm.add_assistant_announcement(_BRIDGE_ACK)

            # 2. Side effect.
            result = await self._bridge.start(REMOTE_BRIDGE_URL)
            log.info(
                "user_id=%s bridge start result ok=%s outcome=%s detail=%r",
                self._user_id, result.ok, result.outcome, result.detail,
            )

            # 3a. Failure → speak failure ack + record.
            if not result.ok:
                fail = _fail_message(result)
                await params.llm.push_frame(TTSSpeakFrame(fail))
                self._llm.add_assistant_announcement(fail)

            # 3b. Return to context with run_llm=False so no Gemini follow-up.
            await params.result_callback(
                {
                    "ok": result.ok,
                    "outcome": result.outcome,
                    "service_id": result.service_id,
                    "detail": result.detail,
                },
                properties=FunctionCallResultProperties(run_llm=False),
            )

        self._llm.register_function(
            START_REMOTE_AUDIO_BRIDGE,
            handle_start_bridge,
            cancel_on_interruption=True,
        )

    async def _on_bridge_remote_close(self) -> None:
        """Remote (not us) closed the bridge → speak the notice + record."""
        log.info("user_id=%s bridge remote-close notification", self._user_id)
        self._llm.add_assistant_announcement(_BRIDGE_DISCONNECT)
        if not self._alive():
            return
        try:
            await self._task.queue_frame(TTSSpeakFrame(_BRIDGE_DISCONNECT))
        except Exception:
            log.exception("user_id=%s on_bridge_remote_close speak failed", self._user_id)
