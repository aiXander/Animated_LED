"""Spec types — the recursive {kind, params} envelope.

`NodeSpec` is the AST leaf the LLM emits; `LayerSpec` and `UpdateLedsSpec`
wrap it. The flattened-params recovery validator is here too — it's a
provider-quirk workaround that needs to live with the spec it's healing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodeSpec(BaseModel):
    """One node in the surface AST.

    Discriminated by `kind`. `params` is intentionally free-form here — each
    primitive's own pydantic Params validates the contents at compile time, so
    the error path lives at compile (with the full tree path) rather than at
    JSON parse (which would be a flat blob). This keeps `extra="forbid"`
    *per primitive* but lets the outer envelope accept any registered kind.
    """

    model_config = ConfigDict(extra="forbid")
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _recover_flattened_params(cls, data: Any) -> Any:
        """Tolerate a recurring LLM tool-call failure mode.

        Some providers (notably Gemini through OpenRouter) consistently emit
        nodes with the per-primitive params *flattened* — as siblings of
        `kind` instead of nested under `params: {...}` — and sometimes leave
        a truncated string in `params` itself, e.g.::

            {"kind": "wave", "params": "{axis:", "speed": 0.2, "shape": "cosine"}

        The tool schema can't pin a closed shape for `params` (it's
        per-primitive), so it's declared `additionalProperties: true`; the
        model loses the nesting. Without recovery the whole tree fails
        validation and the operator sees a `layer_validation_failed` retry
        loop. With recovery the primitive's own `Params` model still runs
        strictly downstream, so typos and bad enums are still rejected with
        a precise path — we just stop tripping on the structural mistake.

        Recovery rule: if the dict has a string `kind` plus sibling keys
        AND `params` is missing or not a dict, fold the siblings into a
        real `params` dict and drop the broken value. Well-formed input
        (params already a dict, no extras) takes the strict path unchanged.
        """
        if not isinstance(data, dict):
            return data
        if not isinstance(data.get("kind"), str):
            return data
        extras = {k: v for k, v in data.items() if k not in {"kind", "params"}}
        if not extras:
            return data
        params = data.get("params")
        if isinstance(params, dict):
            return data
        return {"kind": data["kind"], "params": extras}


class LayerSpec(BaseModel):
    """One mixer layer: a tree leaf rendering RGB, plus blend + opacity."""

    model_config = ConfigDict(extra="forbid")
    node: NodeSpec
    blend: Literal["normal", "add", "screen", "multiply"] = "normal"
    opacity: float = Field(1.0, ge=0.0, le=1.0)


class UpdateLedsSpec(BaseModel):
    """Tool argument: the complete new state of the install.

    Crossfade duration is *not* part of this spec. The operator's master
    crossfade slider is the single source of truth — the LLM never picks
    transition speed (the field is shown read-only in the system prompt).
    """

    model_config = ConfigDict(extra="forbid")
    layers: list[LayerSpec] = Field(default_factory=list)
    blackout: bool = False
