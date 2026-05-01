"""Per-conversation rolling-buffer state.

Each session keeps the last `history_max_messages` messages (excluding the
system prompt — the system prompt is regenerated fresh on every turn). One
user turn produces three messages: `user`, `assistant` (with optional
tool_calls), `tool` (the tool result), so a 20-message cap covers ~6 turns.

Sessions live in memory for v1 — restart wipes them. That's fine while we
learn how the panel gets used.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTurn:
    """One operator-visible turn (user → assistant text + optional tool call → tool result)."""

    user: str
    assistant_text: str = ""
    tool_call: dict[str, Any] | None = None  # {name, arguments}
    tool_result: dict[str, Any] | None = None
    error: str | None = None
    # Number of automatic retries the agent ran on top of the initial attempt
    # before the loop exited (success, no-tool, or budget-exhausted).
    retries_used: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class ChatSession:
    id: str
    created_at: float = field(default_factory=time.time)
    # Rolling LLM message buffer in OpenAI format. Capped via `history_max`.
    messages: deque[dict[str, Any]] = field(default_factory=deque)
    # Operator-visible transcript (one entry per user turn). Not capped — UI
    # rehydration wants the full thing while the session is alive.
    turns: list[AgentTurn] = field(default_factory=list)
    history_max: int = 20
    # Rolling-window rate limit timestamps (seconds since epoch).
    _rate_window: deque[float] = field(default_factory=deque)

    def append_messages(self, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            self.messages.append(m)
        # Cap to the most recent `history_max` messages. We never split a
        # tool_calls / tool pair: pop in 3-message chunks (user/asst/tool)
        # so the buffer stays well-formed for the next request.
        while len(self.messages) > self.history_max:
            self.messages.popleft()
        self._heal_dangling_tool_messages()

    def _heal_dangling_tool_messages(self) -> None:
        """If the buffer's first message is a `tool` reply (because we trimmed
        the assistant's tool_calls turn off the front), drop it — most providers
        reject a `tool` message that isn't preceded by an assistant tool_calls."""
        while self.messages and self.messages[0].get("role") == "tool":
            self.messages.popleft()

    def check_rate_limit(self, per_minute: int, now: float | None = None) -> bool:
        """Return True if the call is allowed; False if it should be denied."""
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


class SessionStore:
    """Process-local store for chat sessions (in-memory, v1)."""

    def __init__(self, history_max: int = 20):
        self.history_max = int(history_max)
        self._sessions: dict[str, ChatSession] = {}

    def get_or_create(self, session_id: str | None) -> ChatSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or _new_session_id()
        sess = ChatSession(id=sid, history_max=self.history_max)
        self._sessions[sid] = sess
        return sess

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def list_ids(self) -> list[str]:
        return list(self._sessions.keys())


def _new_session_id() -> str:
    return uuid.uuid4().hex
