"""Single-turn Gemini text reply for the developer WebSocket path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("developer_ws")

_DEFAULT_SYSTEM = (
    "You are a helpful assistant. The user's message below was transcribed from their speech. "
    "Reply briefly and clearly, as if you are speaking aloud to them. "
    "Do not prefix with 'The user said' unless necessary. "
    "If — and only if — the user explicitly asks to connect, hand off, or talk to the remote "
    "server/operator, call the start_remote_audio_bridge tool. Otherwise reply with plain text."
)


@dataclass
class ToolCall:
    name: str
    arguments: dict


@dataclass
class GeminiReply:
    """Either text or tool_call is populated (never both meaningful)."""
    text: str = ""
    tool_call: ToolCall | None = None


def _parse_tool_call(tc) -> ToolCall | None:
    name = getattr(tc.function, "name", None)
    if not name:
        return None
    raw = getattr(tc.function, "arguments", "") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError):
        args = {}
    return ToolCall(name=name, arguments=args)


def _reply_sync(
    transcript: str, history: list[dict] | None, tools: list[dict] | None
) -> GeminiReply:
    text = (transcript or "").strip()
    if not text:
        return GeminiReply()
    from agents.gemini_client import call_gemini, gemini_response_to_openai_like

    system = (
        os.environ.get("DEVELOPER_GEMINI_SYSTEM_INSTRUCTION", "").strip()
        or _DEFAULT_SYSTEM
    )
    # System first, then prior turns (oldest → newest), then the new user transcript.
    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    try:
        # tool_choice="auto" lets Gemini decide between a tool call and a plain reply.
        response = call_gemini(messages, tools=tools, tool_choice="auto")
    except Exception as e:
        log.warning("Gemini generateContent failed: %s", e)
        return GeminiReply()
    wrapped = gemini_response_to_openai_like(response)
    msg = wrapped.choices[0].message
    if msg.tool_calls:
        parsed = _parse_tool_call(msg.tool_calls[0])
        if parsed is not None:
            return GeminiReply(tool_call=parsed)
    return GeminiReply(text=(msg.content or "").strip())


async def gemini_reply(
    transcript: str,
    history: list[dict] | None = None,
    tools: list[dict] | None = None,
) -> GeminiReply:
    """Run one text turn through Gemini. Returns text or a tool call, never both."""
    return await asyncio.to_thread(_reply_sync, transcript, history, tools)
