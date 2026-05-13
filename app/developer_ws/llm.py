"""Single-turn Gemini text reply for the developer WebSocket path."""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("developer_ws")

_DEFAULT_SYSTEM = (
    "You are a helpful assistant. The user's message below was transcribed from their speech. "
    "Reply briefly and clearly, as if you are speaking aloud to them. "
    "Do not prefix with 'The user said' unless necessary."
)


def _reply_sync(transcript: str, history: list[dict] | None) -> str:
    text = (transcript or "").strip()
    if not text:
        return ""
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
        response = call_gemini(messages, tools=None)
    except Exception as e:
        log.warning("Gemini generateContent failed: %s", e)
        return ""
    wrapped = gemini_response_to_openai_like(response)
    return (wrapped.choices[0].message.content or "").strip()


async def gemini_reply(transcript: str, history: list[dict] | None = None) -> str:
    """Run one text turn through Gemini with optional prior conversation context."""
    return await asyncio.to_thread(_reply_sync, transcript, history)
