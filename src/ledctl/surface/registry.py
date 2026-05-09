"""Primitive base classes, the global registry, broadcast helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel

from ..masters import RenderContext

if TYPE_CHECKING:
    from ..topology import Topology
    from .compiler import Compiler

OutputKind = Literal["scalar_field", "scalar_t", "palette", "rgb_field"]


class CompiledNode:
    """Per-primitive instance produced by `Primitive.compile()`.

    Each compiled node owns its own state (RNGs, lattices, envelope memory)
    and exposes a single `render(ctx) -> value` method called from the hot
    path. The `output_kind` is set at compile time so the parent primitive
    can validate compatibility.
    """

    output_kind: ClassVar[OutputKind | None] = None

    def render(self, ctx: RenderContext) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class Primitive:
    """Marker base for a registered primitive.

    Concrete primitives expose:
      - `kind` (class var) — the registry key
      - `Params` — pydantic model (extra="forbid")
      - `compile(params, topology, compiler) -> CompiledNode`
      - `output_kind` for fixed-kind primitives, or polymorphic combinators
        resolve their output kind from compiled children inside `compile()`.
    """

    kind: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel]] = BaseModel
    output_kind: ClassVar[OutputKind | None] = None
    summary: ClassVar[str] = ""

    @classmethod
    def compile(
        cls,
        params: BaseModel,
        topology: Topology,
        compiler: Compiler,
    ) -> CompiledNode:  # pragma: no cover - abstract
        raise NotImplementedError


REGISTRY: dict[str, type[Primitive]] = {}


def primitive(cls: type[Primitive]) -> type[Primitive]:
    """Class decorator: register a primitive under its `kind`."""
    if not cls.kind:
        raise ValueError(f"{cls.__name__} has no `kind`")
    if cls.kind in REGISTRY:
        raise ValueError(f"primitive {cls.kind!r} already registered")
    REGISTRY[cls.kind] = cls
    return cls


def primitives_producing(kind: str) -> list[str]:
    """Return the kinds of primitives that produce `kind` directly.

    Used by `_check_kind` to turn "expected scalar_t, got scalar_field" into
    actionable advice ("use audio_band, constant, lfo"). Polymorphic
    combinators are excluded from the suggestion — they could match, but the
    LLM does better with concrete leaf primitives in front of it.
    """
    return sorted(
        prim.kind
        for prim in REGISTRY.values()
        if prim.output_kind == kind
    )


def broadcast_kind(a: str, b: str) -> str:
    """Compute the output kind of a binary scalar/RGB combinator.

    Rules:
      - rgb_field × scalar_*  → rgb_field (broadcast over channels)
      - rgb_field × rgb_field → rgb_field
      - scalar_field × scalar_t → scalar_field
      - scalar_t × scalar_t → scalar_t
      - palette anywhere → reject (use mix for palette lerp)
    """
    # Imported lazily to avoid the registry → compiler cycle.
    from .compiler import CompileError

    if a == "palette" or b == "palette":
        raise CompileError(
            "palette can only feed palette_lookup or mix(palette_a, palette_b, t); "
            "use palette_lookup to convert it to rgb_field"
        )
    if a == "rgb_field" or b == "rgb_field":
        return "rgb_field"
    if "scalar_field" in (a, b):
        return "scalar_field"
    return "scalar_t"


def broadcast_to_rgb(v: Any) -> np.ndarray:
    """Make `v` shaped (N, 3) — for binary ops where one side is rgb_field."""
    if isinstance(v, np.ndarray) and v.ndim == 2 and v.shape[1] == 3:
        return v
    if isinstance(v, np.ndarray):
        return v[:, None]
    return np.float32(v)  # type: ignore[return-value]


# Back-compat private aliases (the old surface.py exposed underscore names).
_primitives_producing = primitives_producing
_broadcast_kind = broadcast_kind
_broadcast_to_rgb = broadcast_to_rgb
