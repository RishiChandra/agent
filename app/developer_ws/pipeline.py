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
import urllib.parse
from typing import Awaitable, Callable, Optional

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
from .pipecat_llm import CustomGeminiLLMService, _DEFAULT_SYSTEM
from .scratchpad import Scratchpad
from .tools import ALL_TOOLS, END_CONVERSATION, START_REMOTE_AUDIO_BRIDGE
from .utterance import UtteranceBuffer

import agents_registry

log = logging.getLogger("developer_ws")


def build_developer_system_instruction() -> str:
    """Base system prompt plus the current list of registered agents.

    The base is the env override (`DEVELOPER_GEMINI_SYSTEM_INSTRUCTION`) or the
    default in `pipecat_llm`. We append the active agents so Gemini knows which
    names it may pass as the `agent` argument to `start_remote_audio_bridge`.
    Snapshotted once per session (at pipeline construction); agents registered
    mid-call are picked up on the next session.
    """
    base = os.environ.get("DEVELOPER_GEMINI_SYSTEM_INSTRUCTION", "").strip() or _DEFAULT_SYSTEM
    try:
        agents = agents_registry.list_agents(active_only=True)
    except Exception:
        log.exception("could not load agents for system prompt; using base only")
        agents = []
    if not agents:
        return base
    lines = [
        "\n\nRegistered agents you can bridge to via start_remote_audio_bridge "
        "(pass the exact name as the `agent` argument):",
    ]
    for a in agents:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        desc = (a.get("description") or "").strip()
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    lines.append(
        "When the user asks to reach one of these by name, pass that name as `agent`. "
        "If they just say 'call the service' without naming one, omit `agent`. "
        "IMPORTANT: the transcript comes from imperfect speech-to-text that garbles "
        "names not in its vocabulary — e.g. 'Kairos' may arrive as 'cut in', 'cairo's', "
        "or 'kai ross'. If any part of the request sounds like one of the registered "
        "agent names above, or the described purpose matches one agent's description, "
        "treat it as that agent and pass the REGISTERED name (never the garbled text) "
        "as `agent`."
    )
    return base + "\n".join(lines)


def _resolve_bridge_url(selector: str = "", user_id: str = "") -> str:
    """Map an agent name/service_id to its bridge URL, or the env default.

    Falls back to `REMOTE_BRIDGE_URL` when the selector is empty or matches no
    registered agent, preserving the original single-endpoint behaviour.

    Registered URLs may contain a literal ``{user_id}`` placeholder (e.g.
    ``wss://host/ws/{user_id}``); it is replaced with the caller's user id so
    the remote session runs as the real user.
    """
    sel = (selector or "").strip()
    url = ""
    if sel:
        url = agents_registry.resolve_bridge_url(sel) or ""
        if url:
            log.info("bridge target resolved selector=%r -> %s", sel, url)
        else:
            log.info("bridge target selector=%r unmatched; using default", sel)
    if not url:
        url = REMOTE_BRIDGE_URL
    if user_id and "{user_id}" in url:
        url = url.replace("{user_id}", urllib.parse.quote(user_id, safe=""))
    return url

_BRIDGE_ACK = "Connecting you to the remote service now."
_BRIDGE_FAIL_GENERIC = "Sorry, I couldn't open the remote connection."
_BRIDGE_FAIL_NO_PICKUP = "The remote service didn't pick up."
_BRIDGE_FAIL_REJECTED = "The remote service declined the call."
_BRIDGE_DISCONNECT = "The remote service disconnected. You're back with me now."
_SERVICE_PING_ANNOUNCE = "Your service wants to speak with you. Connecting you now."
_END_CONVERSATION_ACK = "Goodbye! Take care."


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
        # `_register_tools(ALL_TOOLS)` owns both halves of tool binding (schemas
        # sent to Gemini + Python handlers dispatched on function-call frames),
        # so we don't pass `tools_schema=` here.
        self._llm = CustomGeminiLLMService(
            user_id=user_id,
            system_instruction=build_developer_system_instruction(),
            on_message_added=self._mirror_to_scratchpad,
        )
        self._register_tools(ALL_TOOLS)

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
            # A ping names its service_id; dial that agent's registered URL if we
            # have one, else the env default.
            result = await self._bridge.start(
                _resolve_bridge_url(service_id, user_id=self._user_id)
            )
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

    def _register_tools(self, tools: list[dict]) -> None:
        """Bind each schema in `tools` to its handler — both halves at once.

        Tool wiring has two distinct surfaces:
          1. **Schemas** go up to Gemini in `generateContent` so it knows what
             tools exist (name, description, parameter shape). Set on the LLM
             service via `set_tools_schema(...)`.
          2. **Handlers** stay in our process and run when Gemini emits a
             function call. Registered on the LLM service via
             `register_function(name, handler, ...)`.

        Not every tool needs a handler — server-handled tools (currently
        `google_search`, Gemini's built-in grounding) carry only a declaration
        and are executed inside Gemini's runtime. The fail-fast check below
        only validates that **function-typed** tools have matching handlers.
        """
        handlers: dict[str, Callable[[FunctionCallParams], Awaitable[None]]] = {
            START_REMOTE_AUDIO_BRIDGE: self._handle_start_remote_audio_bridge,
            END_CONVERSATION: self._handle_end_conversation,
        }

        function_tool_names = {
            tool["function"]["name"]
            for tool in tools
            if tool.get("type") == "function" and "function" in tool
        }
        missing_handler = function_tool_names - handlers.keys()
        missing_schema = handlers.keys() - function_tool_names
        if missing_handler:
            raise ValueError(
                f"Function tools declared with no handler in _register_tools: "
                f"{sorted(missing_handler)}"
            )
        if missing_schema:
            raise ValueError(
                f"Handlers in _register_tools with no declared function schema: "
                f"{sorted(missing_schema)}"
            )

        self._llm.set_tools_schema(tools)
        for name, handler in handlers.items():
            self._llm.register_function(name, handler, cancel_on_interruption=True)

    async def _handle_start_remote_audio_bridge(self, params: FunctionCallParams) -> None:
        """Handler for the `start_remote_audio_bridge` Gemini tool.

        Implements the immediate-ack workaround:
          1. Push `TTSSpeakFrame` with our exact ack — synthesised in parallel
             with the side effect; no LLM roundtrip for the ack text.
          2. Run the side effect (dial the remote bridge).
          3. On failure, push a second `TTSSpeakFrame` with the failure ack.
          4. Return via `result_callback(..., run_llm=False)` so Gemini does
             not generate a follow-up response after the tool result lands in
             context.
        """
        # 1. Deterministic ack starts speaking immediately.
        await params.llm.push_frame(TTSSpeakFrame(_BRIDGE_ACK))
        self._llm.add_assistant_announcement(_BRIDGE_ACK)

        # 2. Side effect — dial the agent the user named, or the env default.
        agent_selector = str(params.arguments.get("agent") or "").strip()
        result = await self._bridge.start(
            _resolve_bridge_url(agent_selector, user_id=self._user_id)
        )
        log.info(
            "user_id=%s bridge start result ok=%s outcome=%s detail=%r",
            self._user_id, result.ok, result.outcome, result.detail,
        )

        # 3. Failure → speak failure ack + record.
        if not result.ok:
            fail = _fail_message(result)
            await params.llm.push_frame(TTSSpeakFrame(fail))
            self._llm.add_assistant_announcement(fail)

        # 4. Return to context with run_llm=False so no Gemini follow-up.
        await params.result_callback(
            {
                "ok": result.ok,
                "outcome": result.outcome,
                "service_id": result.service_id,
                "detail": result.detail,
            },
            properties=FunctionCallResultProperties(run_llm=False),
        )

    async def _handle_end_conversation(self, params: FunctionCallParams) -> None:
        """Handler for the `end_conversation` Gemini tool.

        The tool description in ``tools.py`` is intentionally permissive about
        which user phrasings should trigger this — Gemini infers intent
        semantically so we don't have to maintain a list of goodbye patterns
        in code.

        Sequence:
          1. Push a brief goodbye via ``TTSSpeakFrame`` so the user hears it
             synthesised in parallel with the close-out.
          2. Return ``run_llm=False`` so Gemini doesn't try to follow up.
          3. Spawn a background task that waits for AudioIO to drain, then
             closes the WebSocket with code 1000. The endpoint's receive
             loop catches the close and runs the normal teardown path
             (``_drain_on_close`` → ``pipeline.close()``).
        """
        reason = str(params.arguments.get("reason") or "").strip() or "user_wrapup"
        log.info(
            "user_id=%s end_conversation tool fired reason=%r",
            self._user_id, reason,
        )
        await params.llm.push_frame(TTSSpeakFrame(_END_CONVERSATION_ACK))
        self._llm.add_assistant_announcement(_END_CONVERSATION_ACK)

        await params.result_callback(
            {"closed": True, "reason": reason},
            properties=FunctionCallResultProperties(run_llm=False),
        )

        asyncio.create_task(self._close_session_after_speak(_END_CONVERSATION_ACK))

    async def _close_session_after_speak(self, text: str) -> None:
        """Wait for the goodbye TTS to play, then close the WebSocket.

        Timing is estimated, not measured: a short initial delay lets the
        ``TTSSpeakFrame`` enter the pipeline, then we sleep proportional to
        word count so playback has time to land at the client, then close.
        The receive loop in ``endpoint.py`` catches the close and runs the
        full session teardown.
        """
        # Initial beat for the frame to enter the pipeline + Piper to start.
        await asyncio.sleep(0.4)
        # ~400ms/word at average TTS rate + a 0.5s tail to flush AudioIO's coalesce.
        word_count = max(1, len(text.split()))
        synthesis_and_playback = min(0.4 * word_count + 0.5, 8.0)
        await asyncio.sleep(synthesis_and_playback)
        if not self._alive():
            return
        try:
            await self._ws.close(code=1000, reason="conversation ended")
            log.info("user_id=%s end_conversation closed ws", self._user_id)
        except Exception:
            log.exception(
                "user_id=%s end_conversation ws close failed", self._user_id,
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
