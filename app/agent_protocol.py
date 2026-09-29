"""Orchestrator ↔ agent wire protocol, v2.

Single source of truth for frame types, field names, versions and close codes,
shared by the live-audio bridge (`developer_ws/bridge.py`), the task dispatcher
(`task_dispatcher.py`) and the reference agent (`developer_ws/testing/echo_server.py`).
The human-readable spec is `developer_ws/BRIDGE_PROTOCOL.md`.

v2 is a superset of v1:

  * `hello` gains `mode` ("bridge" | "task") and `session_id`. A v1 agent
    ignores unknown fields and still answers with a v1 ack, which the
    orchestrator treats as "bridge only".
  * `ack` gains `modes` (what the agent supports) and `max_concurrency`.
  * **Task mode** adds a request/response exchange multiplexed by `task_id`
    over one WebSocket, so the orchestrator can *dispatch work* to an agent
    rather than only hand the user's microphone over to it.

Everything here is plain data + tiny pure helpers so it can be unit-tested and
imported without FastAPI, Pipecat or a database.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

PROTOCOL_VERSION = "2"
SUPPORTED_VERSIONS = ("1", "2")

MODE_BRIDGE = "bridge"
MODE_TASK = "task"

# ----- handshake -------------------------------------------------------------
HELLO = "hello"
ACK = "ack"
BYE = "bye"

# ----- bridge mode (unchanged from v1) ---------------------------------------
SAY = "say"  # remote → orchestrator: TTS this text to the user

# ----- liveness (both modes) ---------------------------------------------------
PING = "ping"
PONG = "pong"

# ----- task mode: orchestrator → agent ---------------------------------------
TASK_DISPATCH = "task.dispatch"
TASK_CANCEL = "task.cancel"
TASK_INPUT = "task.input"          # answer to a task.input_required question

# ----- task mode: agent → orchestrator ---------------------------------------
TASK_ACCEPTED = "task.accepted"
TASK_REJECTED = "task.rejected"
TASK_PROGRESS = "task.progress"
TASK_INPUT_REQUIRED = "task.input_required"
TASK_RESULT = "task.result"

AGENT_TO_ORCH_TASK_FRAMES = frozenset({
    TASK_ACCEPTED, TASK_REJECTED, TASK_PROGRESS, TASK_INPUT_REQUIRED, TASK_RESULT,
})

# task.result `status` values
RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"

# ----- close codes ------------------------------------------------------------
CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_REJECTED = 4403
CLOSE_UNSUPPORTED_MODE = 4405

# Limits the orchestrator enforces on agent → orchestrator payloads.
MAX_SAY_CHARS = 2000
MAX_OUTPUT_BYTES = 256 * 1024


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dumps(frame: dict) -> str:
    return json.dumps(frame, separators=(",", ":"), default=str)


def parse(raw: Any) -> Optional[dict]:
    """Decode one text frame into a dict, or None for anything else.

    Binary frames, non-JSON and non-object JSON are all None — callers drop
    them, which is what keeps the protocol forward-compatible.
    """
    if isinstance(raw, (bytes, bytearray)):
        return None
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) else None


# ----- frame builders --------------------------------------------------------


def hello(user_id: str, mode: str = MODE_BRIDGE, session_id: Optional[str] = None) -> dict:
    return {
        "type": HELLO,
        "version": PROTOCOL_VERSION,
        "user_id": user_id,
        "mode": mode,
        "session_id": session_id or new_id("sess"),
    }


def task_dispatch(
    *,
    task_id: str,
    user_id: str,
    intent: str,
    input: Optional[dict] = None,
    deadline_ms: int,
    attempt: int = 1,
    idempotency_key: Optional[str] = None,
) -> dict:
    return {
        "type": TASK_DISPATCH,
        "task_id": task_id,
        "user_id": user_id,
        "intent": intent,
        "input": input or {},
        "deadline_ms": int(deadline_ms),
        "attempt": attempt,
        "idempotency_key": idempotency_key or task_id,
    }


def task_cancel(task_id: str, reason: str = "") -> dict:
    return {"type": TASK_CANCEL, "task_id": task_id, "reason": reason}


def task_input(task_id: str, answer: str) -> dict:
    return {"type": TASK_INPUT, "task_id": task_id, "answer": answer}


# ----- ack interpretation ------------------------------------------------------


def ack_modes(ack: dict) -> list[str]:
    """Modes an agent declared in its ack. A v1 ack (no `modes`) means bridge only."""
    modes = ack.get("modes")
    if isinstance(modes, list) and modes:
        return [str(m).lower() for m in modes]
    return [MODE_BRIDGE]


def ack_max_concurrency(ack: dict, default: int = 8) -> int:
    try:
        return max(1, min(1000, int(ack.get("max_concurrency", default))))
    except (TypeError, ValueError):
        return default


def clip_text(text: Any, limit: int = MAX_SAY_CHARS) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"
