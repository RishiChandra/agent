"""Per-connection turn log: user transcripts + assistant replies.

`history_messages()` returns prior turns in Gemini-style `{role, content}` form so each
new LLM call sees the conversation so far. Special assistant entries (bridge open/close
notifications) are added as plain text so the model can infer current bridge state from
history alone. Dumped to stdout on socket close for post-session inspection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Turn:
    role: str          # "user" or "assistant"
    text: str
    t_offset_s: float  # seconds since scratchpad start


@dataclass
class Scratchpad:
    user_id: str
    _t0: float = field(default_factory=time.monotonic)
    turns: list[Turn] = field(default_factory=list)

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def add_user(self, text: str) -> None:
        """Append a user turn. Called by `pipeline.flush` after STT returns."""
        t = (text or "").strip()
        if t:
            self.turns.append(Turn("user", t, self._now()))

    def add_assistant(self, text: str) -> None:
        """Append an assistant turn.

        Called by `pipeline.flush` (after a successful Gemini reply),
        `pipeline._handle_tool_call` (bridge open/fail acks),
        `pipeline.on_service_ping` (announcement + bridge ack),
        `pipeline._on_bridge_remote_close` (disconnect notice).
        """
        t = (text or "").strip()
        if t:
            self.turns.append(Turn("assistant", t, self._now()))

    def history_messages(self) -> list[dict]:
        """Return prior turns as Gemini-style messages (no system prompt, no current turn).

        Called by `pipeline.flush` *before* `scratchpad.add_user(text)` so the snapshot
        excludes the current user turn; `gemini_reply` appends it itself.
        """
        return [{"role": t.role, "content": t.text} for t in self.turns]

    def render(self) -> str:
        if not self.turns:
            return f"[scratchpad] user_id={self.user_id} (empty)"
        lines = [f"[scratchpad] user_id={self.user_id} turns={len(self.turns)}"]
        for turn in self.turns:
            lines.append(f"  +{turn.t_offset_s:6.2f}s {turn.role:>9}: {turn.text}")
        return "\n".join(lines)

    def dump(self) -> None:
        """Called by `developer_websocket_endpoint` finally block (always runs)."""
        # Plain print (not log) so the transcript is always visible regardless of log level.
        print(self.render())
