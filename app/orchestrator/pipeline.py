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

import array
import asyncio
import logging
import math
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    FunctionCallResultProperties,
    InputAudioRawFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
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
    ThinkingCueProcessor,
    VoskUtteranceSTTProcessor,
)
from .pipecat_llm import CustomGeminiLLMService, _DEFAULT_SYSTEM
from .scratchpad import Scratchpad
from .tools import ALL_TOOLS, END_CONVERSATION, FIND_AGENTS, MANAGE_TASK, ROUTE_TO_AGENT, pairs_to_dict
from .utterance import UtteranceBuffer

import agents_registry
from orchestrator.routing import embeddings, gate, policy
from orchestrator import speech
from orchestrator.tasks import protocol as proto
from orchestrator.routing.router import (
    DECISION_AMBIGUOUS,
    DECISION_NONE,
    DECISION_WRONG_MODE,
    AgentRecord,
    get_router,
)
from orchestrator.tasks.service import Announcement, get_service, what
from orchestrator.tasks.store import parse_ts

log = logging.getLogger("developer_ws")


# Rules appended to the system prompt whenever agents exist
# (ORCHESTRATOR_V2_TOOL_CALLS.md §2.6). Production overrides the base prompt via
# DEVELOPER_GEMINI_SYSTEM_INSTRUCTION, so these are appended, not merged.
_ROUTING_RULES = """
Routing and tasks. For each turn decide exactly one:
 ANSWER   general knowledge is enough -> reply, no tool. Never answer questions about the
          user's own data ("my ...", "have I ...") yourself: route them.
 ASK      a needed detail is missing or you're unsure what they said -> ONE short question.
          Never fill in a value the user didn't say.
 DECLINE  nothing can do it -> say so. Never claim something was done.
 ROUTE    call route_to_agent: mode_hint "connect" for live talk, "dispatch" for
          get-it-done-and-report, "auto" if unsure. Pass agent names exactly as heard.
 TASKS    use manage_task for status, changes, cancelling, completing, answering an agent's
          question, and for the user's yes/no to the orchestrator's questions ("Shall I go
          ahead?", "Want me to ...?", "Want me to put you through?") -> confirm / decline.
If the user corrects themselves ("no, Wednesday"), use the LAST value.
The orchestrator speaks tool outcomes itself; don't repeat or rephrase them.
Live-call state: "Connecting you to <agent> now." or "Getting <agent> for that..." means a live
call opened; "... disconnected. You're back with me now." means it closed.
Speech-to-text garbles names ("Kairos" may arrive as "cut in", "cairo's", "kai ross"); pass
what you heard, the orchestrator resolves it.
"""


def build_developer_system_instruction() -> str:
    """Base system prompt, plus routing rules and a bounded agent list.

    The base is the env override (`DEVELOPER_GEMINI_SYSTEM_INSTRUCTION`) or the
    default in `pipecat_llm`. The agent list is bounded (≤ DEVELOPER_PROMPT_AGENT_LIMIT
    agents; beyond that only a count and "use find_agents"), so prompt size no
    longer grows with the registry. Snapshotted once per session.
    """
    base = os.environ.get("DEVELOPER_GEMINI_SYSTEM_INSTRUCTION", "").strip() or _DEFAULT_SYSTEM
    try:
        limit = int(os.environ.get("DEVELOPER_PROMPT_AGENT_LIMIT", "30"))
    except ValueError:
        limit = 30
    try:
        summary = get_router().prompt_summary(limit=limit)
    except Exception:
        log.exception("could not load agents for system prompt; using base only")
        summary = ""
    if not summary:
        return base
    return f"{base}\n\n{_ROUTING_RULES.strip()}\n\n{summary}"


def _resolve_ping_url(service_id: str = "", user_id: str = "") -> str:
    """Bridge URL for a service-initiated ping: that service's URL, else the env default.

    Pings name their own service_id, so falling back to the env default only
    covers legacy single-endpoint setups. User-spoken names never fall back
    (decision A3).
    """
    url = agents_registry.resolve_bridge_url(service_id) if service_id else None
    return _expand_url(url or REMOTE_BRIDGE_URL, user_id)


def _expand_url(url: str, user_id: str) -> str:
    """Replace a literal ``{user_id}`` placeholder in a registered URL."""
    if user_id and "{user_id}" in url:
        url = url.replace("{user_id}", urllib.parse.quote(user_id, safe=""))
    return url


_BRIDGE_DISCONNECT = "The remote service disconnected. You're back with me now."
_SERVICE_PING_ANNOUNCE = "Your service wants to speak with you. Connecting you now."
_END_CONVERSATION_ACK = "Goodbye! Take care."
# Spoken the instant the socket opens so "connected" is unmistakable — the
# orchestrator actually says hello instead of only chiming. Override with
# DEVELOPER_WS_CONNECT_GREETING; set it to "" to disable the spoken greeting.
_CONNECT_GREETING = "You're connected. How can I help?"
_NEGATION_RE = re.compile(r"^\W*(no|nope|nah|wait|stop|cancel|not that|wrong|don'?t)\b", re.I)
_AFFIRM_RE = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|right|correct|go ahead|do it|please)\b", re.I)
_DROP_AT_DEADLINE_RE = re.compile(
    r"\b(forget it|drop it|cancel it|don'?t bother|never mind|no point|skip it)\b", re.I
)


def _env_float(name: str, default: float) -> float:
    """Parse a float env var, falling back to `default` on missing/garbage."""
    try:
        raw = os.environ.get(name)
        return float(raw) if raw is not None and raw.strip() != "" else default
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env var (0/false/no/off = False)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _build_ding_pcm(sample_rate: int = DOWNLINK_SAMPLE_RATE) -> bytes:
    """A short two-tone ascending chime (int16 mono @ DOWNLINK_SAMPLE_RATE).

    Played the instant a remote-bridge connect begins so the handoff to the
    remote agent is unmistakable. The total (0.12s + 0.14s = 260 ms) is NOT an
    exact multiple of the 40 ms downlink Opus frame, so it leaves a 20 ms
    residual; that residual is carried out by the trailing BotStoppedSpeakingFrame
    in `_ding_frames` → `AudioIO.mark_turn_complete` → `flush_residual`. (Contrast
    `_build_thinking_tick_pcm`, whose 120 ms IS frame-aligned and needs no flush.)
    """
    tones = ((784.0, 0.12), (1047.0, 0.14))  # G5 -> C6, a rising "connecting" cue
    amp = 0.28 * 32767
    fade = max(1, int(0.006 * sample_rate))  # 6 ms in/out fade to avoid clicks
    samples = array.array("h")
    for freq, dur in tones:
        n = int(dur * sample_rate)
        for i in range(n):
            gain = 1.0
            if i < fade:
                gain = i / fade
            elif i > n - fade:
                gain = max(0.0, (n - i) / fade)
            samples.append(
                int(amp * gain * math.sin(2.0 * math.pi * freq * i / sample_rate))
            )
    if sys.byteorder == "big":  # downlink PCM is int16 little-endian
        samples.byteswap()
    return samples.tobytes()


# Precomputed once at import; reused for every ding.
_DING_PCM = _build_ding_pcm()


def _build_thinking_tick_pcm(sample_rate: int = DOWNLINK_SAMPLE_RATE) -> bytes:
    """A single soft, low "thinking" pulse (int16 mono @ DOWNLINK_SAMPLE_RATE).

    Deliberately quieter and lower than the connect/handoff chime so a pulse
    repeating every ~1.5 s reads as an unobtrusive "still working" cue rather
    than an alert. One 120 ms tone == exactly 3 × 40 ms downlink Opus frames, so
    each independent tick self-aligns to the encoder and leaves no residual
    sub-frame carried into the next tick (the cue plays tick-by-tick with no
    mark_turn_complete between them). Short in/out fades avoid clicks. Frequency
    and gain are env-tunable (`DEVELOPER_WS_THINKING_FREQ_HZ`,
    `DEVELOPER_WS_THINKING_GAIN`) so "not too disruptive" can be dialed in.
    """
    freq = _env_float("DEVELOPER_WS_THINKING_FREQ_HZ", 660.0)
    gain = _env_float("DEVELOPER_WS_THINKING_GAIN", 0.13)
    amp = max(0.0, min(1.0, gain)) * 32767
    dur = 0.12  # 120 ms == 3 × 40 ms Opus frames (OPUS_FRAME_MS); zero residual
    fade = max(1, int(0.008 * sample_rate))  # 8 ms in/out fade
    n = int(dur * sample_rate)
    samples = array.array("h")
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = max(0.0, (n - i) / fade)
        samples.append(
            int(amp * env * math.sin(2.0 * math.pi * freq * i / sample_rate))
        )
    if sys.byteorder == "big":  # downlink PCM is int16 little-endian
        samples.byteswap()
    return samples.tobytes()


# Precomputed once at import; reused for every thinking pulse.
_THINKING_TICK_PCM = _build_thinking_tick_pcm()


def _ding_frames() -> list:
    """Frames that play the connect chime through the normal downlink path.

    A ``TTSAudioRawFrame`` carries the PCM to ``AudioIOSinkProcessor`` (which
    calls ``add_playback_pcm``); the trailing ``BotStoppedSpeakingFrame`` makes
    the sink ``mark_turn_complete`` so the turn state resets cleanly instead of
    leaving the pump armed. Pushed *after* an ack's ``TTSSpeakFrame`` at the same
    injection point, the FIFO Piper processor guarantees the ding lands right
    after the ack's audio — i.e. speech first, then ding.
    """
    return [
        TTSAudioRawFrame(audio=_DING_PCM, sample_rate=DOWNLINK_SAMPLE_RATE, num_channels=1),
        BotStoppedSpeakingFrame(),
    ]


def _fail_message(result: BridgeStartResult, agent_name: str = "The remote service") -> str:
    if result.outcome == OUTCOME_REJECTED:
        line = speech.declined_call(agent_name)
        return f"{line} Reason: {result.detail}." if result.detail else line
    return speech.unreachable(agent_name)


def _name_was_said(name: str, heard: str) -> bool:
    """Did the user say this agent name (allowing STT garbles and shortened names)?"""
    if gate.grounded(name, heard):
        return True
    heard_words = gate.words(heard)
    for w in gate.words(name):
        if len(w) >= 3 and any(h.startswith(w) or (len(h) >= 3 and w.startswith(h)) for h in heard_words):
            return True
    return False


@dataclass
class PendingAction:
    """Something waiting for the user's yes (a read-back, an offer, a confirm)."""

    kind: str  # dispatch | offer_route | dispatch_offer | connect_offer | cancel | delete | escalate
    data: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    def expired(self, ttl_s: float = 120.0) -> bool:
        return time.monotonic() - self.created_at > ttl_s


_ANSWER_HINT = (
    "The orchestrator checked: no agent is needed for the user's last message. "
    "Answer it yourself, briefly, without calling any tool."
)
_PRONOUN_REFS = {"", "that", "it", "this", "the last one", "the latest", "last one", "that one", "the task"}


class SpeechPipeline:
    """One per voice session.

    Constructed by `developer_websocket_endpoint` in endpoint.py.
    Driven by:
      - `feed_audio(pcm)`          : called for each non-bridge audio batch.
      - `signal_user_stopped()`    : silence-timer fire or `turn_complete:true`.
      - `interrupt()`              : user said stop or sent `{interrupt:true}`.
      - `on_service_ping(...)`     : HTTP ping route in app/main.py.
      - `close()`                  : final drain on socket close.

    Tool handlers implement ORCHESTRATOR_V2_TOOL_CALLS.md Part 1 (routing,
    connect vs dispatch, task management) with the validation gate of §2.4.
    Every spoken outcome is a fixed line from `orchestrator.speech`.
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

        # Spoken greeting on connect (empty string disables the spoken cue).
        self._connect_greeting = os.environ.get(
            "DEVELOPER_WS_CONNECT_GREETING", _CONNECT_GREETING
        ).strip()
        # Soft "thinking" pulse played while the reply is being produced.
        self._thinking_enabled = _env_flag("DEVELOPER_WS_THINKING_ENABLED", True)
        self._thinking_delay_s = _env_float("DEVELOPER_WS_THINKING_DELAY_SEC", 0.45)
        self._thinking_interval_s = _env_float("DEVELOPER_WS_THINKING_INTERVAL_SEC", 1.5)
        self._thinking_max_s = _env_float("DEVELOPER_WS_THINKING_MAX_SEC", 20.0)
        self._indirect_window_s = _env_float("ORCHESTRATOR_INDIRECT_CANCEL_WINDOW_S", 1.5)

        # Track whether we've fired UserStartedSpeakingFrame for the current
        # utterance. Reset on stop so a new energetic batch re-arms the cycle.
        self._speaking = False
        # Monotonic time the user last started speaking (or barged in). The
        # indirect-route cancel window watches it.
        self._user_spoke_at = 0.0
        # Per-turn lock: serialises announcements + tool flows that need to
        # happen atomically (e.g. service ping → speak → bridge.start).
        self._announce_lock = asyncio.Lock()

        # Orchestrator state for this session.
        self._service = get_service()
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._held: list[Announcement] = []       # task updates held during a live call (D2)
        self._pending: Optional[PendingAction] = None
        self._last_task_id: Optional[str] = None
        self._contexts: dict[str, str] = {}       # agent_id -> contextId for this user

        self._bridge.set_on_remote_close(self._on_bridge_remote_close)
        # Remote `{"type":"say","text":...}` frames are turned into TTS just
        # like a Gemini reply — same Piper path, same AudioIO downlink.
        self._bridge.set_on_say_text(self.inject_assistant_text)
        self._bridge.set_on_task_created(self._on_bridge_task_created)

        # Build the LLM service first so we can wire tool handlers + scratchpad.
        self._llm = CustomGeminiLLMService(
            user_id=user_id,
            system_instruction=build_developer_system_instruction(),
            on_message_added=self._mirror_to_scratchpad,
        )
        self._register_tools(ALL_TOOLS)

        # Pipeline: SessionSource → BridgeGate → VoskSTT → LLM → TTS →
        #           ThinkingCue → AudioIOSink.
        self._source = SessionSource()
        self._pipeline = Pipeline([
            self._source,
            BridgeGateProcessor(self._bridge),
            VoskUtteranceSTTProcessor(user_id=user_id, sample_rate=UPLINK_SAMPLE_RATE),
            self._llm,
            PiperTTSProcessor(sample_rate=DOWNLINK_SAMPLE_RATE),
            ThinkingCueProcessor(
                audio,
                tick_pcm=_THINKING_TICK_PCM,
                delay_s=self._thinking_delay_s,
                interval_s=self._thinking_interval_s,
                max_s=self._thinking_max_s,
                enabled=self._thinking_enabled,
            ),
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
        """Spawn the pipeline runner and subscribe to task updates. Idempotent."""
        if self._runner_task is not None and not self._runner_task.done():
            return
        self._runner_task = asyncio.create_task(self._runner.run(self._task))
        if self._unsubscribe is None:
            self._unsubscribe = self._service.subscribe(self._user_id, self._on_task_announcement)

    async def play_connect_greeting(self) -> None:
        """Speak a short greeting the instant the session opens, then any
        undelivered task results (ORCHESTRATOR_V2_TOOL_CALLS.md §1.6).

        The greeting is recorded as an assistant turn so the LLM knows it
        already greeted. Set `DEVELOPER_WS_CONNECT_GREETING=""` to disable it.
        """
        greeting = self._connect_greeting
        if greeting and self._alive():
            try:
                self._llm.add_assistant_announcement(greeting)
                await self._task.queue_frame(TTSSpeakFrame(greeting))
            except Exception:
                log.exception("user_id=%s connect greeting failed", self._user_id)
        await self._announce_session_start()

    async def _announce_session_start(self) -> None:
        try:
            items = await self._service.session_start(self._user_id)
        except Exception:
            log.exception("user_id=%s session-start task announcements failed", self._user_id)
            return
        for task_id, line in items:
            if not self._alive():
                return
            if task_id:
                self._last_task_id = task_id
            else:  # "You have N more task updates. Want to hear them?"
                self._pending = PendingAction("more_updates")
            self._llm.add_assistant_announcement(line)
            await self._task.queue_frame(TTSSpeakFrame(line))

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
                self._user_spoke_at = time.monotonic()
                await self._task.queue_frame(UserStartedSpeakingFrame())
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
            return
        self._speaking = False
        await self._task.queue_frame(UserStoppedSpeakingFrame())

    async def interrupt(self) -> None:
        """User said stop / sent `{interrupt:true}` / barged in. Tear down bridge, clear playback."""
        self._user_spoke_at = time.monotonic()
        was_bridged = self._bridge.active
        if was_bridged:
            await self._bridge.close()
        self._speaking = False
        self._llm.mark_user_interruption()
        await self._task.queue_frame(InterruptionFrame())
        if was_bridged:
            asyncio.create_task(self._flush_held())

    async def inject_assistant_text(self, text: str) -> bool:
        """Speak `text` to the user via TTS, bypassing STT and the LLM.

        Used for a live agent's `say` frames. The text is recorded in the LLM
        history as quoted agent output (so "change that booking" can be
        resolved), never as an instruction (ORCHESTRATOR_V2_TOOL_CALLS.md §2.4).
        """
        if not text or not text.strip():
            return False
        if not self._alive():
            log.info("user_id=%s agent text dropped: client gone", self._user_id)
            return False
        who = self._bridge.agent_name or "The agent"
        self._llm.add_assistant_announcement(f'[{who} said: "{text.strip()}"]')
        await self._task.queue_frame(TTSSpeakFrame(text.strip()))
        return True

    async def on_service_ping(self, service_id: str = "") -> bool:
        """Service-initiated call: announce, dial bridge, ack or fail."""
        log.info("user_id=%s service ping received service_id=%s", self._user_id, service_id or "unknown")
        if self._bridge.active:
            log.info("user_id=%s bridge already active; ignoring ping", self._user_id)
            return True
        if not self._alive():
            log.info("user_id=%s client gone; dropping ping", self._user_id)
            return False
        async with self._announce_lock:
            self._llm.add_assistant_announcement(_SERVICE_PING_ANNOUNCE)
            await self._task.queue_frame(TTSSpeakFrame(_SERVICE_PING_ANNOUNCE))
            agent = None
            if service_id:
                res = get_router().resolve(service_id, mode=proto.MODE_BRIDGE)
                agent = res.best if res.matched else None
            result = await self._bridge.start(
                _resolve_ping_url(service_id, user_id=self._user_id),
                context_id=self._context_for(agent) if agent else None,
                agent_id=agent.id if agent else "", agent_name=agent.name if agent else "",
            )
            log.info(
                "user_id=%s ping->bridge result ok=%s outcome=%s detail=%r service_id=%s",
                self._user_id, result.ok, result.outcome, result.detail, result.service_id or "?",
            )
            if result.ok:
                self._llm.add_assistant_announcement(
                    speech.connecting(agent.name if agent else "the remote service"))
                return True
            fail = _fail_message(result, agent.name if agent else "The remote service")
            self._llm.add_assistant_announcement(fail)
            await self._task.queue_frame(TTSSpeakFrame(fail))
            return False

    async def drain(self) -> None:
        """Final flush before close."""
        try:
            if self._speaking:
                await asyncio.wait_for(self.signal_user_stopped(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        """Tear down the bridge + pipeline runner. Always called from finally.

        Held task updates are not marked delivered, so they are announced at
        the next session.
        """
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        held, self._held = self._held, []
        for ann in held:
            try:
                await self._service.defer(ann)
            except Exception:
                log.exception("user_id=%s could not keep a held task update", self._user_id)
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
        role = (message.get("role") or "").lower()
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        if role == "user":
            self._scratchpad.add_user(content)
        elif role == "assistant":
            self._scratchpad.add_assistant(content)

    def _register_tools(self, tools: list[dict]) -> None:
        """Bind each schema in `tools` to its handler — both halves at once.

        Function-typed tools must have a handler; server-side tools
        (`google_search`) carry only a declaration.
        """
        handlers: dict[str, Callable[[FunctionCallParams], Awaitable[None]]] = {
            ROUTE_TO_AGENT: self._handle_route_to_agent,
            FIND_AGENTS: self._handle_find_agents,
            MANAGE_TASK: self._handle_manage_task,
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
            raise ValueError(f"Function tools declared with no handler: {sorted(missing_handler)}")
        if missing_schema:
            raise ValueError(f"Handlers with no declared function schema: {sorted(missing_schema)}")
        self._llm.set_tools_schema(tools)
        for name, handler in handlers.items():
            self._llm.register_function(name, handler, cancel_on_interruption=True)

    async def _say(self, params: Optional[FunctionCallParams], text: str) -> None:
        """Speak a fixed line and record it as an assistant turn."""
        if not text:
            return
        self._llm.add_assistant_announcement(text)
        if params is not None:
            await params.llm.push_frame(TTSSpeakFrame(text))
        elif self._alive():
            await self._task.queue_frame(TTSSpeakFrame(text))

    async def _finish(self, params: FunctionCallParams, result: dict) -> None:
        await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))

    def _context_for(self, agent: Optional[AgentRecord]) -> Optional[str]:
        if agent is None:
            return None
        if agent.id not in self._contexts:
            self._contexts[agent.id] = proto.new_context_id()
        return self._contexts[agent.id]

    def _turns(self) -> list[str]:
        return self._llm.recent_user_texts(3)

    # ----- route_to_agent -----------------------------------------------------

    async def _handle_route_to_agent(self, params: FunctionCallParams) -> None:
        args = dict(params.arguments or {})
        try:
            outcome = await self._route(params, args)
        except Exception:
            log.exception("user_id=%s route_to_agent failed", self._user_id)
            await self._say(params, speech.SOMETHING_WRONG_AGENT)
            outcome = {"outcome": "error"}
        await self._finish(params, outcome)

    async def _route(self, params: Optional[FunctionCallParams], args: dict) -> dict:
        agent_said = str(args.get("agent") or "").strip()
        intent = str(args.get("intent") or "").strip()
        turns = self._turns()
        current = turns[-1] if turns else ""
        router = get_router()
        # A route only counts as *named* if the user actually said the name.
        # Gemini sometimes fills `agent` itself for an unnamed request ("how many
        # calories have I had today" → agent="MyFitnessPal"); that is an
        # indirect route (cancel cue, failover allowed), with Gemini's pick as a hint.
        if agent_said and not _name_was_said(agent_said, " ".join(turns)):
            log.info("user_id=%s agent %r not in the user's words; treating as indirect", self._user_id, agent_said)
            res = router.resolve(agent_said, intent, mode=None)
            if not res.matched:
                emb = await embeddings.embed_query(intent or current)
                res = router.resolve("", intent or current, mode=None, query_embedding=emb)
            agent_said = ""
        elif agent_said:
            res = router.resolve(agent_said, intent, mode=None)
        else:
            emb = await embeddings.embed_query(intent or current)
            res = router.resolve("", intent or current, mode=None, query_embedding=emb)
        log.info(
            "user_id=%s route said=%r intent=%r decision=%s candidates=%s",
            self._user_id, agent_said, intent, res.decision,
            [(c.agent.name, round(c.score, 2)) for c in res.candidates[:3]],
        )
        if res.decision == DECISION_AMBIGUOUS:
            await self._say(params, speech.did_you_mean(res.names()))
            return {"outcome": "ambiguous", "candidates": res.names()}
        if res.decision in (DECISION_NONE, DECISION_WRONG_MODE) or res.best is None:
            if agent_said:
                await self._say(params, speech.unknown_agent(agent_said))
                return {"outcome": "unknown_agent"}
            weak = res.candidates[0] if res.candidates and res.candidates[0].score >= 0.45 else None
            if weak is not None:
                self._pending = PendingAction("offer_route", {"agent_id": weak.agent.id, "args": args})
                await self._say(params, speech.ask_check_agent(weak.agent.name))
                return {"outcome": "offered", "agent": weak.agent.name}
            await self._llm.answer_directly(_ANSWER_HINT)
            return {"outcome": "answered"}
        agent = res.best
        if not agent_said and not policy.indirect_route(agent, f"{intent} {current}"):
            await self._llm.answer_directly(_ANSWER_HINT)
            return {"outcome": "answered", "agent": agent.name}
        chain = [c.agent for c in res.candidates[1:3]] if not agent_said else []
        return await self._route_to(params, agent, args, named=bool(agent_said), chain=chain)

    async def _route_to(
        self,
        params: Optional[FunctionCallParams],
        agent: AgentRecord,
        args: dict,
        *,
        named: bool,
        chain: list[AgentRecord] = (),
        force_mode: Optional[str] = None,
    ) -> dict:
        intent = str(args.get("intent") or "").strip()
        slots = pairs_to_dict(args.get("slots"))
        turns = self._turns()
        current = turns[-1] if turns else ""
        missing = [s for s in agent.required_slots if s["name"] not in slots]
        if force_mode:
            decision, other = force_mode, None
        else:
            decision, other = policy.decide_mode(
                agent, str(args.get("mode_hint") or "auto"), f"{intent} {current}",
                missing_required=len(missing),
            )
        if decision == policy.WRONG_MODE:
            if other == policy.DISPATCH:
                self._pending = PendingAction("dispatch_offer", {"agent_id": agent.id, "args": args, "named": named})
                await self._say(params, speech.task_only(agent.name))
            elif other == policy.CONNECT:
                self._pending = PendingAction("connect_offer", {"agent_id": agent.id, "named": named})
                await self._say(params, speech.bridge_only(agent.name))
            else:
                await self._say(params, speech.NO_AGENT_FOR_REQUEST)
            return {"outcome": "wrong_mode", "agent": agent.name}
        if decision == policy.ASK:
            await self._say(params, speech.ASK_CONNECT_OR_DISPATCH)
            return {"outcome": "ask_mode", "agent": agent.name}
        if decision == policy.CONNECT:
            return await self._connect(params, agent, named=named)
        return await self._prepare_dispatch(params, agent, args, named=named, chain=chain)

    async def _prepare_dispatch(
        self,
        params: Optional[FunctionCallParams],
        agent: AgentRecord,
        args: dict,
        *,
        named: bool,
        chain: list[AgentRecord] = (),
    ) -> dict:
        intent = str(args.get("intent") or "").strip()
        slots = pairs_to_dict(args.get("slots"))
        turns = self._turns()
        current = turns[-1] if turns else ""
        deadline_raw = str(args.get("deadline") or "").strip()
        checked = dict(slots)
        if deadline_raw:
            checked["deadline"] = deadline_raw
        verdict = gate.check_slots(checked, turns, agent.slots)
        if not verdict.ok:
            log.info("user_id=%s gate blocked %s slot=%s", self._user_id, verdict.reason, verdict.slot)
            await self._say(params, verdict.question)
            return {"outcome": "ask", "slot": verdict.slot, "reason": verdict.reason}
        deadline = parse_ts(deadline_raw) if deadline_raw else None
        drop = bool(args.get("drop_at_deadline")) and bool(_DROP_AT_DEADLINE_RE.search(" ".join(turns)))
        request = {
            "agent_id": agent.id, "named": named, "intent": intent, "slots": slots,
            "notify": policy.resolve_notify(str(args.get("notify") or ""), current),
            "deadline_at": deadline.isoformat() if deadline else None, "drop_at_deadline": drop,
            "chain": [a.id for a in chain],
        }
        if agent.side_effects:
            self._pending = PendingAction("dispatch", request)
            await self._say(params, speech.read_back(gate.summarize_request(intent, agent.name)))
            return {"outcome": "read_back", "agent": agent.name}
        return await self._do_dispatch(params, request)

    async def _do_dispatch(self, params: Optional[FunctionCallParams], request: dict) -> dict:
        router = get_router()
        agent = router.get(request["agent_id"])
        if agent is None:
            await self._say(params, speech.NO_AGENT_FOR_REQUEST)
            return {"outcome": "no_agent"}
        out = await self._service.dispatch(
            user_id=self._user_id, agent=agent, intent=request["intent"], slots=request["slots"],
            notify=request["notify"], deadline_at=parse_ts(request.get("deadline_at")),
            drop_at_deadline=bool(request.get("drop_at_deadline")),
            context_id=self._context_for(agent),
            chain=[a for a in (router.get(i) for i in request.get("chain") or []) if a is not None],
            named=bool(request.get("named")),
        )
        if not out.ok:
            if out.reason == "user_limit":
                self._pending = PendingAction("pick_cancel")
                line = speech.TOO_MANY_TASKS
            else:
                line = speech.busy_named(agent.name) if request.get("named") else speech.BUSY
            await self._say(params, line)
            return {"outcome": out.reason}
        self._last_task_id = out.task["task_id"]
        await self._say(params, speech.dispatched(out.agent.name, will_tell=request["notify"] != "silent"))
        return {"outcome": "dispatched", "task_id": out.task["task_id"], "agent": out.agent.name}

    async def _connect(
        self,
        params: Optional[FunctionCallParams],
        agent: AgentRecord,
        *,
        named: bool,
        task_id: Optional[str] = None,
    ) -> dict:
        if self._bridge.active:
            await self._say(params, speech.ALREADY_CONNECTED)
            return {"outcome": "already_connected"}

        async def push(frame) -> None:
            if params is not None:
                await params.llm.push_frame(frame)
            else:
                await self._task.queue_frame(frame)

        if named:
            await self._say(params, speech.connecting(agent.name))
        else:
            await self._say(params, speech.connecting_indirect(agent.name))
            verdict = await self._indirect_cancel_window()
            if verdict == "no":
                await self._say(params, speech.INDIRECT_CANCELLED)
                return {"outcome": "cancelled_by_user", "agent": agent.name}
            if verdict == "other":
                # The user said something else: drop the dial, Gemini handles that turn.
                return {"outcome": "superseded", "agent": agent.name}
        for frame in _ding_frames():
            await push(frame)
        result = await self._bridge.start(
            _expand_url(agent.url, self._user_id), context_id=self._context_for(agent),
            task_id=task_id, agent_id=agent.id, agent_name=agent.name,
        )
        log.info(
            "user_id=%s bridge start agent=%s ok=%s outcome=%s detail=%r",
            self._user_id, agent.name, result.ok, result.outcome, result.detail,
        )
        router = get_router()
        if not result.ok:
            if result.outcome != OUTCOME_REJECTED:
                router.report_failure(agent.id, result.outcome)
            await self._say(params, _fail_message(result, agent.name))
            return {"outcome": "failed", "agent": agent.name, "detail": result.outcome}
        router.report_success(agent.id)
        return {"outcome": "connected", "agent": agent.name}

    async def _indirect_cancel_window(self) -> str:
        """M-b: listen during the indirect-route ack and the window after it.

        Returns "go" (silence, "yes", or noise with no words), "no" (the user
        said no) or "other" (the user said something else, which Gemini then
        handles). Speech is judged by its transcript, so a cough or a "yes"
        doesn't cancel the call.
        """
        marker = time.monotonic()
        loop = asyncio.get_running_loop()
        heard: dict[str, str] = {}
        got = asyncio.Event()

        def on_turn(text: str) -> bool:
            heard["text"] = text
            got.set()
            # Swallow a plain yes/no (answered here); let anything else reach Gemini.
            return bool(_NEGATION_RE.match(text) or _AFFIRM_RE.match(text))

        self._llm.set_turn_filter(on_turn)
        try:
            spoke = False
            start_by = loop.time() + 3.0
            while not self._audio.is_bot_audible() and loop.time() < start_by and not spoke:
                spoke = self._user_spoke_at > marker
                await asyncio.sleep(0.05)
            done_by = loop.time() + 10.0
            guard_s = _env_float("DEVELOPER_WS_ECHO_GUARD_SEC", 0.5)
            guard = getattr(self._audio, "output_guard_active", None)
            # Wait out the ack *and* the self-echo guard: the mic is ignored until
            # then, so the user's "no" could not be heard yet.
            while not spoke and loop.time() < done_by and (
                self._audio.is_bot_audible() or (guard is not None and guard(guard_s))
            ):
                spoke = self._user_spoke_at > marker
                await asyncio.sleep(0.05)
            end = loop.time() + self._indirect_window_s
            while not spoke and loop.time() < end:
                spoke = self._user_spoke_at > marker
                await asyncio.sleep(0.05)
            if not spoke and not got.is_set():
                self._llm.set_turn_filter(None)
                return "go"
            # The user spoke: wait for the words (endpointing + STT).
            try:
                await asyncio.wait_for(got.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._llm.set_turn_filter(None)
                return "go"  # noise, no words
            text = heard.get("text", "")
            if _NEGATION_RE.match(text):
                return "no"
            if _AFFIRM_RE.match(text):
                return "go"
            return "other"
        except asyncio.CancelledError:
            # A barge-in cancelled this handler mid-window: a following "no" is
            # answered here; anything else goes to Gemini as a normal turn.
            def after_barge_in(text: str) -> bool:
                if _NEGATION_RE.match(text):
                    asyncio.get_running_loop().create_task(self._say(None, speech.INDIRECT_CANCELLED))
                    return True
                return False

            self._llm.set_turn_filter(after_barge_in)
            raise

    # ----- find_agents --------------------------------------------------------

    async def _handle_find_agents(self, params: FunctionCallParams) -> None:
        query = str((params.arguments or {}).get("query") or "").strip()
        router = get_router()
        emb = await embeddings.embed_query(query)
        found = router.search(query, k=3, query_embedding=emb)
        pairs = [(c.agent.name, c.agent.description.split(".")[0][:90].strip()) for c in found[:3]]
        await self._say(params, speech.found_agents(pairs))
        await self._finish(params, {"agents": [p[0] for p in pairs]})

    # ----- manage_task --------------------------------------------------------

    async def _handle_manage_task(self, params: FunctionCallParams) -> None:
        args = dict(params.arguments or {})
        try:
            outcome = await self._manage(params, args)
        except Exception:
            log.exception("user_id=%s manage_task failed", self._user_id)
            await self._say(params, speech.SOMETHING_WRONG_TASK)
            outcome = {"outcome": "error"}
        await self._finish(params, outcome)

    async def _manage(self, params: Optional[FunctionCallParams], args: dict) -> dict:
        action = str(args.get("action") or "").strip().lower()
        if action in ("confirm", "decline"):
            return await self._resolve_pending(params, action == "confirm")
        tasks = await self._service.list(self._user_id, limit=20)
        if action == "status" and not str(args.get("task_ref") or "").strip():
            active = [t for t in tasks if t["status"] in proto.DB_ACTIVE]
            if len(active) > 1:
                lines = [self._service.status_line(t) for t in active[:3]]
                await self._say(params, " ".join(lines))
                return {"outcome": "status", "count": len(active)}
        task, problem = self._resolve_task(str(args.get("task_ref") or ""), tasks, action)
        if task is None:
            await self._say(params, problem)
            return {"outcome": "no_task"}
        self._last_task_id = task["task_id"]
        agent = get_router().get(task.get("agent_id") or "")
        name = agent.name if agent else ((task.get("task_info") or {}).get("agent_name") or "The agent")
        if action == "status":
            if (task.get("task_info") or {}).get("stalled_since"):
                await self._service.probe(task)  # one attempt to reach a silent agent
            await self._say(params, self._service.status_line(task))
            return {"outcome": "status", "status": task["status"]}
        if action == "update":
            changes = pairs_to_dict(args.get("changes"))
            if args.get("notify"):
                notify = policy.resolve_notify(str(args["notify"]), " ".join(self._turns()))
                await asyncio.to_thread(lambda: self._service.store.update_task(task["task_id"], notify=notify))
            if not changes:
                await self._say(params, speech.OKAY if args.get("notify") else speech.ASK_WHAT_TO_CHANGE)
                return {"outcome": "updated_notify" if args.get("notify") else "ask"}
            verdict = gate.check_slots(changes, self._turns())
            if not verdict.ok:
                await self._say(params, verdict.question)
                return {"outcome": "ask", "slot": verdict.slot}
            if not self._service.can_update_in_place(task) and (agent is None or agent.side_effects):
                # The change means cancelling and re-sending the task: read it back first.
                summary = ", ".join(f"{k} {v}" for k, v in changes.items())
                self._pending = PendingAction("recreate", {"task_id": task["task_id"], "changes": changes})
                await self._say(params, speech.read_back(f"Change {what(task)} to {summary}"))
                return {"outcome": "read_back"}
            await self._say(params, await self._service.update(task, changes))
            return {"outcome": "updated"}
        if action in ("cancel", "delete"):
            risky = task["status"] in (proto.DB_RUNNING, proto.DB_INPUT_REQUIRED) and (agent is None or agent.side_effects)
            if risky:
                self._pending = PendingAction(action, {"task_id": task["task_id"]})
                confirm = speech.cancel_confirm if action == "cancel" else speech.delete_confirm
                await self._say(params, confirm(what(task)))
                return {"outcome": "confirm", "action": action}
            line = await (self._service.cancel(task) if action == "cancel" else self._service.delete(task))
            await self._say(params, line)
            return {"outcome": action}
        if action == "complete":
            await self._say(params, await self._service.complete(task))
            return {"outcome": "complete"}
        if action == "answer":
            answer = str(args.get("answer") or "").strip()
            if not answer:
                await self._say(params, speech.ask_for_answer(name))
                return {"outcome": "ask"}
            await self._say(params, await self._service.answer(task, answer))
            return {"outcome": "answered"}
        await self._say(params, speech.NO_SUCH_TASK)
        return {"outcome": "unknown_action"}

    def _resolve_task(self, ref: str, tasks: list[dict], action: str) -> tuple[Optional[dict], str]:
        """Resolve "that" / "the dinner booking" to one task, or explain why not (§1.5)."""
        if not tasks:
            return None, speech.NO_TASKS
        prefer_active = action in ("status", "update", "cancel", "answer")
        pool = [t for t in tasks if t["status"] in proto.DB_ACTIVE] if prefer_active else list(tasks)
        if not pool:
            pool = list(tasks)
        ref_n = ref.strip().lower()
        if ref_n in _PRONOUN_REFS:
            if action == "answer":
                waiting = [t for t in pool if t["status"] == proto.DB_INPUT_REQUIRED
                           or (t.get("task_info") or {}).get("pending_input")]
                if waiting:
                    return waiting[0], ""
            for t in pool:
                if t["task_id"] == self._last_task_id:
                    return t, ""
            return pool[0], ""
        ref_words = {w for w in gate.content_words(ref_n) if w not in ("task", "one", "request", "thing")}
        if not ref_words:
            return pool[0], ""

        def score(t: dict) -> int:
            info = t.get("task_info") or {}
            hay = f"{info.get('intent') or ''} {info.get('agent_name') or ''} " + " ".join(
                str(v) for v in (info.get("slots") or {}).values())
            hw = set(gate.words(hay))
            keys = {gate.phonetic_key(w) for w in hw if len(w) > 2}
            return sum(1 for w in ref_words if w in hw or gate.phonetic_key(w) in keys
                       or any(w[:4] == h[:4] for h in hw if len(w) > 3 and len(h) > 3))

        scored = sorted(((score(t), t) for t in pool), key=lambda x: -x[0])
        if not scored or scored[0][0] == 0:
            return None, speech.NO_SUCH_TASK
        best = [t for s, t in scored if s == scored[0][0]]
        if len(best) > 1:
            descs = [str((t.get("task_info") or {}).get("intent") or "task").rstrip(".") for t in best[:3]]
            return None, speech.which_task(descs)
        return best[0], ""

    async def _resolve_pending(self, params: Optional[FunctionCallParams], yes: bool) -> dict:
        pending, self._pending = self._pending, None
        if pending is None or pending.expired():
            await self._say(params, speech.NO_PENDING_ACTION)
            return {"outcome": "nothing_pending"}
        router = get_router()
        data = pending.data
        if not yes:
            if pending.kind == "retry_no_deadline":
                task = await self._service.get(data.get("task_id") or "")
                if task is not None:
                    await self._service.cancel(task, quiet=True)
            await self._say(params, speech.ACTION_DECLINED)
            return {"outcome": "declined", "kind": pending.kind}
        if pending.kind == "more_updates":
            await self._announce_session_start()
            return {"outcome": "more_updates"}
        if pending.kind == "pick_cancel":
            active = [t for t in await self._service.list(self._user_id, active_only=True)]
            if not active:
                await self._say(params, speech.NO_TASKS)
                return {"outcome": "no_task"}
            await self._say(params, speech.which_to_cancel([self._service.status_line(t) for t in active[:3]]))
            return {"outcome": "ask_which"}
        if pending.kind in ("cancel_now", "retry_no_deadline", "recreate"):
            task = await self._service.get(data.get("task_id") or "")
            if task is None:
                await self._say(params, speech.NO_SUCH_TASK)
                return {"outcome": "no_task"}
            if pending.kind == "cancel_now":
                line = await self._service.cancel(task)
            elif pending.kind == "retry_no_deadline":
                line = await self._service.retry_without_deadline(task)
            else:
                line = await self._service.update(task, data.get("changes") or {})
            await self._say(params, line)
            return {"outcome": pending.kind}
        agent = router.get(data.get("agent_id") or "") if data.get("agent_id") else None
        if pending.kind == "dispatch":
            return await self._do_dispatch(params, data)
        if pending.kind == "offer_route" and agent is not None:
            return await self._route_to(params, agent, data.get("args") or {}, named=True)
        if pending.kind == "dispatch_offer" and agent is not None:
            return await self._prepare_dispatch(params, agent, data.get("args") or {}, named=bool(data.get("named")))
        if pending.kind == "connect_offer" and agent is not None:
            return await self._connect(params, agent, named=True)
        if pending.kind == "escalate" and agent is not None:
            task = await self._service.get(data.get("task_id") or "")
            if task is not None:
                await self._service.escalate(task)
            return await self._connect(params, agent, named=True, task_id=data.get("task_id"))
        if pending.kind in ("cancel", "delete"):
            task = await self._service.get(data.get("task_id") or "")
            if task is None:
                await self._say(params, speech.NO_SUCH_TASK)
                return {"outcome": "no_task"}
            line = await (self._service.cancel(task) if pending.kind == "cancel" else self._service.delete(task))
            await self._say(params, line)
            return {"outcome": pending.kind}
        await self._say(params, speech.NO_PENDING_ACTION)
        return {"outcome": "nothing_pending"}

    # ----- task announcements (from TaskService) ------------------------------

    async def _on_task_announcement(self, ann: Announcement) -> bool:
        """Deliver a task update to this live session (held during a live call)."""
        if not self._alive():
            return False
        if ann.escalate_agent_id:
            self._pending = PendingAction("escalate", {"task_id": ann.task_id, "agent_id": ann.escalate_agent_id})
        elif ann.offer == "cancel":
            self._pending = PendingAction("cancel_now", {"task_id": ann.task_id})
        elif ann.offer == "retry_without_deadline":
            self._pending = PendingAction("retry_no_deadline", {"task_id": ann.task_id})
        if ann.kind == "question":
            self._last_task_id = ann.task_id
        if self._bridge.active:
            self._held.append(ann)
            log.info("user_id=%s holding task update during live call task_id=%s", self._user_id, ann.task_id)
            return True
        await self._speak_announcement(ann)
        return True

    async def _speak_announcement(self, ann: Announcement) -> None:
        async with self._announce_lock:
            self._llm.add_assistant_announcement(ann.line)
            await self._task.queue_frame(TTSSpeakFrame(ann.line))
        await self._service.mark_delivered(ann.task_id, proto.DELIVERED_LIVE)

    async def _flush_held(self) -> None:
        held, self._held = self._held, []
        for ann in held:
            if not self._alive():
                return
            await self._speak_announcement(ann)

    async def _on_bridge_task_created(self, body: dict, context_id: Optional[str]) -> dict:
        agent = get_router().get(self._bridge.agent_id) if self._bridge.agent_id else None
        if agent is None:
            raise RuntimeError("unknown agent for this live call")
        reply = await self._service.adopt(agent, self._user_id, context_id, body)
        self._last_task_id = reply.get("task_id")
        return reply

    # ----- end_conversation ---------------------------------------------------

    async def _handle_end_conversation(self, params: FunctionCallParams) -> None:
        """Speak a goodbye, return run_llm=False, then close the socket once it's heard."""
        reason = str(params.arguments.get("reason") or "").strip() or "user_wrapup"
        log.info("user_id=%s end_conversation tool fired reason=%r", self._user_id, reason)
        await params.llm.push_frame(TTSSpeakFrame(_END_CONVERSATION_ACK))
        self._llm.add_assistant_announcement(_END_CONVERSATION_ACK)
        await params.result_callback(
            {"closed": True, "reason": reason},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        asyncio.create_task(self._close_session_after_speak(_END_CONVERSATION_ACK))

    async def _close_session_after_speak(self, text: str) -> None:
        """Wait for the goodbye (and any in-flight audio) to play, then close the WebSocket.

        Timing is measured via ``AudioIO.is_bot_audible()`` so we never chop a
        goodbye mid-word. The receive loop in ``endpoint.py`` runs teardown.
        """
        loop = asyncio.get_running_loop()
        await asyncio.sleep(0.3)
        start_deadline = loop.time() + 5.0
        while not self._audio.is_bot_audible() and loop.time() < start_deadline:
            await asyncio.sleep(0.05)
        drain_deadline = loop.time() + 12.0
        while self._audio.is_bot_audible() and loop.time() < drain_deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        if not self._alive():
            return
        try:
            await self._ws.close(code=1000, reason="conversation ended")
            log.info("user_id=%s end_conversation closed ws", self._user_id)
        except Exception:
            log.exception("user_id=%s end_conversation ws close failed", self._user_id)

    async def _on_bridge_remote_close(self) -> None:
        """Remote (not us) closed the bridge → speak the notice, then held task updates."""
        log.info("user_id=%s bridge remote-close notification", self._user_id)
        self._llm.add_assistant_announcement(_BRIDGE_DISCONNECT)
        if not self._alive():
            return
        try:
            await self._task.queue_frame(TTSSpeakFrame(_BRIDGE_DISCONNECT))
        except Exception:
            log.exception("user_id=%s on_bridge_remote_close speak failed", self._user_id)
        await self._flush_held()
