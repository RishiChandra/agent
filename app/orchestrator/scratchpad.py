"""Per-connection turn log dumped to stdout on socket close.

The authoritative conversation history lives inside `CustomGeminiLLMService`'s
`LLMContext`; this is a thin mirror populated via the `on_message_added` callback
the pipeline wires up. Kept separate so a one-line transcript per session lands
in stdout (`render()` via `dump()`) regardless of log level — handy for grepping
post-mortems without an LLM client.

Append-only. `add_user` and `add_assistant` are idempotent on empty/whitespace
input. `dump()` is called by `developer_websocket_endpoint` in its finally block.
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
        """Append a user turn. Called by `pipeline._mirror_to_scratchpad` whenever
        `CustomGeminiLLMService` appends a user message to its LLMContext.
        """
        t = (text or "").strip()
        if t:
            self.turns.append(Turn("user", t, self._now()))

    def add_assistant(self, text: str) -> None:
        """Append an assistant turn.

        Called by `pipeline._mirror_to_scratchpad` (mirroring LLMContext writes)
        and `pipeline.add_assistant_announcement` (bridge/service-ping notices).
        """
        t = (text or "").strip()
        if t:
            self.turns.append(Turn("assistant", t, self._now()))

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
