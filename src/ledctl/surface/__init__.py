"""Surface v2 — LLM-as-author runtime.

Public API:
    from ledctl.surface import (
        Runtime, Effect, EffectInitContext, EffectFrameContext, AudioView,
        FrameMap, MastersView, ParamStore,
        EffectStore, EffectCompileError,
        build_system_prompt, build_runtime_namespace,
        compile_effect,
        WriteEffectArgs, write_effect_tool_schema, apply_write_effect,
        WRITE_EFFECT_TOOL_NAME,
    )
"""

from .base import (
    AudioView,
    Effect,
    EffectFrameContext,
    EffectInitContext,
    FrameMap,
    MastersView,
    ParamStore,
    ParamView,
    RigInfo,
)
from .palettes import LUT_SIZE, NAMED_STOPS, named_palette, named_palette_names
from .persistence import EffectStore, StoredEffect
from .prompt import build_system_prompt
from .runtime import (
    BLEND_MODES,
    ActiveEffect,
    Composition,
    CrossfadeState,
    Layer,
    Runtime,
    build_runtime_namespace,
)
from .sandbox import MAX_SOURCE_BYTES, EffectCompileError, compile_effect
from .schema import WriteEffectArgs
from .tool import (
    WRITE_EFFECT_TOOL_NAME,
    apply_write_effect,
    write_effect_tool_schema,
)

__all__ = [
    "ActiveEffect",
    "AudioView",
    "BLEND_MODES",
    "Composition",
    "CrossfadeState",
    "Layer",
    "Effect",
    "EffectCompileError",
    "EffectFrameContext",
    "EffectInitContext",
    "EffectStore",
    "FrameMap",
    "LUT_SIZE",
    "MAX_SOURCE_BYTES",
    "MastersView",
    "NAMED_STOPS",
    "ParamStore",
    "ParamView",
    "RigInfo",
    "Runtime",
    "StoredEffect",
    "WRITE_EFFECT_TOOL_NAME",
    "WriteEffectArgs",
    "apply_write_effect",
    "build_runtime_namespace",
    "build_system_prompt",
    "compile_effect",
    "named_palette",
    "named_palette_names",
    "write_effect_tool_schema",
]
