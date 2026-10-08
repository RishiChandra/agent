"""The validation gate (ORCHESTRATOR_V2_TOOL_CALLS.md §2.4).

Every tool call Gemini proposes is checked here before anything is spoken or
executed. Pure code: no LLM call, no database, well under a millisecond.

Slot checks, in order:
  1. Required slots (from the agent's registered `slots` schema) are present.
  2. Grounding (decision 2c): every slot value must be traceable to what the
     user actually said in their recent turns. Matching is lenient: case and
     punctuation are ignored, numbers match in words or digits ("seven" = 7,
     "7pm" = 19:00), names match phonetically, and at least half of a value's
     content words must appear. An ungrounded value means Gemini invented it,
     so the gate asks instead.
  3. Self-correction: if a value only appears before a correction marker
     ("no", "actually", "I mean", "wait", "make that"), the user changed their
     mind, so the gate asks which value they meant.

The router decision, read-backs and task references are applied by the
caller (`orchestrator/pipeline.py`), using the speech lines in `speech.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from orchestrator import speech
from orchestrator.routing.router import phonetic_key

_WORD_RE = re.compile(r"[a-z0-9]+(?::[0-9]{2})?")

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "a": None, "an": None,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SPECIAL = {"noon": 12, "midday": 12, "midnight": 0, "half": 30, "quarter": 15,
            "couple": 2, "dozen": 12, "first": 1, "second": 2, "third": 3, "fourth": 4,
            "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}

_STOP = frozenset(
    "a an the to for of and or with at on in by my me i you your please can could "
    "would will should is are be am it this that some any".split()
)

_CORRECTION_RE = re.compile(
    r"\b(no|nope|actually|i mean|wait|sorry|make that|make it|change (?:it|that) to|"
    r"scratch that|rather|instead)\b"
)

_RELATIVE_TIME = frozenset(
    "now today tonight tomorrow morning afternoon evening night noon midnight "
    "minute minutes hour hours day days week weekend next later soon monday tuesday "
    "wednesday thursday friday saturday sunday january february march april may june "
    "july august september october november december am pm oclock".split()
)


def words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower().replace("o'clock", "oclock").replace("'", ""))


def numbers_in(text: str) -> set[int]:
    """Every number mentioned in `text`, in words or digits, plus 12/24 h twins."""
    out: set[int] = set()
    toks = words(text)
    for i, tok in enumerate(toks):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", tok)
        if m:
            out.update({int(m.group(1)), int(m.group(2))})
            continue
        m = re.fullmatch(r"(\d+)(am|pm|st|nd|rd|th|s)?", tok)
        if m:
            out.add(int(m.group(1)))
            continue
        if tok in _UNITS and _UNITS[tok] is not None:
            out.add(_UNITS[tok])
        elif tok in _TENS:
            v = _TENS[tok]
            out.add(v)
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if nxt in _UNITS and _UNITS[nxt]:
                out.add(v + _UNITS[nxt])
        elif tok in _SPECIAL:
            out.add(_SPECIAL[tok])
    twins = {n + 12 for n in out if 1 <= n <= 11} | {n - 12 for n in out if 13 <= n <= 23}
    return out | twins


def _literal_numbers(text: str) -> set[int]:
    """Numbers literally present in a slot value (no 12/24 h twins; ":00" ignored)."""
    out: set[int] = set()
    for tok in words(text):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", tok)
        if m:
            out.add(int(m.group(1)))
            if int(m.group(2)):
                out.add(int(m.group(2)))
            continue
        m = re.fullmatch(r"(\d+)(am|pm|st|nd|rd|th|s)?", tok)
        if m:
            out.add(int(m.group(1)))
        elif tok in _UNITS and _UNITS[tok] is not None:
            out.add(_UNITS[tok])
        elif tok in _TENS:
            out.add(_TENS[tok])
    return out


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in _STOP and not w.isdigit() and w not in _UNITS
            and w not in _TENS and not re.fullmatch(r"\d+(:\d{2})?(am|pm|st|nd|rd|th|s)?", w)]


def _looks_like_datetime(value: str) -> Optional[datetime]:
    v = value.strip().replace("Z", "+00:00")
    if not re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def grounded(value: Any, heard: str) -> bool:
    """True if `value` can be traced to the user's words in `heard` (lenient)."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (list, tuple)):
        return all(grounded(v, heard) for v in value)
    if isinstance(value, dict):
        return all(grounded(v, heard) for v in value.values())
    if isinstance(value, (int, float)):
        return int(value) in numbers_in(heard) if float(value).is_integer() else str(value) in heard
    text = str(value).strip()
    if not text:
        return True
    dt = _looks_like_datetime(text)
    heard_words = set(words(heard))
    if dt is not None:
        # A computed date or time: grounded if the user mentioned the hour, or
        # spoke relative time ("in an hour", "tonight", "tomorrow at 6").
        hour = dt.hour
        if {hour, hour % 12 or 12} & numbers_in(heard):
            return True
        return bool(heard_words & _RELATIVE_TIME)
    # Every number in the value must have been said (in words or digits).
    raw_nums = _literal_numbers(text)
    if raw_nums and not raw_nums <= numbers_in(heard):
        return False
    vwords = content_words(text)
    if not vwords:
        return True
    heard_keys = {phonetic_key(w) for w in heard_words if len(w) > 2}
    hit = 0
    for w in vwords:
        if w in heard_words or (len(w) > 2 and phonetic_key(w) in heard_keys):
            hit += 1
        elif any(w in hw or hw in w for hw in heard_words if len(hw) > 3 and len(w) > 3):
            hit += 1
    return hit * 2 >= len(vwords)


@dataclass
class GateResult:
    ok: bool
    question: str = ""
    slot: str = ""
    reason: str = ""  # "missing" | "ungrounded" | "correction"
    slots: dict = field(default_factory=dict)


def check_slots(
    slots: Optional[dict],
    user_turns: Iterable[str],
    schema: Iterable[dict] = (),
    *,
    skip: Iterable[str] = (),
) -> GateResult:
    """Apply checks 1–3 to the slots of one tool call.

    `user_turns` are the user's recent utterances (oldest first; the last one
    is the current turn). `schema` is the agent's registered slot list:
    `[{"name", "required", "question"?}, ...]`. Slots named in `skip` are not
    grounded (e.g. values the orchestrator itself supplied).
    """
    turns = [t for t in user_turns if t and t.strip()]
    heard = " ".join(turns)
    current = turns[-1] if turns else ""
    clean = {k: v for k, v in (slots or {}).items() if v not in (None, "", [], {})}
    skip = set(skip)
    questions = {s["name"]: str(s.get("question") or "") for s in schema if s.get("name")}

    for s in schema:
        name = s.get("name")
        if name and s.get("required") and name not in clean:
            return GateResult(False, speech.ask_for_slot(name, questions.get(name, "")), name, "missing", clean)

    for name, value in clean.items():
        if name in skip:
            continue
        if not grounded(value, heard):
            return GateResult(False, speech.ask_for_slot(name, questions.get(name, "")), name, "ungrounded", clean)

    m = None
    for m in _CORRECTION_RE.finditer(current.lower()):
        pass
    if m is not None and m.start() > 0:
        before, after = current[: m.start()], current[m.end():]
        # A correction replaces the value mentioned last before the marker
        # ("for two at seven, no, eight" corrects the time, not the party size).
        # If some value already comes from after the marker, Gemini applied it.
        candidates = {n: v for n, v in clean.items() if n not in skip and not isinstance(v, bool)}
        applied = any(grounded(v, after) and not grounded(v, before) for v in candidates.values())
        target, best_pos = None, -1
        for name, value in candidates.items():
            pos = _last_position(value, before)
            if pos > best_pos:
                target, best_pos = name, pos
        if not applied and target is not None:
            value = clean[target]
            if not grounded(value, after) and _has_alternative(value, after):
                options = [_say(value)] + _alternatives(value, after)
                return GateResult(False, speech.which_value(options[:3]), target, "correction", clean)
    return GateResult(True, slots=clean)


def _last_position(value: Any, text: str) -> int:
    """Index of the last word in `text` that alone grounds `value`, or -1."""
    toks = words(text)
    for i in range(len(toks) - 1, -1, -1):
        if toks[i] not in _STOP and grounded(value, toks[i]):
            return i
    return -1


def _has_alternative(value: Any, after: str) -> bool:
    if isinstance(value, (int, float)) or numbers_in(str(value)):
        return bool(numbers_in(after) - {0})
    return bool(content_words(after))


def _alternatives(value: Any, after: str) -> list[str]:
    if isinstance(value, (int, float)) or numbers_in(str(value)):
        raw = [w for w in words(after) if w.isdigit() or w in _UNITS or w in _TENS]
        return [w for w in raw if w not in ("a", "an", "oh")][:2]
    cw = content_words(after)
    return [" ".join(cw[:3])] if cw else []


def _say(value: Any) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if m:
        h, mi = int(m.group(1)) % 12 or 12, int(m.group(2))
        return f"{h}:{mi:02d}" if mi else str(h)
    dt = _looks_like_datetime(str(value)) if isinstance(value, str) else None
    if dt is not None:
        h = dt.hour % 12 or 12
        return f"{h}:{dt.minute:02d}" if dt.minute else str(h)
    return str(value)


def summarize_request(intent: str, agent: str) -> str:
    """A spoken one-line summary for read-backs ("Book Nopa for 2 at 7")."""
    text = (intent or "").strip().rstrip(".")
    if not text:
        return f"Send that to {agent}"
    text = text[0].upper() + text[1:]
    if agent and agent.lower() not in text.lower():
        text = f"{text}, with {agent}"
    return text
