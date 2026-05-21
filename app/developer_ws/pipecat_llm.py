"""Custom GeminiLLMService — we own the Gemini call.

Rather than using Pipecat's built-in `GoogleLLMService`, this subclass keeps the
Gemini request construction in this file so debugging stays in our code (set a
breakpoint in `_call_gemini`, inspect each chunk, swap the model id, etc.). The
existing `agents/gemini_client.py` helpers (message conversion, tool conversion,
response shape adapter) are reused so nothing about the API shape changes.

Pipeline flow:

  TranscriptionFrame  →  append user turn to our internal LLMContext
                      →  call Gemini with the full context
                      →  emit LLMFullResponseStartFrame
                      →  if text part: emit LLMTextFrame (downstream TTS consumes)
                      →  if function_call part: collect → run_function_calls(...)
                      →  emit LLMFullResponseEndFrame

We don't use a context aggregator. The service owns the context. Assistant text
is added back to context when we emit the response end frame. Tool acks added by
handlers (`run_llm=False`) skip the LLM follow-up; tool results that *do* want a
follow-up call `_call_gemini` again via the standard FunctionCallResultFrame path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Optional

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import (
    FunctionCallFromLLM,
    LLMService,
)
from pipecat.services.settings import LLMSettings

log = logging.getLogger("developer_ws")

_DEFAULT_SYSTEM = (
    "You are a helpful assistant. The user's message below was transcribed from their speech. "
    "Reply briefly and clearly, as if you are speaking aloud to them. "
    "Do not prefix with 'The user said' unless necessary. "
    "\n\n"
    "Tool use — start_remote_audio_bridge: "
    "Call this tool only if the user asks to call/connect to the service, dial the remote, "
    "hand off to the remote server/operator, or says anything along the lines of 'call the "
    "service'. "
    "Bridge state is reflected in the conversation history: an assistant turn like "
    "'Connecting you to the remote service now.' means the bridge was opened, and a later "
    "turn like 'The remote service disconnected.' means it has since closed. If the most "
    "recent of those two is the open one, the bridge is currently OPEN — do not call the "
    "tool again unless the user explicitly asks to reconnect; instead acknowledge that "
    "they're already connected. If the most recent is the disconnect (or neither has been "
    "said), the bridge is currently CLOSED. "
    "Otherwise reply with plain text."
)


class CustomGeminiLLMService(LLMService):
    """Gemini LLM service that owns its own Gemini call.

    Constructed once per session. Holds an `LLMContext` with conversation history.
    `register_function(name, handler)` (from the LLMService base) wires tool
    handlers; handlers receive `FunctionCallParams` and may push TTSSpeakFrames
    for deterministic acks before returning `run_llm=False` via the result callback.
    """

    def __init__(
        self,
        *,
        user_id: str = "",
        tools_schema=None,
        system_instruction: Optional[str] = None,
        on_message_added: Optional[Callable[[dict], None]] = None,
        **kwargs,
    ) -> None:
        # Initialise LLMSettings explicitly so Pipecat's settings validator
        # doesn't warn about NOT_GIVEN fields. We use None for fields this
        # service doesn't honour (sampling params live in gemini_client.py).
        resolved_system = (
            (system_instruction or os.environ.get("DEVELOPER_GEMINI_SYSTEM_INSTRUCTION", "").strip())
            or _DEFAULT_SYSTEM
        )
        settings = LLMSettings(
            model=os.environ.get("GEMINI_TEXT_MODEL", "gemini-3-flash-preview"),
            system_instruction=resolved_system,
            temperature=None,
            max_tokens=None,
            top_p=None,
            top_k=None,
            frequency_penalty=None,
            presence_penalty=None,
            seed=None,
            filter_incomplete_user_turns=None,
            user_turn_completion_config=None,
        )
        super().__init__(settings=settings, **kwargs)
        self._user_id = user_id
        self._system = resolved_system
        self._tools_schema = tools_schema
        self._on_message_added = on_message_added
        # We seed the context with the system instruction as a "system" role
        # message. `_messages_to_contents` in gemini_client.py pulls system role
        # out and feeds it as system_instruction, so this round-trips cleanly.
        self._context = LLMContext(messages=[{"role": "system", "content": self._system}])

    # Exposed so other code (scratchpad dump, ping handler) can read history.
    @property
    def context(self) -> LLMContext:
        return self._context

    def _add_message(self, role: str, content: str) -> None:
        if not (content or "").strip():
            return
        message = {"role": role, "content": content}
        self._context.add_message(message)
        if self._on_message_added is not None:
            try:
                self._on_message_added(message)
            except Exception:
                log.exception("on_message_added callback failed")

    def add_assistant_announcement(self, text: str) -> None:
        """Inject an assistant turn that the user hears but no LLM produced.

        Used for service-initiated announcements and bridge-remote-close
        notifications so future LLM turns know the assistant 'said' those things.
        """
        self._add_message("assistant", text)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if not text:
                return
            self._add_message("user", text)
            await self._call_gemini()
            return

        if isinstance(frame, FunctionCallResultFrame):
            # Pipecat's run_function_calls broadcasts this with run_llm=True/False.
            # When run_llm=False (our default for deterministic-ack tools), we
            # only record the result in context — no follow-up call. When True,
            # we run Gemini again so it can speak the result.
            run_llm = bool(getattr(frame, "run_llm", False))
            try:
                result_text = (
                    frame.result if isinstance(frame.result, str)
                    else json.dumps(frame.result, default=str)
                )
            except Exception:
                result_text = str(frame.result)
            self._context.add_message({
                "role": "tool",
                "tool_call_id": frame.tool_call_id,
                "content": result_text,
            })
            if run_llm:
                await self._call_gemini()
            return

        await self.push_frame(frame, direction)

    async def _call_gemini(self) -> None:
        # Local import: agents.gemini_client lives at app-root, not under developer_ws.
        # We re-import per-call so it can read live env (model id, key) without restart.
        from agents.gemini_client import call_gemini, gemini_response_to_openai_like

        messages = self._context.get_messages()
        tools = self._tools_schema

        await self.push_frame(LLMFullResponseStartFrame())
        try:
            response = await asyncio.to_thread(
                call_gemini, list(messages), tools, "auto"
            )
        except Exception as e:
            log.warning("gemini generateContent failed user_id=%s err=%s", self._user_id, e)
            await self.push_frame(LLMFullResponseEndFrame())
            return

        wrapped = gemini_response_to_openai_like(response)
        msg = wrapped.choices[0].message
        text = (msg.content or "").strip()
        tool_calls = msg.tool_calls or []

        function_calls: list[FunctionCallFromLLM] = []
        if tool_calls:
            for tc in tool_calls:
                name = getattr(tc.function, "name", None) or ""
                if not name:
                    continue
                raw_args = getattr(tc.function, "arguments", "") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_call_id = getattr(tc, "id", None) or f"call_{uuid.uuid4().hex[:8]}"
                function_calls.append(
                    FunctionCallFromLLM(
                        context=self._context,
                        tool_call_id=tool_call_id,
                        function_name=name,
                        arguments=args,
                    )
                )

        if function_calls:
            # Record an empty assistant turn that announces the tool call so the
            # context stays consistent for follow-up inference.
            self._context.add_message({
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": fc.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": fc.function_name,
                            "arguments": json.dumps(fc.arguments),
                        },
                    }
                    for fc in function_calls
                ],
            })
            log.info(
                "user_id=%s gemini tool_calls=%s",
                self._user_id, [(fc.function_name, fc.arguments) for fc in function_calls],
            )
            try:
                await self.run_function_calls(function_calls)
            except Exception:
                log.exception("run_function_calls failed user_id=%s", self._user_id)
        elif text:
            log.info("user_id=%s gemini text=%r", self._user_id, text)
            self._add_message("assistant", text)
            await self.push_frame(LLMTextFrame(text))

        await self.push_frame(LLMFullResponseEndFrame())
