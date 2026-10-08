"""Protocol 2 (task mode) message names, states and builders.

Wire spec: orchestrator/BRIDGE_PROTOCOL.md ("Task mode"). Task states and
`contextId` use Google A2A's vocabulary (decision M-k); message types are ours.
Pure stdlib so every other module (and the echo agent) can import it.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

PROTOCOL_VERSION = "2"

MODE_BRIDGE = "bridge"
MODE_TASK = "task"
MODES = (MODE_BRIDGE, MODE_TASK)

# One task connection carries many users, so its hello names this user and a
# registered URL's `{user_id}` placeholder is filled with it (BRIDGE_PROTOCOL.md).
TASK_USER_ID = "orchestrator"

# Handshake.
HELLO = "hello"
ACK = "ack"
BYE = "bye"
PING = "ping"
PONG = "pong"

# Orchestrator -> agent requests.
TASK_DISPATCH = "task.dispatch"
TASK_STATUS = "task.status"
TASK_UPDATE = "task.update"
TASK_CANCEL = "task.cancel"
TASK_INPUT = "task.input"
TASK_CLOSE = "task.close"
TASK_DELIVERED = "task.delivered"
REQUEST_TYPES = (
    TASK_DISPATCH, TASK_STATUS, TASK_UPDATE, TASK_CANCEL, TASK_INPUT, TASK_CLOSE, TASK_DELIVERED,
)
# Request type -> the `task_ops` name an agent advertises for it.
OP_OF = {
    TASK_DISPATCH: "dispatch", TASK_STATUS: "status", TASK_UPDATE: "update",
    TASK_CANCEL: "cancel", TASK_INPUT: "input", TASK_CLOSE: "close", TASK_DELIVERED: "delivered",
}
DEFAULT_TASK_OPS = ("dispatch", "cancel", "input")

# Replies (both directions).
TASK_ACK = "task.ack"
TASK_NACK = "task.nack"
# Agent -> orchestrator.
TASK_EVENT = "task.event"
TASK_CREATED = "task.created"
# Orchestrator -> agent, acknowledging an event on a socket.
TASK_EVENT_ACK = "task.event_ack"

# A2A task states on the wire.
SUBMITTED = "submitted"
WORKING = "working"
INPUT_REQUIRED = "input-required"
COMPLETED = "completed"
CANCELED = "canceled"
FAILED = "failed"
WIRE_STATES = (SUBMITTED, WORKING, INPUT_REQUIRED, COMPLETED, CANCELED, FAILED)
TERMINAL_WIRE_STATES = (COMPLETED, CANCELED, FAILED)

# Database `tasks.status` vocabulary (shared with Kairos reminders).
DB_PENDING = "pending"
DB_RUNNING = "running"
DB_INPUT_REQUIRED = "input_required"
DB_COMPLETED = "completed"
DB_FAILED = "failed"
DB_CANCELLED = "cancelled"
DB_TIMED_OUT = "timed_out"
DB_TERMINAL = (DB_COMPLETED, DB_FAILED, DB_CANCELLED, DB_TIMED_OUT)
DB_ACTIVE = (DB_PENDING, "dispatching", DB_RUNNING, DB_INPUT_REQUIRED)

WIRE_TO_DB = {
    SUBMITTED: DB_RUNNING,
    WORKING: DB_RUNNING,
    INPUT_REQUIRED: DB_INPUT_REQUIRED,
    COMPLETED: DB_COMPLETED,
    CANCELED: DB_CANCELLED,
    FAILED: DB_FAILED,
}

# Nack codes.
NACK_BUSY = "busy"
NACK_UNSUPPORTED_INTENT = "unsupported_intent"
NACK_MISSING_INPUT = "missing_input"
NACK_INVALID_INPUT = "invalid_input"
NACK_UNAUTHORIZED = "unauthorized"
NACK_UNKNOWN_TASK = "unknown_task"
NACK_TOO_LATE = "too_late"
NACK_NO_QUESTION = "no_question_pending"
NACK_UNSUPPORTED = "unsupported"
NACK_CANNOT_MEET_DEADLINE = "cannot_meet_deadline"

# Reasons.
CANCEL_USER = "user_cancelled"
CANCEL_DEADLINE = "deadline"
CANCEL_ESCALATED = "escalated_to_bridge"
CANCEL_ACCEPT_TIMEOUT = "accept_timeout"
CLOSE_COMPLETED_BY_USER = "completed_by_user"
CLOSE_DELETED = "deleted"
CLOSE_AGENT_LOST = "agent_lost"

DELIVERED_LIVE = "live"
DELIVERED_DEVICE_WAKE = "device_wake"
DELIVERED_NEXT_SESSION = "next_session"

CLOSE_MODE_NOT_SUPPORTED = 4405

MAX_SAY_CHARS = 2000
MAX_OUTPUT_BYTES = 256 * 1024
MAX_QUESTION_CHARS = 500


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_task_id() -> str:
    return str(uuid.uuid4())


def new_msg_id(prefix: str = "m") -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def new_context_id() -> str:
    return f"c-{uuid.uuid4().hex[:16]}"


def new_callback_token() -> str:
    return secrets.token_urlsafe(32)


def envelope(
    mtype: str,
    *,
    task_id: Optional[str] = None,
    context_id: Optional[str] = None,
    agent_task_ref: Optional[str] = None,
    body: Optional[dict] = None,
    msg_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    seq: Optional[int] = None,
) -> dict:
    """Build a Protocol 2 message (BRIDGE_PROTOCOL.md "Envelope")."""
    msg: dict[str, Any] = {"type": mtype, "msg_id": msg_id or new_msg_id()}
    if task_id is not None:
        msg["task_id"] = task_id
    if context_id is not None:
        msg["contextId"] = context_id
    if agent_task_ref is not None:
        msg["agent_task_ref"] = agent_task_ref
    if reply_to is not None:
        msg["reply_to"] = reply_to
    if seq is not None:
        msg["seq"] = seq
    msg["sent_at"] = now_iso()
    msg["body"] = body or {}
    return msg


def hello(user_id: str, *, mode: str, session_id: str = "",
          context_id: Optional[str] = None, task_id: Optional[str] = None) -> dict:
    msg: dict[str, Any] = {
        "type": HELLO, "user_id": user_id, "version": PROTOCOL_VERSION, "mode": mode,
        "session_id": session_id or new_msg_id("s"),
    }
    if context_id:
        msg["contextId"] = context_id
    if task_id:
        msg["task_id"] = task_id
    return msg


def task_url(url: str) -> str:
    """The URL a task-mode connection dials: `{user_id}` becomes TASK_USER_ID."""
    return (url or "").replace("{user_id}", TASK_USER_ID)


def ack_modes(ack: dict) -> tuple[str, ...]:
    """Modes an agent's handshake ack advertises. A v1 ack means bridge only."""
    modes = ack.get("modes")
    if isinstance(modes, list) and modes:
        return tuple(str(m).lower() for m in modes)
    return (MODE_BRIDGE,)


def reply(kind: str, request: dict, body: Optional[dict] = None) -> dict:
    """A task.ack / task.nack answering `request`."""
    return envelope(
        kind, task_id=request.get("task_id"), reply_to=request.get("msg_id"),
        body=body or {},
    )


def dumps(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), default=str)


def parse(raw: Any) -> Optional[dict]:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def clip(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
