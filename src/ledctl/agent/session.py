"""Per-conversation rolling-buffer state.

Each session keeps the last `history_max` messages (excluding the system
prompt — system prompt is rebuilt fresh every turn). One user turn produces
three messages: user / assistant (with optional tool_calls) / tool, so a
20-message cap covers ~6 turns.

In-memory only — restart wipes them.
"""

from __future__ import annotations

import time
import uuid
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
    id: str
    created_at: float = field(default_factory=time.time)
    messages: deque[dict[str, Any]] = field(default_factory=deque)
    turns: list[AgentTurn] = field(default_factory=list)
    history_max: int = 20
    _rate_window: deque[float] = field(default_factory=deque)

    def append_messages(self, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            self.messages.append(m)
        while len(self.messages) > self.history_max:
            self.messages.popleft()
        self._heal_dangling_tool_messages()

    def _heal_dangling_tool_messages(self) -> None:
        while self.messages and self.messages[0].get("role") == "tool":
            self.messages.popleft()

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


class SessionStore:
    def __init__(self, history_max: int = 20):
        self.history_max = int(history_max)
        self._sessions: dict[str, ChatSession] = {}

    def get_or_create(self, session_id: str | None) -> ChatSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or uuid.uuid4().hex
        sess = ChatSession(id=sid, history_max=self.history_max)
        self._sessions[sid] = sess
        return sess

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def reset_all_buffers(self) -> None:
        for sess in self._sessions.values():
            sess.messages.clear()
