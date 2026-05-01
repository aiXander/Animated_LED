"""Language-driven control panel (Phase 6).

A thin language layer over the existing engine: the user types, the LLM emits
*one* `update_leds` tool call describing the complete desired layer stack, the
engine crossfades to it. No multi-tool agent loop. Follow-ups like "more red,
slower" work because the system prompt is regenerated *every* turn from
`Topology` + current layer stack + a fresh audio snapshot.
"""

from .client import AgentClient, MissingApiKey
from .session import AgentTurn, ChatSession, SessionStore
from .system_prompt import build_system_prompt
from .tool import (
    UPDATE_LEDS_TOOL_NAME,
    UpdateLedsInput,
    apply_update_leds,
    update_leds_tool_schema,
)

__all__ = [
    "AgentClient",
    "AgentTurn",
    "ChatSession",
    "MissingApiKey",
    "SessionStore",
    "UPDATE_LEDS_TOOL_NAME",
    "UpdateLedsInput",
    "apply_update_leds",
    "build_system_prompt",
    "update_leds_tool_schema",
]
