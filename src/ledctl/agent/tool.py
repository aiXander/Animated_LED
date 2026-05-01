"""The single `update_leds` tool.

Argument shape mirrors a preset YAML — the LLM emits the *complete* new layer
stack every turn, never a diff. The handler validates each layer through the
effect's pydantic Params schema (so per-effect param clamps come for free) and
calls `Engine.crossfade_to`, the same code path that powers `POST /presets/{name}`.

If validation fails, we surface the structured pydantic error in the tool
result. The next turn sees that error in the rolling buffer and can correct.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..effects.registry import list_effects
from ..mixer import BLEND_MODES

UPDATE_LEDS_TOOL_NAME = "update_leds"


class UpdateLedsLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect: str = Field(..., description="Effect name (see /effects for the catalogue)")
    # Free-form per-effect params. Advertised to the LLM as a proper JSON
    # object (`type: object, additionalProperties: true`) — the OpenRouter tool
    # contract. The validator below also accepts a JSON-encoded string as a
    # safety net, in case a provider ever falls back to that shape.
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-effect parameters; see the EFFECTS section of the system prompt.",
    )
    blend: Literal["normal", "add", "screen", "multiply"] = Field(
        "normal",
        description="How this layer composites onto the layers below.",
    )
    opacity: float = Field(1.0, ge=0.0, le=1.0)

    @field_validator("params", mode="before")
    @classmethod
    def _parse_params(cls, v: Any) -> Any:
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"params must be a JSON object; parse error: {e}"
                ) from e
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"params must decode to an object, got "
                    f"{type(parsed).__name__}"
                )
            return parsed
        return v


class UpdateLedsInput(BaseModel):
    """Tool argument: the complete new state of the install."""

    model_config = ConfigDict(extra="forbid")
    layers: list[UpdateLedsLayer] = Field(
        default_factory=list,
        description=(
            "Ordered layer stack; layer 0 renders onto black. Empty list with "
            "blackout=False means 'all dark' but blackout=True is preferred."
        ),
    )
    crossfade_seconds: float | None = Field(
        None,
        ge=0.0,
        description=(
            "How long to morph from old → new. Pick to fit: snappy ~0.3 s, "
            "smooth ~1.5 s, slow drift ~5 s."
        ),
    )
    blackout: bool = Field(
        False,
        description="Convenience: kill output. When true, `layers` is ignored.",
    )


def update_leds_tool_schema() -> dict[str, Any]:
    """OpenAI/OpenRouter-compatible tool schema.

    Standard JSON Schema as documented at
    https://openrouter.ai/docs/guides/features/tool-calling — `type: object`
    with `properties` and `required` per OpenAI's function-calling format.

    `params` is per-effect, so its schema can't be a single fixed object. We
    describe the *valid* effects as an enum and pin per-effect param shapes in
    the layer description, but keep the JSON-Schema shape of `params` open
    (`additionalProperties: true`) so providers like Gemini that don't fully
    support discriminated unions still accept the spec. Authoritative shape
    is in the system prompt's EFFECTS section, and the server-side validator
    rejects unknown keys with a structured error so the LLM can self-correct.
    """
    parameters = _flatten_schema(UpdateLedsInput.model_json_schema())
    layer_props = parameters["properties"]["layers"]["items"]["properties"]
    known_effects = sorted(list_effects().keys())
    # Pin `effect` to the actual catalogue so the LLM can't invent names.
    layer_props["effect"] = {
        "type": "string",
        "enum": known_effects,
        "description": (
            "Which generator to render. See EFFECTS in the system prompt for "
            "the full per-effect param schema."
        ),
    }
    layer_props["params"] = {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Per-effect parameters. Authoritative schema for each effect "
            "(including nested `palette` and `bindings`) is in the EFFECTS "
            "section of the system prompt. Validation is strict server-side: "
            "unknown keys, wrong nesting, or unknown enum values fail the "
            "tool call with a structured error you can read on the next turn."
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
    """Inline a Pydantic JSON Schema into the OpenRouter-canonical shape.

    Two normalisations, both required for broad provider compatibility:
      - Resolve `$ref` against `$defs` (Pydantic emits these for nested models;
        Gemini's OpenAPI 3.0 subset rejects refs — OpenRouter's docs show
        inlined schemas).
      - Collapse `anyOf: [X, {type: "null"}]` to `X` (Gemini conveys
        nullability via `nullable: true`, not a `null` type member).

    Everything else — `title`, `default`, `description`, `enum`,
    `additionalProperties`, etc. — is standard JSON Schema and goes through
    untouched.
    """
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
        args = UpdateLedsInput.model_validate(raw_args)
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
    # Coming out of blackout: clear the flag before crossfading so the new
    # stack actually renders.
    engine.mixer.blackout = False

    duration = (
        args.crossfade_seconds
        if args.crossfade_seconds is not None
        else float(default_crossfade_seconds)
    )

    # Pre-flight: structured validation of every layer against its effect
    # schema, before mutating engine state. The engine itself would catch
    # these too, but doing it here keeps the failure mode "no change applied"
    # — partial crossfades would be confusing.
    known = list_effects()
    specs: list[dict[str, Any]] = []
    layer_errors: list[dict[str, Any]] = []
    for i, layer in enumerate(args.layers):
        if layer.effect not in known:
            layer_errors.append(
                {
                    "layer": i,
                    "field": "effect",
                    "msg": (
                        f"unknown effect {layer.effect!r}; "
                        f"choose one of {sorted(known)}"
                    ),
                }
            )
            continue
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
        cls = known[layer.effect]
        try:
            cls.Params(**(layer.params or {}))
        except ValidationError as e:
            allowed = sorted(cls.Params.model_fields.keys())
            for err in e.errors(include_url=False, include_context=False):
                # Pydantic 'extra_forbidden' errors mean an unknown key snuck in.
                # Annotate with the allowed key list so the LLM can self-correct
                # in one turn instead of guessing.
                entry: dict[str, Any] = {"layer": i, **err}
                if err.get("type") == "extra_forbidden":
                    entry["hint"] = (
                        f"unknown key for effect={layer.effect!r}; "
                        f"valid keys: {allowed}"
                    )
                layer_errors.append(entry)
            continue
        specs.append(
            {
                "effect": layer.effect,
                "params": layer.params,
                "blend": layer.blend,
                "opacity": layer.opacity,
            }
        )

    if layer_errors:
        return {
            "ok": False,
            "error": "layer_validation_failed",
            "details": layer_errors,
            "layers": engine.layer_state(),
        }

    try:
        engine.crossfade_to(specs, duration)
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
