"""Single-flight orchestration for one voice session.

`SpeechPipeline.flush()` runs STT → Gemini → TTS (or a tool dispatch) inside an asyncio
lock so only one turn is in flight at a time; new flushes queue behind in-flight ones.
Also handles tool calls (currently just `start_remote_audio_bridge`), service-initiated
pings (`on_service_ping`), and bridge remote-close notifications (TTS announcement +
scratchpad entry).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from audio_codec import UPLINK_SAMPLE_RATE, rms_int16_le

from .audio_io import AudioIO
from .bridge import (
    OUTCOME_NO_PICKUP,
    OUTCOME_REJECTED,
    REMOTE_BRIDGE_URL,
    BridgeStartResult,
    RemoteAudioBridge,
)
from .llm import GeminiReply, gemini_reply
from .scratchpad import Scratchpad
from .stt import transcribe_pcm16
from .tools import ALL_TOOLS, START_REMOTE_AUDIO_BRIDGE
from .tts import synthesize_speech_pcm24
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

    Constructed by: `developer_websocket_endpoint` in endpoint.py.
    Reached by:
      - `flush()` / `schedule_flush()` from the silence timer (`utterance.arm_timer`)
        or directly from `_receive_loop` on a `turn_complete:true` frame.
      - `on_service_ping(...)` from the HTTP ping route in `app/main.py` (looked up
        via `registry.get(user_id)`).
      - `_on_bridge_remote_close()` from the bridge's `_recv_loop` when the remote
        side closes; wired in `__init__` via `bridge.set_on_remote_close`.
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
        # At most one STT→LLM→TTS turn at a time; new flushes wait their turn.
        self._lock = asyncio.Lock()
        self._min_rms = float(os.environ.get("DEVELOPER_WS_MIN_INPUT_RMS", "20"))
        # Notify the user (TTS + scratchpad) when the remote side closes the bridge.
        self._bridge.set_on_remote_close(self._on_bridge_remote_close)

    def _alive(self) -> bool:
        try:
            return self._ws.client_state == WebSocketState.CONNECTED
        except Exception:
            return False

    async def _abort(self, label: str) -> None:
        log.info("client gone after %s; skipping rest user_id=%s", label, self._user_id)
        await self._audio.shutdown_playback()

    def _is_low_energy(self, pcm: bytes) -> bool:
        # Below ~50 ms there is nothing to transcribe; very short buffers are passed through.
        duration_s = len(pcm) / (2 * UPLINK_SAMPLE_RATE)
        return duration_s >= 0.05 and rms_int16_le(pcm) < self._min_rms

    async def flush(self) -> None:
        """Run one full turn: snapshot utterance → STT → LLM → TTS (or tool dispatch).

        Called by:
          - `utterance.arm_timer(self.flush)` — silence timer fires after a quiet gap.
          - `schedule_flush()` — wraps this in a task for fire-and-forget callers.
          - `_drain_on_close` in endpoint.py — final flush during shutdown (45s cap).
        Calls (in order, with early returns at every checkpoint):
          - `utterance.snapshot_and_clear()`
          - `stt.transcribe_pcm16(pcm, sr)`
          - `scratchpad.history_messages()` + `scratchpad.add_user(text)`
          - `llm.gemini_reply(text, history, tools)`
          - either `_handle_tool_call(reply)` or `scratchpad.add_assistant(...)` + `_speak(...)`
        Holds `self._lock` for the full turn so concurrent flushes serialise.
        """
        async with self._lock:
            pcm16 = await self._utterance.snapshot_and_clear()
            if not pcm16:
                return

            if self._is_low_energy(pcm16):
                log.info(
                    "skip flush (low energy) user_id=%s pcm=%dB rms=%d",
                    self._user_id, len(pcm16), rms_int16_le(pcm16),
                )
                self._audio.mark_turn_complete()
                return

            text = await transcribe_pcm16(pcm16, UPLINK_SAMPLE_RATE)
            log.info(
                "user_id=%s transcript=%r (pcm=%dB ~%.2fs)",
                self._user_id, text, len(pcm16), len(pcm16) / (2 * UPLINK_SAMPLE_RATE),
            )
            if not text.strip():
                self._audio.mark_turn_complete()
                return
            # Snapshot history BEFORE recording the new user turn — Gemini receives prior
            # turns as context, then the current transcript is appended inside gemini_reply.
            history = self._scratchpad.history_messages()
            self._scratchpad.add_user(text)
            if not self._alive():
                await self._abort("STT")
                return

            reply = await gemini_reply(text, history=history, tools=ALL_TOOLS)
            log.info("user_id=%s gemini_reply=%r", self._user_id, reply)

            if reply.tool_call is not None:
                await self._handle_tool_call(reply)
                return

            if not reply.text.strip():
                self._audio.mark_turn_complete()
                return
            self._scratchpad.add_assistant(reply.text)
            if not self._alive():
                await self._abort("Gemini")
                return

            await self._speak(reply.text)

    async def _handle_tool_call(self, reply: GeminiReply) -> None:
        """Dispatch a Gemini tool call. Currently only `start_remote_audio_bridge`.

        Called by: `flush()` when `reply.tool_call` is set.
        Calls: `bridge.start(...)` → `_speak(ack)`. The ack text is chosen by
        `_fail_message(result)` for failures, or the bridge-open string for success.
        """
        assert reply.tool_call is not None
        name = reply.tool_call.name
        if name != START_REMOTE_AUDIO_BRIDGE:
            log.warning("unknown tool call name=%s; ignoring", name)
            self._audio.mark_turn_complete()
            return

        log.info(
            "user_id=%s tool=%s args=%s",
            self._user_id, name, reply.tool_call.arguments,
        )
        result = await self._bridge.start(REMOTE_BRIDGE_URL)
        log.info(
            "user_id=%s bridge start result ok=%s outcome=%s detail=%r",
            self._user_id, result.ok, result.outcome, result.detail,
        )
        ack = _BRIDGE_ACK if result.ok else _fail_message(result)
        self._scratchpad.add_assistant(ack)
        if not self._alive():
            await self._abort("tool")
            return
        await self._speak(ack)

    async def on_service_ping(
        self, service_id: str = "", public_url: str | None = None,
    ) -> bool:
        """Service-initiated call: announce to the user, then dial the bridge.

        Called by: `developer_ping` HTTP handler in `app/main.py` after
        `registry.get(user_id)` returns this pipeline. `public_url` is the URL
        the orchestrator just resolved out of `service_registry.get(service_id)`
        — when provided, the bridge dials it instead of the legacy
        `DEVELOPER_WS_REMOTE_BRIDGE_URL` env var. `public_url` may be None for
        back-compat with callers that did not register a service_id, in which
        case we fall back to the env-driven default.
        Calls: `_speak(announce)` → `bridge.start(...)` → either `_speak(fail)` or
        a scratchpad ack. Compares `service_id` from the ping body against
        `result.service_id` from the WS ack; logs a warning on mismatch.
        Takes `self._lock` so the announcement can't talk over an in-flight user turn.
        Returns True if the bridge is open (newly or already) when we exit.
        """
        dial_url = public_url or REMOTE_BRIDGE_URL
        log.info(
            "user_id=%s service ping received service_id=%s dial_url=%s "
            "(source=%s)",
            self._user_id, service_id or "unknown", dial_url,
            "registry" if public_url else "env-fallback",
        )
        if self._bridge.active:
            log.info("user_id=%s bridge already active; ignoring ping", self._user_id)
            return True
        if not self._alive():
            log.info("user_id=%s client gone; dropping ping", self._user_id)
            return False
        # Serialise against any in-flight user turn so the announcement doesn't
        # talk over a pending reply.
        async with self._lock:
            self._scratchpad.add_assistant(_SERVICE_PING_ANNOUNCE)
            if not self._alive():
                return False
            await self._speak(_SERVICE_PING_ANNOUNCE)
            result = await self._bridge.start(dial_url)
            log.info(
                "user_id=%s ping->bridge result ok=%s outcome=%s detail=%r service_id=%s",
                self._user_id, result.ok, result.outcome, result.detail,
                result.service_id or "?",
            )
            if (
                result.ok
                and service_id
                and result.service_id
                and service_id != result.service_id
            ):
                log.warning(
                    "user_id=%s service_id mismatch ping=%s ack=%s — "
                    "different service answered the bridge call",
                    self._user_id, service_id, result.service_id,
                )
            if result.ok:
                self._scratchpad.add_assistant(_BRIDGE_ACK)
                return True
            fail = _fail_message(result)
            self._scratchpad.add_assistant(fail)
            if self._alive():
                await self._speak(fail)
            return False

    async def _on_bridge_remote_close(self) -> None:
        """Callback fired when the remote (not us) closed the bridge.

        Registered with: `bridge.set_on_remote_close(self)` in `__init__`.
        Fired from: `bridge._recv_loop` finally block (only when `_self_closing` is False).
        Adds a scratchpad turn and speaks an announcement so the user knows they're
        back with the local assistant.
        """
        log.info("user_id=%s bridge remote-close notification", self._user_id)
        self._scratchpad.add_assistant(_BRIDGE_DISCONNECT)
        if not self._alive():
            return
        try:
            await self._speak(_BRIDGE_DISCONNECT)
        except Exception:
            log.exception("user_id=%s on_bridge_remote_close speak failed", self._user_id)

    async def _speak(self, text: str) -> None:
        """Synthesize and queue TTS for downlink, then mark the turn complete.

        Called by: `flush()`, `_handle_tool_call()`, `on_service_ping()`,
        `_on_bridge_remote_close()`.
        Calls: `tts.synthesize_speech_pcm24(text)` → `audio.add_playback_pcm(pcm)`
        → `audio.mark_turn_complete()` (flushes any Opus residual).
        """
        pcm24 = await synthesize_speech_pcm24(text)
        if not self._alive():
            await self._abort("TTS synth")
            return
        if pcm24:
            self._audio.add_playback_pcm(pcm24)
        self._audio.mark_turn_complete()

    def schedule_flush(self) -> None:
        """Fire-and-forget wrapper around `flush()`.

        Called by: `_receive_loop` in endpoint.py on `turn_complete:true` frames.
        Spawns a task so the caller (the WS read loop) doesn't block on the whole
        STT/LLM/TTS round-trip.
        """

        async def _run() -> None:
            try:
                await self.flush()
            except Exception as e:
                log.exception("flush failed user_id=%s: %s", self._user_id, e)

        asyncio.create_task(_run())
