"""Gemini text (generateContent) for developer_ws — no Live API, no tools."""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_SYSTEM = (
    "You are a helpful assistant. The user's message below was transcribed from their speech. "
    "Reply briefly and clearly, as if you are speaking aloud to them. "
    "Do not prefix with 'The user said' unless necessary."
)


def gemini_reply_sync(transcript: str) -> str:
    """Run one text turn: system + user transcript → model reply (plain text)."""
    t = (transcript or "").strip()
    if not t:
        return ""
    from agents.gemini_client import call_gemini, gemini_response_to_openai_like

    system = (
        os.environ.get("DEVELOPER_GEMINI_SYSTEM_INSTRUCTION", "").strip()
        or _DEFAULT_SYSTEM
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": t},
    ]
    try:
        response = call_gemini(messages, tools=None)
    except Exception as e:
        print(f"[developer_ws] Gemini generateContent failed: {e}")
        return ""
    wrapped = gemini_response_to_openai_like(response)
    return (wrapped.choices[0].message.content or "").strip()


async def gemini_reply(transcript: str) -> str:
    return await asyncio.to_thread(gemini_reply_sync, transcript)
