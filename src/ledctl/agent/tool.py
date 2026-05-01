"""The single `update_leds` tool.

Argument shape mirrors a preset YAML — the LLM emits the *complete* new layer
stack every turn, never a diff. The argument is `surface.UpdateLedsSpec`: a
list of `LayerSpec`s, each carrying a tree of `{kind, params}` nodes.

The handler validates and compiles each layer against the surface registry,
then calls `Engine.crossfade_to`, the same code path that powers
`POST /presets/{name}`. If validation/compilation fails, we surface a
structured error in the tool result. The next turn sees that error in the
rolling buffer and can correct.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..mixer import BLEND_MODES
from ..surface import (
    REGISTRY,
    CompileError,
    Compiler,
    UpdateLedsSpec,
)

UPDATE_LEDS_TOOL_NAME = "update_leds"

# Re-export so callers (api/agent.py) don't import from surface directly.
UpdateLedsInput = UpdateLedsSpec


def update_leds_tool_schema() -> dict[str, Any]:
    """OpenAI/OpenRouter-compatible tool schema.

    The recursive `node: {kind, params}` shape can't fit a single closed
    JSON Schema (the `params` object is per-primitive). We pin `kind` to the
    registered primitives via `enum` and leave `params` open
    (`additionalProperties: true`) — the system prompt's CONTROL SURFACE
    block carries the per-primitive param schemas, and the server-side
    compiler rejects unknown keys with structured errors.
    """
    parameters = _flatten_schema(UpdateLedsSpec.model_json_schema())
    layer_props = parameters["properties"]["layers"]["items"]["properties"]
    # Layer.node is a NodeSpec; pin its `kind` enum and keep `params` open.
    node_schema = layer_props.get("node", {})
    node_props = node_schema.setdefault("properties", {})
    node_props["kind"] = {
        "type": "string",
        "enum": sorted(REGISTRY.keys()),
        "description": (
            "Primitive name. The leaf of every layer tree must be a primitive "
            "with output_kind=rgb_field (palette_lookup or solid). See the "
            "CONTROL SURFACE section of the system prompt for the catalogue."
        ),
    }
    node_props["params"] = {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Per-primitive parameters. Authoritative schema for each primitive "
            "is in the CONTROL SURFACE section of the system prompt. "
            "Validation is strict server-side: unknown keys, wrong nesting, "
            "or unknown enum values fail with a structured error you can read "
            "on the next turn."
        ),
    }
    return {
        "type": "function",
        "function": {
            "name": UPDATE_LEDS_TOOL_NAME,
            "description": (
                "Replace the LED layer stack with a complete new spec and "
                "crossfade to it. Always emit the full state — never a diff. "
                "For 'make it more red' or 'slower', re-emit the full stack "
                "with the relevant fields adjusted. The current stack is "
                "shown in CURRENT STATE in the system prompt."
            ),
            "parameters": parameters,
        },
    }


def _flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$ref`s and collapse `anyOf: [X, null]` for OpenRouter
    compatibility (Gemini's OpenAPI 3.0 subset rejects refs)."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref.startswith("#/$defs/"):
                    name = ref[len("#/$defs/"):]
                    target = defs.get(name)
                    if target is not None:
                        return walk(target)
                return {}
            if "anyOf" in node:
                branches = [b for b in node["anyOf"] if not _is_null_branch(b)]
                if len(branches) == 1:
                    merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **branches[0]}
                    return walk(merged)
                node = {**node, "anyOf": [walk(b) for b in branches]}
            return {
                k: walk(v) for k, v in node.items() if k not in {"$defs", "$schema"}
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def _is_null_branch(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "null"


def apply_update_leds(
    raw_args: dict[str, Any],
    *,
    engine: Any,
    default_crossfade_seconds: float,
) -> dict[str, Any]:
    """Validate + apply an `update_leds` call. Returns the tool result payload.

    `engine` is duck-typed (`Engine` from `ledctl.engine`) so this module stays
    test-friendly without importing the full render loop.
    """
    try:
        args = UpdateLedsSpec.model_validate(raw_args)
    except ValidationError as e:
        return {
            "ok": False,
            "error": "tool_argument_validation_failed",
            "details": e.errors(include_url=False, include_context=False),
        }

    if args.blackout:
        engine.mixer.blackout = True
        return {
            "ok": True,
            "applied": "blackout",
            "blackout": True,
            "layers": engine.layer_state(),
            "crossfade_seconds": 0.0,
        }
    engine.mixer.blackout = False

    duration = (
        args.crossfade_seconds
        if args.crossfade_seconds is not None
        else float(default_crossfade_seconds)
    )

    # Pre-flight: structured compile of every layer against the registry,
    # before mutating engine state. The engine itself would catch these too,
    # but doing it here keeps the failure mode "no change applied" — partial
    # crossfades would be confusing.
    layer_errors: list[dict[str, Any]] = []
    for i, layer in enumerate(args.layers):
        if layer.blend not in BLEND_MODES:
            layer_errors.append(
                {
                    "layer": i,
                    "field": "blend",
                    "msg": (
                        f"unknown blend {layer.blend!r}; "
                        f"must be one of {list(BLEND_MODES)}"
                    ),
                }
            )
            continue
        try:
            Compiler(engine.topology).compile_layer(layer)
        except CompileError as e:
            layer_errors.append(
                {
                    "layer": i,
                    "path": e.path,
                    "msg": e.raw_message,
                    "valid_kinds": sorted(REGISTRY.keys()),
                }
            )
        except ValidationError as e:
            for err in e.errors(include_url=False, include_context=False):
                layer_errors.append({"layer": i, **err})

    if layer_errors:
        return {
            "ok": False,
            "error": "layer_validation_failed",
            "details": layer_errors,
            "layers": engine.layer_state(),
        }

    try:
        engine.crossfade_to(args.layers, duration)
    except (ValidationError, TypeError, ValueError, KeyError) as e:
        return {
            "ok": False,
            "error": "engine_rejected_layers",
            "details": str(e),
            "layers": engine.layer_state(),
        }

    return {
        "ok": True,
        "applied": "update_leds",
        "blackout": False,
        "crossfade_seconds": duration,
        "layers": engine.layer_state(),
    }


__all__ = [
    "UPDATE_LEDS_TOOL_NAME",
    "UpdateLedsInput",
    "apply_update_leds",
    "update_leds_tool_schema",
]
