"""The single `write_effect` tool.

The LLM emits the COMPLETE effect (code + param schema) for the *selected*
preview layer. On receipt we:
  1. parse args via WriteEffectArgs (pydantic),
  2. AST-scan + sandbox-compile the source,
  3. instantiate + run init() against the real topology,
  4. fence-test synthetic frames,
  5. swap into the PREVIEW slot's selected layer (hard cut).

LLM-authored effects are NOT auto-saved to the library — the library is a
curated, manually-saved set. The operator clicks 💾 save effect to persist.
Live promotion is a separate operator action.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .persistence import EffectStore
from .runtime import Runtime
from .sandbox import EffectCompileError
from .schema import WriteEffectArgs

WRITE_EFFECT_TOOL_NAME = "write_effect"


def write_effect_tool_schema() -> dict[str, Any]:
    """OpenRouter / OpenAI tool schema for write_effect."""
    return {
        "type": "function",
        "function": {
            "name": WRITE_EFFECT_TOOL_NAME,
            "description": (
                "Replace the selected PREVIEW layer with a new Python Effect class plus "
                "an operator-UI param schema. The operator promotes preview → live "
                "separately, and tunes individual values via the UI sliders. "
                "Always emit the COMPLETE effect — never a diff."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "code", "params"],
                "properties": {
                    "name": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_]{0,40}$",
                        "description": "snake_case identifier, ≤40 chars",
                    },
                    "summary": {
                        "type": "string",
                        "maxLength": 400,
                        "description": "One-sentence description shown in the chat panel.",
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Python source defining exactly one `Effect` subclass at "
                            "module top level. ≤8 KB. No imports — runtime API is in scope."
                        ),
                    },
                    "params": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "required": ["key", "control"],
                            "properties": {
                                "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,40}$"},
                                "label": {"type": "string"},
                                "help": {"type": "string"},
                                "control": {
                                    "type": "string",
                                    "enum": [
                                        "slider", "int_slider", "color",
                                        "select", "toggle", "palette",
                                    ],
                                },
                                "min": {"type": "number"},
                                "max": {"type": "number"},
                                "step": {"type": "number"},
                                "default": {},
                                "options": {"type": "array", "items": {"type": "string"}},
                                "unit": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }


def apply_write_effect(
    raw_args: dict[str, Any],
    *,
    runtime: Runtime,
    store: EffectStore,
) -> dict[str, Any]:
    """Validate + compile + persist + swap into the selected PREVIEW layer."""
    try:
        args = WriteEffectArgs.model_validate(raw_args)
    except ValidationError as e:
        return {
            "ok": False,
            "error": "tool_argument_validation_failed",
            "details": e.errors(include_url=False, include_context=False),
        }

    param_schema = [p.model_dump() for p in args.params]

    # Auto-merge: pull operator's current tweaks for matching keys from the
    # currently-selected preview layer (the layer this write replaces).
    carry: dict[str, object] = {}
    sel = runtime.preview.selected_layer()
    if sel is not None:
        prev_values = sel.params.values()
        for spec in param_schema:
            key = spec["key"]
            if key in prev_values:
                carry[key] = prev_values[key]

    try:
        runtime.install_layer(
            "preview",
            name=args.name,
            summary=args.summary,
            source=args.code,
            param_schema=param_schema,
            param_values=carry,
            blend="normal",
            opacity=1.0,
        )
    except EffectCompileError as e:
        return {
            "ok": False,
            "error": "compile_failed",
            "details": str(e),
        }

    # Note: LLM-authored effects are intentionally NOT persisted here.
    # The library is a curated set; the operator must explicitly click 💾 save
    # (POST /preview/save) to add an effect to it.
    _ = store  # kept for signature compatibility / future use
    return {
        "ok": True,
        "applied": "preview",
        "name": args.name,
        "params": param_schema,
    }
