"""Single rolling-buffer chat state.

There is exactly ONE chat session in this app at any time — no
multi-tenant, no session ids, no dictionary lookup. The session keeps the
last `history_max_turns` turns of conversation. A "turn" is everything
anchored to one user message: the user message itself, the assistant
reply, any tool calls + tool results, and any retry pairs.

Trimming by turn (rather than raw message count) guarantees the buffer
never starts mid-turn — no orphan tool-result with no preceding
assistant tool_call, which OpenAI/OpenRouter rejects.

In-memory only — restart wipes it. Operator actions that change which
layer the LLM is authoring (preview layer select, library load, pull
live→preview, save/rename of the preview source) call `reset()` to wipe
both `messages` and `turns`, so the next `/agent/chat` call starts from
a clean slate with just the regenerated system prompt + new user msg.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTurn:
    user: str
    assistant_text: str = ""
    tool_call: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    error: str | None = None
    retries_used: int = 0
    usage: dict[str, int] | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class ChatSession:
    created_at: float = field(default_factory=time.time)
    messages: deque[dict[str, Any]] = field(default_factory=deque)
    turns: list[AgentTurn] = field(default_factory=list)
    history_max_turns: int = 5
    _rate_window: deque[float] = field(default_factory=deque)

    def append_messages(self, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            self.messages.append(m)
        self._trim_to_last_turns()

    def _trim_to_last_turns(self) -> None:
        """Keep at most `history_max_turns` user-anchored turns in the buffer.

        Walks from the end backwards, counting `role == "user"` markers; any
        messages before the (N+1)-th user marker get dropped. Trimming by
        turn keeps tool_call / tool_result pairs together — required so the
        OpenAI message format stays valid (every `tool` message must follow
        an `assistant` message that issued its tool_call).
        """
        if self.history_max_turns <= 0:
            self.messages.clear()
            return
        seen_users = 0
        keep_from = 0
        msgs = list(self.messages)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                seen_users += 1
                if seen_users == self.history_max_turns:
                    keep_from = i
                    break
        else:
            return
        for _ in range(keep_from):
            self.messages.popleft()

    def reset(self) -> None:
        """Full new-chat wipe: drop messages, transcript, and rate-window."""
        self.messages.clear()
        self.turns.clear()
        self._rate_window.clear()

    def check_rate_limit(self, per_minute: int, now: float | None = None) -> bool:
        if per_minute <= 0:
            return True
        t = time.time() if now is None else now
        cutoff = t - 60.0
        while self._rate_window and self._rate_window[0] < cutoff:
            self._rate_window.popleft()
        if len(self._rate_window) >= per_minute:
            return False
        self._rate_window.append(t)
        return True
