"""Code-side policies (ORCHESTRATOR_V2_TOOL_CALLS.md §1.3, §1.4, §1.6).

* `decide_mode`: connect (live call) or dispatch (background task), given
  Gemini's `mode_hint` (what the wording implied) and the agent's modes.
* `indirect_route`: for unnamed requests, route to the owning agent or let
  the orchestrator answer, from the agent's `routing_policy` and `user_data`.
* `resolve_notify`: `notify` is a slot like any other; `device` only when
  the user asked to be told.
* `wake_allowed`: quiet hours and the hourly cap on task wake-ups.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from orchestrator.tasks.protocol import MODE_BRIDGE, MODE_TASK
from orchestrator.routing.router import POLICY_OWNS_DOMAIN, AgentRecord

CONNECT = "connect"
DISPATCH = "dispatch"
ASK = "ask"
WRONG_MODE = "wrong_mode"

_CONNECT_WORDS = re.compile(
    r"\b(talk to|talk with|speak to|speak with|put me through|connect me|call|chat with|"
    r"get me|let me (ask|talk|speak))\b"
)
# Explicit "do it and report back" wording (rule 2).
_EXPLICIT_DISPATCH = re.compile(
    r"\b(have (it|him|her|them|\w+) |get it done|let me know|tell me when|when it'?s done|"
    r"in the background|report back)\b"
)
# One-shot actions (rule 4).
_DISPATCH_WORDS = re.compile(
    r"\b(remind|book|order|log|add|send|buy|schedule|set up|cancel|track|record)\b"
)
_OPEN_ENDED = re.compile(
    r"\b(help me|plan|planning|talk through|walk me through|brainstorm|ideas|advice|"
    r"figure out|discuss|chat|explore|questions about)\b"
)
_FUTURE = re.compile(
    r"\b(tomorrow|tonight|later|next \w+|in \d+ ?(min|minute|hour|day)s?|at \d|"
    r"when|watch|keep an eye|every|until)\b"
)
_PERSONAL = re.compile(
    r"\b(my|mine|me|i'?ve|i'?m|i had|i ate|i did|have i|did i|am i|i have|i need|i want)\b"
)
_ACTION = re.compile(
    r"\b(log|add|book|order|track|record|set|remind|cancel|delete|update|buy|send|"
    r"schedule|save|create|change|move)\b"
)
_ASKED_TO_BE_TOLD = re.compile(
    r"\b(let me know|tell me|notify me|ping me|wake me|call me|alert me|text me|"
    r"keep me (posted|updated)|update me|report back)\b"
)
_ASKED_SILENT = re.compile(
    r"\b(don'?t (tell|notify|bother|ping|wake) me|no need to (tell|let) me|"
    r"don'?t let me know|without telling me|silently|quietly)\b"
)


def decide_mode(
    agent: AgentRecord,
    mode_hint: str,
    intent: str = "",
    *,
    missing_required: int = 0,
) -> tuple[str, Optional[str]]:
    """Return (decision, other) where decision is connect / dispatch / ask / wrong_mode.

    For wrong_mode, `other` is the one mode the agent does support.
    Rule order matches the doc's §1.4 table.
    """
    hint = (mode_hint or "auto").strip().lower()
    can_connect = agent.supports(MODE_BRIDGE)
    can_dispatch = agent.supports(MODE_TASK)
    wanted = {"connect": CONNECT, "dispatch": DISPATCH}.get(hint)

    # 1. Only one mode supported.
    if can_connect != can_dispatch:
        only = CONNECT if can_connect else DISPATCH
        if wanted and wanted != only:
            return WRONG_MODE, only
        return only, None
    if not can_connect and not can_dispatch:
        return WRONG_MODE, None

    # 2. Explicit wording.
    if wanted:
        return wanted, None
    text = (intent or "").lower()
    if _EXPLICIT_DISPATCH.search(text):
        return DISPATCH, None
    if _CONNECT_WORDS.search(text) and not _DISPATCH_WORDS.search(text):
        return CONNECT, None

    # 3. Open-ended, or several details missing: faster for the agent to ask live.
    if _OPEN_ENDED.search(text) or missing_required > 1:
        return CONNECT, None

    # 4. Complete one-shot, future-dated or long-running.
    if _DISPATCH_WORDS.search(text) or _FUTURE.search(text):
        return DISPATCH, None

    # 5. Still unclear.
    return ASK, None


def needs_user_data_or_action(intent: str) -> bool:
    text = (intent or "").lower()
    return bool(_PERSONAL.search(text) or _ACTION.search(text))


def indirect_route(agent: AgentRecord, intent: str) -> bool:
    """For an unnamed request matched to `agent`: route (True) or answer (False).

    `owns_domain` agents get every in-domain request. `on_request` agents get
    requests that ask for an action, or that ask about the user's own data
    ("my …", "have I …") when the agent holds personal data (`user_data`).
    """
    if agent.routing_policy == POLICY_OWNS_DOMAIN:
        return True
    text = (intent or "").lower()
    if _ACTION.search(text):
        return True
    return bool(agent.user_data and _PERSONAL.search(text))


def resolve_notify(requested: str, user_text: str, *, default: str = "next_session") -> str:
    """`device` / `next_session` / `silent`, grounded in what the user said."""
    text = (user_text or "").lower()
    if _ASKED_SILENT.search(text):
        return "silent"
    if _ASKED_TO_BE_TOLD.search(text):
        return "device"
    requested = (requested or "").strip().lower()
    if requested == "silent":
        return "silent"
    # `device` from Gemini without the user asking is not grounded.
    if requested == "next_session":
        return "next_session"
    return default if default in ("device", "next_session", "silent") else "next_session"


def _quiet_hours() -> tuple[int, int]:
    raw = os.environ.get("ORCHESTRATOR_QUIET_HOURS", "22-7").strip()
    try:
        start, end = (int(x) for x in raw.split("-", 1))
        return start, end
    except ValueError:
        return 22, 7


def in_quiet_hours(tz_name: Optional[str], now: Optional[datetime] = None) -> bool:
    start, end = _quiet_hours()
    if end - start >= 24:  # "0-24": quiet all day
        return True
    start, end = start % 24, end % 24
    if start == end:  # "0-0": no quiet hours
        return False
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo(os.environ.get("ORCHESTRATOR_DEFAULT_TZ", "America/Los_Angeles"))
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    hour = (now or datetime.now(timezone.utc)).astimezone(tz).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def max_wakes_per_hour() -> int:
    try:
        return int(os.environ.get("ORCHESTRATOR_MAX_WAKES_PER_HOUR", "4"))
    except ValueError:
        return 4


def wake_allowed(tz_name: Optional[str], wakes_last_hour: int, now: Optional[datetime] = None) -> bool:
    if in_quiet_hours(tz_name, now):
        return False
    return wakes_last_hour < max_wakes_per_hour()
