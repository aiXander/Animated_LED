"""The single LED control surface.

One file owns the entire effect / palette / modulator vocabulary. Everything
the engine needs to render a layer, the agent needs to describe a layer, and
the operator UI needs to build a form lives here:

  - colour utilities (hex_to_rgb01)
  - shape utilities (cosine / sawtooth / pulse / gauss as 1-D pure fns)
  - the primitive registry
  - every primitive's Params + compile()
  - named palettes
  - the spec types (NodeSpec, LayerSpec, UpdateLedsSpec)
  - the compiler (`compile(spec, topology) → render_fn`)
  - generate_docs() → the prompt-ready CONTROL SURFACE block
  - example trees + anti-patterns the LLM is taught from

Adding a new visual idea is a one-place change: write a `@primitive` class,
re-export nothing, the agent prompt and operator UI both pick it up via
`generate_docs()` / `GET /surface/primitives` automatically.

The output kinds the type-checker enforces at compile time:

  scalar_field — per-LED scalar in [0, 1]; spatial
  scalar_t     — single scalar/frame; time-only, no spatial dep
  palette      — 256-entry RGB LUT
  rgb_field    — per-LED RGB in [0, 1]³; the layer leaf

Compatibility (enforced at compile, not at runtime):

  param expects   accepts
  scalar_field    scalar_field directly, or scalar_t (broadcast)
  scalar_t        scalar_t only
  palette         palette only
  rgb_field       rgb_field only

For polymorphic combinators (`mul`, `add`, `screen`, `max`, `min`, `mix`):
  - if any input is rgb_field → output is rgb_field, scalar inputs broadcast
  - palette × scalar_t → palette (mix only); palette + anything else is rejected
  - otherwise output is scalar_field if any input is scalar_field, else scalar_t

This module never imports from `engine.py` or `mixer.py` — they import
us — so the dependency direction stays clean.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .masters import RenderContext
from .topology import Topology

# ---------------------------------------------------------------------------
# Colour + shape utilities
# ---------------------------------------------------------------------------


def hex_to_rgb01(s: str) -> np.ndarray:
    """Parse a #rrggbb (or rrggbb) hex into a float32 RGB array in [0, 1]."""
    raw = s.lstrip("#")
    if len(raw) != 6:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}")
    try:
        r = int(raw[0:2], 16)
        g = int(raw[2:4], 16)
        b = int(raw[4:6], 16)
    except ValueError as e:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}") from e
    return np.array([r / 255.0, g / 255.0, b / 255.0], dtype=np.float32)


def _hsv_to_rgb01(
    h: np.ndarray, s: np.ndarray, v: np.ndarray
) -> np.ndarray:
    """Vectorised HSV->RGB. Hue in degrees (any sign / magnitude — taken mod 360);
    s, v in [0, 1]. Returns (N, 3) float32 in [0, 1].

    Used to bake `palette_hsv` (and the named `rainbow` palette) without the
    muddy / desaturated midpoints that RGB-space lerp gives between
    complementary colours.
    """
    h = np.mod(h.astype(np.float32, copy=False), 360.0)
    s = s.astype(np.float32, copy=False)
    v = v.astype(np.float32, copy=False)
    c = v * s
    h6 = h / 60.0
    x = c * (1.0 - np.abs(np.mod(h6, 2.0) - 1.0))
    zero = np.zeros_like(h)
    # Per-sector channel tables; shape (6, N) each.
    r_t = np.stack([c, x, zero, zero, x, c])
    g_t = np.stack([x, c, c, x, zero, zero])
    b_t = np.stack([zero, zero, x, c, c, x])
    seg = np.minimum(h6.astype(np.int32), 5)  # h=360 wraps to seg 0 via mod above
    rows = np.arange(h.shape[0])
    m = v - c
    return np.stack(
        [r_t[seg, rows] + m, g_t[seg, rows] + m, b_t[seg, rows] + m],
        axis=1,
    ).astype(np.float32, copy=False)


def _apply_shape(
    phase: np.ndarray,
    shape: str,
    softness: float,
    width: float,
    out: np.ndarray,
) -> None:
    """Evaluate `shape` on a fract-phase array and write into `out` (N,)."""
    if shape == "cosine":
        smooth = (np.cos(2.0 * np.pi * phase) + 1.0) * 0.5
        if softness >= 1.0:
            np.copyto(out, smooth.astype(np.float32, copy=False))
        elif softness <= 0.0:
            np.copyto(out, (smooth > 0.5).astype(np.float32))
        else:
            hard = (smooth > 0.5).astype(np.float32)
            np.copyto(
                out,
                (softness * smooth + (1.0 - softness) * hard).astype(
                    np.float32, copy=False
                ),
            )
    elif shape == "sawtooth":
        np.copyto(out, phase.astype(np.float32, copy=False))
    elif shape == "pulse":
        np.copyto(out, (phase < 0.5).astype(np.float32))
    elif shape == "gauss":
        d = phase - np.round(phase)
        np.copyto(
            out,
            np.exp(-(d * d) / max(width * width, 1e-9)).astype(
                np.float32, copy=False
            ),
        )
    else:
        raise ValueError(f"unknown shape {shape!r}")


# ---------------------------------------------------------------------------
# Named palettes
# ---------------------------------------------------------------------------

# Mutable at boot via `set_lut_size()` (driven by `output.lut_size` in the
# YAML). 256 is the default — adequate for the 1800-LED install in most
# scenes. Bump higher (e.g. 1024) if a smooth scalar walking the full palette
# shows visible "stair" banding; cost is purely the one-time bake + LUT memory.
LUT_SIZE = 256


def set_lut_size(n: int) -> None:
    """Override the palette LUT size. Must be called before any palettes bake."""
    global LUT_SIZE
    if n < 2:
        raise ValueError(f"lut_size must be >= 2, got {n}")
    LUT_SIZE = int(n)

# Tagged: each entry is {"interp": "rgb"|"hsv", "stops": ...}.
#   - "rgb" stops are (pos, "#rrggbb") tuples; intentionally vary brightness
#     (fire / ice / sunset / ocean go dark->bright on purpose).
#   - "hsv" stops are {pos, hue, sat?, val?} dicts; they bake via hue-space
#     interpolation so the LUT stays at uniform brightness with no muddy /
#     grey midpoints between complementary colours.
NAMED_PALETTES: dict[str, dict[str, Any]] = {
    "rainbow": {
        "interp": "hsv",
        "stops": [
            {"pos": 0.0, "hue": 0.0},
            {"pos": 1.0, "hue": 360.0},
        ],
    },
    "fire": {
        "interp": "rgb",
        "stops": [
            (0.00, "#000000"),
            (0.25, "#600000"),
            (0.50, "#ff3000"),
            (0.75, "#ffa000"),
            (1.00, "#ffff80"),
        ],
    },
    "ice": {
        "interp": "rgb",
        "stops": [
            (0.0, "#000010"),
            (0.4, "#003080"),
            (0.7, "#00a0e0"),
            (1.0, "#ffffff"),
        ],
    },
    "sunset": {
        "interp": "rgb",
        "stops": [
            (0.0, "#100030"),
            (0.4, "#c02060"),
            (0.7, "#ff7020"),
            (1.0, "#ffe080"),
        ],
    },
    "ocean": {
        "interp": "rgb",
        "stops": [
            (0.0, "#001020"),
            (0.4, "#006080"),
            (0.7, "#20a0c0"),
            (1.0, "#c0f0ff"),
        ],
    },
    "warm": {
        "interp": "rgb",
        "stops": [
            (0.0, "#ff3000"),
            (0.5, "#ffa000"),
            (1.0, "#ff5000"),
        ],
    },
    "white": {"interp": "rgb", "stops": [(0.0, "#ffffff"), (1.0, "#ffffff")]},
    "black": {"interp": "rgb", "stops": [(0.0, "#000000"), (1.0, "#000000")]},
}


def _bake_lut(positions: np.ndarray, colors: np.ndarray) -> np.ndarray:
    x = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32)
    lut = np.empty((LUT_SIZE, 3), dtype=np.float32)
    for ch in range(3):
        lut[:, ch] = np.interp(x, positions, colors[:, ch])
    return lut


def _lut_from_named(name: str) -> np.ndarray:
    if name.startswith("mono_"):
        rgb = hex_to_rgb01(name[5:])
        positions = np.array([0.0, 1.0], dtype=np.float32)
        colors = np.stack([rgb, rgb])
        return _bake_lut(positions, colors)
    if name not in NAMED_PALETTES:
        raise ValueError(
            f"unknown palette {name!r}; choose one of {sorted(NAMED_PALETTES)} "
            f"or mono_<hex>"
        )
    spec = NAMED_PALETTES[name]
    if spec["interp"] == "hsv":
        return _lut_from_hsv_stops(spec["stops"])
    stops = spec["stops"]
    positions = np.array([p for p, _ in stops], dtype=np.float32)
    colors = np.stack([hex_to_rgb01(c) for _, c in stops])
    return _bake_lut(positions, colors)


def _lut_from_stops(stops: list[dict[str, Any]]) -> np.ndarray:
    if len(stops) < 2:
        raise ValueError("palette_stops needs at least 2 stops")
    sorted_stops = sorted(stops, key=lambda s: s["pos"])
    positions = np.array([s["pos"] for s in sorted_stops], dtype=np.float32)
    colors = np.stack([hex_to_rgb01(s["color"]) for s in sorted_stops])
    return _bake_lut(positions, colors)


def _lut_from_hsv_stops(stops: list[dict[str, Any]]) -> np.ndarray:
    """Bake an RGB LUT (size = `LUT_SIZE`) from hue/sat/val stops via HSV-space lerp.

    Hue can take any signed value (interpreted mod 360 only at the final
    HSV->RGB step), so the user explicitly controls the path: stops at
    hue=0,360 walks the full chromatic circle red->...->red the long way;
    stops at hue=0,-180 goes red->magenta->blue (the other way).
    """
    if len(stops) < 2:
        raise ValueError("palette_hsv needs at least 2 stops")
    sorted_stops = sorted(stops, key=lambda s: s["pos"])
    positions = np.array([s["pos"] for s in sorted_stops], dtype=np.float32)
    hues = np.array([s["hue"] for s in sorted_stops], dtype=np.float32)
    sats = np.array(
        [s.get("sat", 1.0) for s in sorted_stops], dtype=np.float32
    )
    vals = np.array(
        [s.get("val", 1.0) for s in sorted_stops], dtype=np.float32
    )
    x = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32)
    h = np.interp(x, positions, hues).astype(np.float32, copy=False)
    s = np.interp(x, positions, sats).astype(np.float32, copy=False)
    v = np.interp(x, positions, vals).astype(np.float32, copy=False)
    return _hsv_to_rgb01(h, s, v)


# ---------------------------------------------------------------------------
# Spec types — the recursive {kind, params} envelope
# ---------------------------------------------------------------------------


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
            # Both forms present — let strict `extra_forbidden` fire; this is
            # likely a real typo, not the known flattening pattern.
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


# ---------------------------------------------------------------------------
# Primitive registry
# ---------------------------------------------------------------------------

OutputKind = Literal["scalar_field", "scalar_t", "palette", "rgb_field"]


class CompiledNode:
    """Per-primitive instance produced by `Primitive.compile()`.

    Each compiled node owns its own state (RNGs, lattices, envelope memory)
    and exposes a single `render(ctx) -> value` method called from the hot
    path. The `output_kind` is set at compile time so the parent primitive
    can validate compatibility.
    """

    output_kind: ClassVar[OutputKind | None] = None  # set on subclasses

    def render(self, ctx: RenderContext) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class Primitive:
    """Marker base for a registered primitive.

    Concrete primitives expose:
      - `kind` (class var) — the registry key
      - `Params` — pydantic model (extra="forbid")
      - `compile(params, topology, compiler) -> CompiledNode`
      - `output_kind` for fixed-kind primitives, or `output_kind_for(...)` for
        polymorphic combinators (resolved from compiled children).
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


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


class CompileError(ValueError):
    """Structured compile error with a tree path.

    The path is dotted (`layers[0].node.params.scalar.params.shape`) so the
    LLM tool-result formatter can surface it to the model verbatim.

    `expected_kind` is set when the failure is a kind mismatch — the tool-
    result formatter uses it to filter `valid_kinds` to just the primitives
    that produce the expected kind, so the LLM gets a small, relevant list
    instead of the entire registry.
    """

    def __init__(
        self,
        message: str,
        path: list[str] | None = None,
        expected_kind: str | None = None,
    ):
        self.raw_message = message
        self.path = list(path or [])
        self.expected_kind = expected_kind
        super().__init__(self._format())

    def _format(self) -> str:
        if not self.path:
            return self.raw_message
        return f"{'.'.join(self.path)}: {self.raw_message}"


def _format_nodespec_error(e: ValidationError, raw: Any) -> str:
    """Tool-result-friendly summary of NodeSpec validation failure.

    The default pydantic dump is a multi-error wall; the LLM is more likely
    to self-correct when the structural rule is stated up front. We detect
    the flattened-params shape (extras alongside `kind`, with `params`
    missing or not a dict) the recovery validator didn't catch and add a
    one-line hint."""
    if isinstance(raw, dict) and isinstance(raw.get("kind"), str):
        extras = sorted(k for k in raw if k not in {"kind", "params"})
        if extras:
            params = raw.get("params")
            params_kind = (
                "a dict" if isinstance(params, dict) else type(params).__name__
            )
            return (
                f"node has sibling keys {extras} alongside `kind` (params is "
                f"{params_kind}). Per-primitive parameters must be nested as "
                f"`params: {{...}}`, not flattened onto the node."
            )
    errs = e.errors(include_url=False, include_context=False)
    if errs:
        first = errs[0]
        return f"NodeSpec invalid: {first['msg']} at {first['loc']}"
    return str(e)


class Compiler:
    """Walks a NodeSpec tree, validating + instantiating CompiledNodes."""

    def __init__(self, topology: Topology):
        self.topology = topology
        self._path: list[str] = []

    # ---- child-node compilation, used by primitives ----

    def compile_child(
        self,
        raw: Any,
        *,
        expect: OutputKind,
        path: str,
    ) -> CompiledNode:
        """Compile `raw` (a NodeSpec, dict, number, or string) and check kind.

        Numbers become a `constant` primitive (output: scalar_t) which is then
        broadcast if the expected kind is scalar_field. Strings become a
        `palette_named` primitive (output: palette).
        """
        self._path.append(path)
        try:
            node = self._coerce_to_nodespec(raw)
            child = self._compile_node(node)
            self._check_kind(child.output_kind, expect)
            return child
        finally:
            self._path.pop()

    def _coerce_to_nodespec(self, raw: Any) -> NodeSpec:
        if isinstance(raw, NodeSpec):
            return raw
        if isinstance(raw, dict):
            try:
                return NodeSpec.model_validate(raw)
            except ValidationError as e:
                raise CompileError(_format_nodespec_error(e, raw), self._path) from e
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return NodeSpec(kind="constant", params={"value": float(raw)})
        if isinstance(raw, str):
            return NodeSpec(kind="palette_named", params={"name": raw})
        raise CompileError(
            f"cannot use {type(raw).__name__} here; expected a node, number, "
            f"or palette string",
            self._path,
        )

    def _compile_node(self, node: NodeSpec) -> CompiledNode:
        cls = REGISTRY.get(node.kind)
        if cls is None:
            raise CompileError(
                f"unknown primitive kind {node.kind!r}; "
                f"choose one of {sorted(REGISTRY)}",
                self._path,
            )
        try:
            params = cls.Params.model_validate(node.params or {})
        except ValidationError as e:
            errs = e.errors(include_url=False, include_context=False)
            raise CompileError(
                f"{node.kind}: {errs[0]['msg']} at {errs[0]['loc']}",
                self._path,
            ) from e
        try:
            return cls.compile(params, self.topology, self)
        except CompileError as e:
            if not e.path:
                raise CompileError(
                    e.raw_message, list(self._path), expected_kind=e.expected_kind,
                ) from e
            raise
        except (ValueError, TypeError) as e:
            raise CompileError(f"{node.kind}: {e}", self._path) from e

    def _check_kind(self, got: OutputKind | None, expected: OutputKind) -> None:
        if got is None:
            raise CompileError(
                "primitive returned no output_kind (internal bug)", self._path
            )
        if got == expected:
            return
        # Allow scalar_t to broadcast wherever scalar_field is expected.
        if expected == "scalar_field" and got == "scalar_t":
            return
        suggestion = _primitives_producing(expected)
        if suggestion:
            msg = (
                f"expected {expected}, got {got}; this slot needs a {expected} "
                f"primitive (e.g. {', '.join(suggestion)}) — "
                f"{got} primitives are spatial/per-LED and cannot be used here"
                if expected == "scalar_t" and got == "scalar_field"
                else f"expected {expected}, got {got}; "
                f"use one of: {', '.join(suggestion)}"
            )
        else:
            msg = f"expected {expected}, got {got}"
        raise CompileError(msg, self._path, expected_kind=expected)

    # ---- entry points ----

    def compile_layer(self, layer: LayerSpec) -> CompiledLayer:
        self._path.append("node")
        try:
            child = self._compile_node(layer.node)
            self._check_kind(child.output_kind, "rgb_field")
        finally:
            self._path.pop()
        return CompiledLayer(node=child, blend=layer.blend, opacity=layer.opacity)

    def compile_layers(self, layers: list[LayerSpec]) -> list[CompiledLayer]:
        compiled: list[CompiledLayer] = []
        for i, layer in enumerate(layers):
            self._path.append(f"layers[{i}]")
            try:
                compiled.append(self.compile_layer(layer))
            finally:
                self._path.pop()
        return compiled


@dataclass
class CompiledLayer:
    node: CompiledNode
    blend: str
    opacity: float


def compile_layers(
    layers: list[LayerSpec], topology: Topology
) -> list[CompiledLayer]:
    """Compile a list of LayerSpecs against a topology."""
    return Compiler(topology).compile_layers(layers)


# ---------------------------------------------------------------------------
# Output-kind helpers used by polymorphic combinators
# ---------------------------------------------------------------------------


def _primitives_producing(kind: str) -> list[str]:
    """Return the kinds of primitives that produce `kind` directly.

    Used by `_check_kind` to turn "expected scalar_t, got scalar_field" into
    actionable advice ("use audio_band, constant, envelope, lfo"). Polymorphic
    combinators are excluded from the suggestion — they could match, but the
    LLM does better with concrete leaf primitives in front of it.
    """
    return sorted(
        prim.kind
        for prim in REGISTRY.values()
        if prim.output_kind == kind
    )


def _broadcast_kind(a: str, b: str) -> str:
    """Compute the output kind of a binary scalar/RGB combinator.

    Rules:
      - rgb_field × scalar_*  → rgb_field (broadcast over channels)
      - rgb_field × rgb_field → rgb_field
      - scalar_field × scalar_t → scalar_field
      - scalar_t × scalar_t → scalar_t
      - palette anywhere → reject (use mix for palette lerp)
    """
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


def _broadcast_to_rgb(v: Any) -> np.ndarray:
    """Make `v` shaped (N, 3) — for binary ops where one side is rgb_field."""
    if isinstance(v, np.ndarray) and v.ndim == 2 and v.shape[1] == 3:
        return v
    if isinstance(v, np.ndarray):
        # scalar_field (N,)
        return v[:, None]
    # scalar_t — float
    return np.float32(v)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Primitives — scalar_t (time-only)
# ---------------------------------------------------------------------------


class _ConstantParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float = Field(0.0, description="Fixed scalar value")


class _CompiledConstant(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, value: float):
        self._v = float(value)

    def render(self, ctx: RenderContext) -> float:
        return self._v


@primitive
class Constant(Primitive):
    kind = "constant"
    output_kind = "scalar_t"
    summary = "Fixed scalar value (also produced by bare numeric literals)."
    Params = _ConstantParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledConstant(params.value)


class _LfoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shape: Literal["sin", "saw", "triangle", "pulse"] = Field(
        "sin", description="Waveform"
    )
    period_s: float = Field(1.0, gt=0.0, description="Cycle duration in seconds")
    phase: float = Field(0.0, description="Phase offset in cycles [0, 1)")
    duty: float = Field(
        0.5, ge=0.0, le=1.0,
        description="High fraction for shape=pulse",
    )


class _CompiledLfo(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, params: _LfoParams):
        self._shape = params.shape
        self._period = float(params.period_s)
        self._phase = float(params.phase)
        self._duty = float(params.duty)

    def render(self, ctx: RenderContext) -> float:
        phase = (ctx.t / self._period + self._phase) % 1.0
        s = self._shape
        if s == "sin":
            return 0.5 + 0.5 * math.sin(2.0 * math.pi * phase)
        if s == "saw":
            return phase
        if s == "triangle":
            return 1.0 - 2.0 * abs(phase - 0.5)
        # pulse
        return 1.0 if phase < self._duty else 0.0


@primitive
class Lfo(Primitive):
    kind = "lfo"
    output_kind = "scalar_t"
    summary = "Clock-driven oscillator. Reads ctx.t (master-speed-scaled)."
    Params = _LfoParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledLfo(params)


class _AudioBandParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    band: Literal["low", "mid", "high"] = Field(
        ...,
        description=(
            "Which rolling-normalised frequency band to read: "
            "low (20–250 Hz, kick/sub), mid (250 Hz–2 kHz, vocals/snare body), "
            "high (2–12 kHz, hats/cymbals)"
        ),
    )


class _CompiledAudioBand(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, band: str):
        self._field = f"{band}_norm"

    def render(self, ctx: RenderContext) -> float:
        if ctx.audio is None:
            return 0.0
        return float(getattr(ctx.audio, self._field, 0.0))


@primitive
class AudioBand(Primitive):
    kind = "audio_band"
    output_kind = "scalar_t"
    summary = (
        "Rolling-normalised frequency band (low/mid/high) — ~[0, 1] under "
        "typical room loudness; may exceed 1 when masters.audio_reactivity > 1 "
        "(clip downstream if needed). Always pick a band that matches the "
        "musical element you want; full-band loudness is intentionally not "
        "exposed."
    )
    Params = _AudioBandParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledAudioBand(params.band)


class _EnvelopeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(
        ...,
        description="A scalar_t node (audio_band, lfo, constant, …) to smooth",
    )
    attack_ms: float = Field(60.0, ge=0.0, description="Rise smoothing tau")
    release_ms: float = Field(250.0, ge=0.0, description="Fall smoothing tau")
    gain: float = Field(1.0, ge=0.0, description="Multiplier before clamping")
    curve: Literal["linear", "sqrt", "square"] = Field(
        "linear",
        description="Perceptual shape: sqrt = punchier on quiet input, square = lazier",
    )
    floor: float = Field(0.0, description="Output value when source is at minimum")
    ceiling: float = Field(1.0, description="Output value when source is at maximum")


class _CompiledEnvelope(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, child: CompiledNode, params: _EnvelopeParams):
        self._child = child
        self._attack_s = float(params.attack_ms) / 1000.0
        self._release_s = float(params.release_ms) / 1000.0
        self._gain = float(params.gain)
        self._curve = params.curve
        self._floor = float(params.floor)
        self._ceiling = float(params.ceiling)
        self._value = 0.0
        self._last_wall: float | None = None

    def render(self, ctx: RenderContext) -> float:
        raw = float(self._child.render(ctx))
        wall = ctx.wall_t
        if self._last_wall is None or wall < self._last_wall:
            self._value = raw
        else:
            dt = wall - self._last_wall
            tau = self._attack_s if raw > self._value else self._release_s
            if tau > 0.0 and dt > 0.0:
                k = math.exp(-dt / tau)
                self._value = self._value * k + raw * (1.0 - k)
            else:
                self._value = raw
        self._last_wall = wall

        v = self._value * self._gain
        if self._curve == "sqrt":
            v = math.sqrt(v) if v > 0.0 else 0.0
        elif self._curve == "square":
            v = v * v
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        return self._floor + (self._ceiling - self._floor) * v


@primitive
class Envelope(Primitive):
    kind = "envelope"
    output_kind = "scalar_t"
    summary = (
        "Smooth a scalar_t with asymmetric attack/release, then map "
        "[0,1] → [floor, ceiling] via gain + curve. Smoothing dt is "
        "wall-clock — a frozen pattern still breathes with the room."
    )
    Params = _EnvelopeParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_t", path="input")
        return _CompiledEnvelope(child, params)


class _ClampParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(..., description="A scalar_t/scalar_field node to clip")
    min: float = Field(0.0, description="Lower bound")
    max: float = Field(1.0, description="Upper bound")


class _CompiledClamp(CompiledNode):
    def __init__(self, child: CompiledNode, lo: float, hi: float):
        self._child = child
        self._lo = float(lo)
        self._hi = float(hi)
        self.output_kind = child.output_kind  # type: ignore[assignment]

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        if isinstance(v, np.ndarray):
            return np.clip(v, self._lo, self._hi)
        return _clip_scalar(float(v), self._lo, self._hi)


def _clip_scalar(v: float, lo: float, hi: float) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


@primitive
class Clamp(Primitive):
    kind = "clamp"
    output_kind = None
    summary = "Clip an input scalar/field to [min, max]. Output kind matches input."
    Params = _ClampParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_field", path="input")
        if params.min > params.max:
            raise CompileError(
                f"clamp.min ({params.min}) > clamp.max ({params.max})"
            )
        return _CompiledClamp(child, params.min, params.max)


class _RangeMapParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(..., description="A scalar_t/scalar_field node")
    in_min: float = Field(0.0)
    in_max: float = Field(1.0)
    out_min: float = Field(0.0)
    out_max: float = Field(1.0)


class _CompiledRangeMap(CompiledNode):
    def __init__(self, child: CompiledNode, params: _RangeMapParams):
        self._child = child
        self.output_kind = child.output_kind  # type: ignore[assignment]
        self._in_lo = float(params.in_min)
        self._in_hi = float(params.in_max)
        self._out_lo = float(params.out_min)
        self._out_hi = float(params.out_max)

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        denom = self._in_hi - self._in_lo
        t = 0.0 if denom == 0.0 else (v - self._in_lo) / denom
        return self._out_lo + (self._out_hi - self._out_lo) * t


@primitive
class RangeMap(Primitive):
    kind = "range_map"
    output_kind = None
    summary = "Linearly remap [in_min, in_max] → [out_min, out_max]."
    Params = _RangeMapParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_field", path="input")
        if params.in_min == params.in_max:
            raise CompileError("range_map: in_min must differ from in_max")
        return _CompiledRangeMap(child, params)


# ---------------------------------------------------------------------------
# Primitives — scalar_field (spatial)
# ---------------------------------------------------------------------------


class _WaveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: Literal["x", "y", "z"] = Field("x", description="Axis the pattern travels along")
    wavelength: float = Field(
        1.0, gt=0.0,
        description="Cycles per full normalised span; 1.0 = one cycle end-to-end",
    )
    speed: Any = Field(
        0.3,
        description="Cycles/sec (scalar_t). Sign sets direction.",
    )
    shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = Field(
        "sawtooth",
        description=(
            "How phase sweeps [0,1] each cycle. "
            "sawtooth = continuous linear flow — DEFAULT, smoothest color across "
            "LEDs, use for flowing/scrolling palettes (esp. cyclic ones like rainbow). "
            "cosine = up/down pulse — color *plateaus* at peaks and troughs because "
            "its derivative is zero there, so use this for breathing brightness with "
            "mono palettes, NOT for smooth color sweeps. "
            "pulse = hard on/off bands. "
            "gauss = single comet pulse per cycle."
        ),
    )
    softness: float = Field(
        1.0, ge=0.0, le=1.0,
        description="cosine only: 0 = hard bands, 1 = fully smooth",
    )
    width: float = Field(
        0.15, gt=0.0, le=2.0,
        description="gauss only: peak width in cycles",
    )
    cross_phase: tuple[float, float, float] = Field(
        (0.0, 0.0, 0.0),
        description=(
            "Per-axis phase offset in cycles per unit normalised position. "
            "(0, 0.15, 0) makes the top row lead the bottom by ~0.3 cycles."
        ),
    )


class _CompiledWave(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        params: _WaveParams,
        topology: Topology,
        speed_node: CompiledNode,
    ):
        axis_idx = "xyz".index(params.axis)
        self._u_axis = (
            topology.normalised_positions[:, axis_idx] + 1.0
        ) * 0.5
        self._wavelength = float(params.wavelength)
        self._shape = params.shape
        self._softness = float(params.softness)
        self._width = float(params.width)
        cp = np.asarray(params.cross_phase, dtype=np.float32)
        self._u_cross: np.ndarray | None = (
            topology.normalised_positions @ cp if np.any(cp) else None
        )
        self._speed = speed_node
        self._scratch = np.empty(topology.pixel_count, dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        u = self._u_axis / self._wavelength - speed * ctx.t
        if self._u_cross is not None:
            u = u + self._u_cross
        phase = u - np.floor(u)
        _apply_shape(phase, self._shape, self._softness, self._width, self._scratch)
        return self._scratch


@primitive
class Wave(Primitive):
    kind = "wave"
    output_kind = "scalar_field"
    summary = "1-D travelling pattern along an axis (replaces scroll/wave/gradient/chase)."
    Params = _WaveParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        return _CompiledWave(params, topology, speed)


class _RadialParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center: tuple[float, float, float] = Field(
        (0.0, 0.0, 0.0),
        description="Centre in normalised coords [-1, 1]",
    )
    speed: Any = Field(
        0.3,
        description="Cycles/sec (scalar_t); positive = rings travel outward",
    )
    wavelength: float = Field(
        0.5, gt=0.0,
        description="Cycles per unit normalised distance from centre",
    )
    shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = Field("cosine")
    softness: float = Field(1.0, ge=0.0, le=1.0)
    width: float = Field(0.15, gt=0.0, le=2.0)


class _CompiledRadial(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        params: _RadialParams,
        topology: Topology,
        speed_node: CompiledNode,
    ):
        c = np.asarray(params.center, dtype=np.float32)
        diff = topology.normalised_positions - c
        self._dist = np.sqrt(np.sum(diff * diff, axis=1)).astype(np.float32)
        self._wavelength = float(params.wavelength)
        self._shape = params.shape
        self._softness = float(params.softness)
        self._width = float(params.width)
        self._speed = speed_node
        self._scratch = np.empty(topology.pixel_count, dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        u = self._dist / self._wavelength - speed * ctx.t
        phase = u - np.floor(u)
        _apply_shape(phase, self._shape, self._softness, self._width, self._scratch)
        return self._scratch


@primitive
class Radial(Primitive):
    kind = "radial"
    output_kind = "scalar_field"
    summary = "Distance-from-point pattern. Rings expanding out, or pulses in."
    Params = _RadialParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        return _CompiledRadial(params, topology, speed)


class _GradientParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: Literal["x", "y", "z"] = "x"
    invert: bool = False


class _CompiledGradient(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(self, params: _GradientParams, topology: Topology):
        idx = "xyz".index(params.axis)
        ramp = (topology.normalised_positions[:, idx] + 1.0) * 0.5
        if params.invert:
            ramp = 1.0 - ramp
        self._ramp = ramp.astype(np.float32, copy=True)

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._ramp


@primitive
class Gradient(Primitive):
    kind = "gradient"
    output_kind = "scalar_field"
    summary = "Static linear ramp 0→1 along an axis."
    Params = _GradientParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledGradient(params, topology)


class _PositionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: Literal["x", "y", "z", "distance"] = Field(
        "x",
        description="Which normalised position component to surface; 'distance' = √(x²+y²+z²)",
    )


class _CompiledPosition(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(self, params: _PositionParams, topology: Topology):
        if params.axis == "distance":
            d = np.sqrt(np.sum(topology.normalised_positions ** 2, axis=1))
            d = d / max(d.max(), 1e-9)
            self._field = d.astype(np.float32, copy=True)
        else:
            idx = "xyz".index(params.axis)
            self._field = (
                (topology.normalised_positions[:, idx] + 1.0) * 0.5
            ).astype(np.float32, copy=True)

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._field


@primitive
class Position(Primitive):
    kind = "position"
    output_kind = "scalar_field"
    summary = "Raw normalised position component (mapped to [0, 1])."
    Params = _PositionParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPosition(params, topology)


_NOISE_LATTICE = 64


class _NoiseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: Any = Field(
        0.2,
        description="Field flow speed in lattice units per second (scalar_t)",
    )
    scale: Any = Field(
        0.5,
        description="Spatial scale; smaller = larger blobs (scalar_t)",
    )
    octaves: int = Field(1, ge=1, le=4, description="Octaves summed")
    seed: int = Field(0, description="Lattice RNG seed")


class _CompiledNoise(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        params: _NoiseParams,
        topology: Topology,
        speed_node: CompiledNode,
        scale_node: CompiledNode,
    ):
        rng = np.random.default_rng(params.seed)
        self._lattice = rng.random(
            (_NOISE_LATTICE, _NOISE_LATTICE), dtype=np.float32
        )
        self._octaves = int(params.octaves)
        self._x = topology.normalised_positions[:, 0].astype(np.float32, copy=True)
        self._y = topology.normalised_positions[:, 1].astype(np.float32, copy=True)
        self._speed = speed_node
        self._scale = scale_node
        self._scratch = np.empty(topology.pixel_count, dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        base_scale = float(self._scale.render(ctx))
        n = _NOISE_LATTICE
        out = self._scratch
        out.fill(0.0)
        amp = 1.0
        total_amp = 0.0
        for octave in range(self._octaves):
            scale = base_scale * (2 ** octave)
            ox = (self._x * scale * n + speed * ctx.t * n) % n
            oy = (self._y * scale * n + speed * ctx.t * 0.7 * n) % n
            x0 = np.floor(ox).astype(np.int32)
            y0 = np.floor(oy).astype(np.int32)
            x1 = (x0 + 1) % n
            y1 = (y0 + 1) % n
            fx = (ox - x0).astype(np.float32)
            fy = (oy - y0).astype(np.float32)
            v00 = self._lattice[y0, x0]
            v10 = self._lattice[y0, x1]
            v01 = self._lattice[y1, x0]
            v11 = self._lattice[y1, x1]
            v0 = v00 * (1.0 - fx) + v10 * fx
            v1 = v01 * (1.0 - fx) + v11 * fx
            out += amp * (v0 * (1.0 - fy) + v1 * fy)
            total_amp += amp
            amp *= 0.5
        if total_amp > 0.0:
            out /= total_amp
        return out


@primitive
class Noise(Primitive):
    kind = "noise"
    output_kind = "scalar_field"
    summary = (
        "Smooth 2D value-noise field flowing in time. Use as a scalar_field "
        "for blobby washes or to drive palette_lookup. Distinct from "
        "`sparkles` (discrete stamp grain)."
    )
    Params = _NoiseParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        scale = compiler.compile_child(params.scale, expect="scalar_t", path="scale")
        return _CompiledNoise(params, topology, speed, scale)


class _TrailParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(..., description="A scalar_field node to leave a trail behind")
    decay: Any = Field(
        2.0,
        description="Exponential decay per second of the trail brightness",
    )


class _CompiledTrail(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        topology: Topology,
        child: CompiledNode,
        decay_node: CompiledNode,
    ):
        self._child = child
        self._decay = decay_node
        self._buf = np.zeros(topology.pixel_count, dtype=np.float32)
        self._last_t: float | None = None

    def render(self, ctx: RenderContext) -> np.ndarray:
        new = self._child.render(ctx)
        if isinstance(new, float):
            new = np.full(self._buf.shape, float(new), dtype=np.float32)
        dt = (
            0.0
            if self._last_t is None or ctx.t < self._last_t
            else ctx.t - self._last_t
        )
        self._last_t = ctx.t
        decay = max(0.0, float(self._decay.render(ctx)))
        if dt > 0.0:
            self._buf *= float(np.exp(-decay * dt))
        np.maximum(self._buf, new, out=self._buf)
        return self._buf


@primitive
class Trail(Primitive):
    kind = "trail"
    output_kind = "scalar_field"
    summary = "Fading echo of an input scalar_field. Stateful, uses ctx.t."
    Params = _TrailParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_field", path="input")
        decay = compiler.compile_child(params.decay, expect="scalar_t", path="decay")
        return _CompiledTrail(topology, child, decay)


# ---------------------------------------------------------------------------
# Primitives — palette
# ---------------------------------------------------------------------------


class _PaletteNamedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        ...,
        description=(
            "rainbow, fire, ice, sunset, ocean, warm, white, black, "
            "or mono_<hex> for a single colour"
        ),
    )

    @model_validator(mode="after")
    def _validate_name(self) -> _PaletteNamedParams:
        n = self.name
        if n in NAMED_PALETTES:
            return self
        if n.startswith("mono_"):
            hex_to_rgb01(n[5:])
            return self
        raise ValueError(
            f"unknown palette {n!r}; choose one of {sorted(NAMED_PALETTES)} "
            f"or mono_<hex>"
        )


class _CompiledPaletteNamed(CompiledNode):
    output_kind: ClassVar[OutputKind] = "palette"

    def __init__(self, name: str):
        self._lut = _lut_from_named(name)

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._lut


@primitive
class PaletteNamed(Primitive):
    kind = "palette_named"
    output_kind = "palette"
    summary = (
        "Named LUT (rainbow / fire / ice / sunset / ocean / warm / white / "
        "black / mono_<hex>). `rainbow` is HSV-baked at uniform brightness; "
        "the others encode brightness on purpose. Bare strings in node "
        "fields are sugar for this primitive."
    )
    Params = _PaletteNamedParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPaletteNamed(params.name)


class _PaletteStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pos: float = Field(..., ge=0.0, le=1.0)
    color: str = Field(..., description="Hex colour at this stop")

    @model_validator(mode="after")
    def _check_color(self) -> _PaletteStop:
        hex_to_rgb01(self.color)
        return self


class _PaletteStopsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stops: list[_PaletteStop] = Field(..., min_length=2)


class _CompiledPaletteStops(CompiledNode):
    output_kind: ClassVar[OutputKind] = "palette"

    def __init__(self, params: _PaletteStopsParams):
        self._lut = _lut_from_stops([s.model_dump() for s in params.stops])

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._lut


@primitive
class PaletteStops(Primitive):
    kind = "palette_stops"
    output_kind = "palette"
    summary = "Custom palette from explicit (pos, color) stops (>= 2)."
    Params = _PaletteStopsParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPaletteStops(params)


class _PaletteHsvStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pos: float = Field(..., ge=0.0, le=1.0)
    hue: float = Field(
        ...,
        description=(
            "Hue in degrees: 0=red, 60=yellow, 120=green, 180=cyan, "
            "240=blue, 300=magenta. Values can exceed 360 (or go negative) "
            "for multi-cycle / direction-controlled sweeps; e.g. stops "
            "hue=0 and hue=360 walk the full chromatic circle the long way, "
            "hue=0 and hue=-180 go red->magenta->blue."
        ),
    )
    sat: float = Field(
        1.0, ge=0.0, le=1.0,
        description="Saturation (default 1 = pure colour, 0 = grey).",
    )
    val: float = Field(
        1.0, ge=0.0, le=1.0,
        description=(
            "HSV value/brightness (default 1 = max). Keep at 1 if you want "
            "the master + per-LED brightness controls to do all the dimming."
        ),
    )


class _PaletteHsvParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stops: list[_PaletteHsvStop] = Field(..., min_length=2)


class _CompiledPaletteHsv(CompiledNode):
    output_kind: ClassVar[OutputKind] = "palette"

    def __init__(self, params: _PaletteHsvParams):
        self._lut = _lut_from_hsv_stops([s.model_dump() for s in params.stops])

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._lut


@primitive
class PaletteHsv(Primitive):
    kind = "palette_hsv"
    output_kind = "palette"
    summary = (
        "Custom palette baked by HSV interpolation between hue stops. The "
        "LUT walks the chromatic surface so brightness stays uniform and "
        "complementary-colour midpoints stay saturated (no muddy/grey runs "
        "you'd get from RGB-space lerp). Prefer this over `palette_stops` "
        "whenever the palette is meant to encode colour and you want "
        "master/per-LED brightness controls to handle all the dimming."
    )
    Params = _PaletteHsvParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPaletteHsv(params)


# ---------------------------------------------------------------------------
# Primitives — rgb_field (layer leaves)
# ---------------------------------------------------------------------------


class _PaletteLookupParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scalar: Any = Field(
        ...,
        description="A scalar_field (or scalar_t broadcast) used to index the palette",
    )
    palette: Any = Field(
        ...,
        description="A palette node, or bare string for palette_named sugar",
    )
    brightness: Any = Field(
        1.0,
        description="Multiplier on output (scalar_t or scalar_field). Default 1.",
    )
    hue_shift: Any = Field(
        0.0,
        description="Rotate the palette LUT by N cycles (scalar_t or scalar_field).",
    )


class _CompiledPaletteLookup(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        scalar_node: CompiledNode,
        palette_node: CompiledNode,
        brightness_node: CompiledNode,
        hue_shift_node: CompiledNode,
    ):
        self._n = topology.pixel_count
        self._scalar = scalar_node
        self._palette = palette_node
        self._brightness = brightness_node
        self._hue_shift = hue_shift_node
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        lut = self._palette.render(ctx)
        s = self._scalar.render(ctx)
        if isinstance(s, float):
            s = np.full(self._n, s, dtype=np.float32)
        hue = self._hue_shift.render(ctx)
        if isinstance(hue, np.ndarray):
            t = (s + hue) % 1.0
        elif float(hue) != 0.0:
            t = (s + float(hue)) % 1.0
        else:
            t = np.clip(s, 0.0, 1.0)
        idx = np.minimum(
            (t * (LUT_SIZE - 1) + 0.5).astype(np.int32),
            LUT_SIZE - 1,
        )
        rgb = lut[idx]
        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            self._out[:] = rgb * bright[:, None]
        else:
            b = float(bright)
            if b == 1.0:
                self._out[:] = rgb
            else:
                self._out[:] = rgb * b
        return self._out


@primitive
class PaletteLookup(Primitive):
    kind = "palette_lookup"
    output_kind = "rgb_field"
    summary = (
        "Sample a palette LUT with a scalar field. Per-LED brightness and "
        "hue_shift are accepted (broadcast scalar_t for whole-frame variation)."
    )
    Params = _PaletteLookupParams

    @classmethod
    def compile(cls, params, topology, compiler):
        scalar = compiler.compile_child(params.scalar, expect="scalar_field", path="scalar")
        palette = compiler.compile_child(params.palette, expect="palette", path="palette")
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        hue_shift = compiler.compile_child(
            params.hue_shift, expect="scalar_field", path="hue_shift"
        )
        return _CompiledPaletteLookup(topology, scalar, palette, brightness, hue_shift)


class _SolidParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rgb: tuple[float, float, float] = Field(
        ..., description="Uniform colour as (r, g, b) in [0, 1]"
    )


class _CompiledSolid(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(self, topology: Topology, rgb: tuple[float, float, float]):
        self._out = np.tile(
            np.asarray(rgb, dtype=np.float32), (topology.pixel_count, 1)
        )

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._out


@primitive
class Solid(Primitive):
    kind = "solid"
    output_kind = "rgb_field"
    summary = "Uniform colour. Cheaper than palette_lookup for plain washes."
    Params = _SolidParams

    @classmethod
    def compile(cls, params, topology, compiler):
        rgb = tuple(_clip_scalar(float(v), 0.0, 1.0) for v in params.rgb)
        return _CompiledSolid(topology, rgb)  # type: ignore[arg-type]


class _SparklesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    palette: Any = Field(
        "mono_ffffff",
        description=(
            "Palette each stamp samples its colour from (palette node or "
            "bare string sugar). Default white = classic sparkle."
        ),
    )
    density: Any = Field(
        0.3,
        description=(
            "New sparkles per LED per second (scalar_t). Higher = busier "
            "grain; combine with `decay` to set steady-state coverage."
        ),
    )
    decay: Any = Field(
        2.0,
        description=(
            "Exponential brightness decay per second (scalar_t). Higher = "
            "shorter pixels; 0 keeps every stamp lit."
        ),
    )
    spread: Any = Field(
        0.0,
        description=(
            "Palette window width in [0, 1] each stamp samples from "
            "(wraps mod 1). 0 = single colour at `palette_center`, "
            "1 = full palette. (scalar_t)"
        ),
    )
    palette_center: Any = Field(
        0.5,
        description="Centre of the palette window in [0, 1] (scalar_t).",
    )
    brightness: Any = Field(
        1.0,
        description="Output multiplier (scalar_t or scalar_field).",
    )
    seed: int | None = Field(None, description="RNG seed; None = unpredictable.")


class _CompiledSparkles(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        palette_node: CompiledNode,
        density_node: CompiledNode,
        decay_node: CompiledNode,
        spread_node: CompiledNode,
        center_node: CompiledNode,
        brightness_node: CompiledNode,
        seed: int | None,
    ):
        self._n = topology.pixel_count
        self._palette = palette_node
        self._density = density_node
        self._decay = decay_node
        self._spread = spread_node
        self._center = center_node
        self._brightness = brightness_node
        self._rng = np.random.default_rng(seed)
        self._intensity = np.zeros(self._n, dtype=np.float32)
        self._palette_idx = np.zeros(self._n, dtype=np.float32)
        self._last_t: float | None = None
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        dt = (
            0.0
            if self._last_t is None or ctx.t < self._last_t
            else ctx.t - self._last_t
        )
        self._last_t = ctx.t
        density = max(0.0, float(self._density.render(ctx)))
        decay = max(0.0, float(self._decay.render(ctx)))
        if dt > 0.0:
            self._intensity *= float(np.exp(-decay * dt))
            expected = density * self._n * dt
            if expected > 0.0:
                n_new = int(self._rng.poisson(expected))
                if n_new > 0:
                    spread = _clip_scalar(
                        float(self._spread.render(ctx)), 0.0, 1.0
                    )
                    center = float(self._center.render(ctx))
                    half = spread * 0.5
                    samples = self._rng.uniform(-half, half, n_new) + center
                    samples = np.mod(samples, 1.0).astype(np.float32)
                    idxs = self._rng.integers(0, self._n, n_new)
                    self._intensity[idxs] = 1.0
                    self._palette_idx[idxs] = samples

        lut = self._palette.render(ctx)
        idx = np.minimum(
            (np.clip(self._palette_idx, 0.0, 1.0) * (LUT_SIZE - 1) + 0.5).astype(
                np.int32
            ),
            LUT_SIZE - 1,
        )
        rgb = lut[idx]
        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            bright_eff = self._intensity * bright.astype(np.float32, copy=False)
        else:
            bright_eff = self._intensity * float(bright)
        self._out[:] = rgb * bright_eff[:, None]
        return self._out


@primitive
class Sparkles(Primitive):
    kind = "sparkles"
    output_kind = "rgb_field"
    summary = (
        "Poisson-stamped twinkles with exponential decay. Layer leaf — each "
        "stamp samples a colour from the palette window (default white). "
        "Stack via blend modes (`add` / `screen` to overlay a base layer). "
        "Stateful, uses ctx.t (so freeze halts decay too)."
    )
    Params = _SparklesParams

    @classmethod
    def compile(cls, params, topology, compiler):
        palette = compiler.compile_child(
            params.palette, expect="palette", path="palette"
        )
        density = compiler.compile_child(
            params.density, expect="scalar_t", path="density"
        )
        decay = compiler.compile_child(
            params.decay, expect="scalar_t", path="decay"
        )
        spread = compiler.compile_child(
            params.spread, expect="scalar_t", path="spread"
        )
        center = compiler.compile_child(
            params.palette_center, expect="scalar_t", path="palette_center"
        )
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        return _CompiledSparkles(
            topology, palette, density, decay, spread, center, brightness,
            params.seed,
        )


# ---------------------------------------------------------------------------
# Combinators (polymorphic)
# ---------------------------------------------------------------------------


class _BinaryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: Any
    b: Any


class _CompiledBinary(CompiledNode):
    """Generic two-input combinator. `op` is a numpy ufunc-style callable.

    `output_kind` is resolved at compile against the children. Broadcasting:
    if either side is rgb_field, the other broadcasts over the channel axis.
    """

    def __init__(
        self,
        a: CompiledNode,
        b: CompiledNode,
        op,
        output_kind: str,
        n: int,
    ):
        self._a = a
        self._b = b
        self._op = op
        self.output_kind = output_kind  # type: ignore[assignment]
        self._n = n

    def render(self, ctx: RenderContext) -> Any:
        va = self._a.render(ctx)
        vb = self._b.render(ctx)
        if self.output_kind == "rgb_field":
            va = _broadcast_to_rgb(va)
            vb = _broadcast_to_rgb(vb)
        return self._op(va, vb)


def _make_binary(kind: str, op_name: str, op):
    summary = (
        f"Polymorphic {op_name}. Output kind resolved from inputs "
        f"(rgb_field × scalar broadcasts; palette inputs not allowed — use mix)."
    )

    class _Op(Primitive):
        pass

    _Op.kind = kind
    _Op.output_kind = None
    _Op.summary = summary
    _Op.Params = _BinaryParams
    _Op.__name__ = f"_Binary_{kind}"

    def _compile(cls, params, topology, compiler):
        # Polymorphic: kinds resolved from inputs, not fixed up front. We can't
        # use `compile_child(expect=...)` because that gates rgb_field out;
        # `_broadcast_kind` does the kind validation (and palette rejection).
        a = _compile_unconstrained(params.a, "a", compiler)
        b = _compile_unconstrained(params.b, "b", compiler)
        out_kind = _broadcast_kind(a.output_kind, b.output_kind)
        return _CompiledBinary(a, b, op, out_kind, topology.pixel_count)

    _Op.compile = classmethod(_compile)  # type: ignore[assignment]
    REGISTRY[kind] = _Op
    return _Op


def _np_add(a, b):
    return a + b


def _np_mul(a, b):
    return a * b


def _np_screen(a, b):
    return 1.0 - (1.0 - a) * (1.0 - b)


def _np_max(a, b):
    return np.maximum(a, b) if isinstance(a, np.ndarray) or isinstance(b, np.ndarray) else max(a, b)


def _np_min(a, b):
    return np.minimum(a, b) if isinstance(a, np.ndarray) or isinstance(b, np.ndarray) else min(a, b)


_make_binary("add", "addition", _np_add)
_make_binary("mul", "multiplication", _np_mul)
_make_binary("screen", "screen blend", _np_screen)
_make_binary("max", "elementwise max", _np_max)
_make_binary("min", "elementwise min", _np_min)


# ---- mix (polymorphic, palette-aware) --------------------------------------


class _MixParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: Any
    b: Any
    t: Any = Field(0.5, description="Lerp factor (scalar_t)")


class _CompiledMix(CompiledNode):
    def __init__(self, a: CompiledNode, b: CompiledNode, t: CompiledNode, output_kind: str):
        self._a = a
        self._b = b
        self._t = t
        self.output_kind = output_kind  # type: ignore[assignment]

    def render(self, ctx: RenderContext) -> Any:
        va = self._a.render(ctx)
        vb = self._b.render(ctx)
        u = float(self._t.render(ctx))
        u = _clip_scalar(u, 0.0, 1.0)
        if self.output_kind == "palette":
            return va * (1.0 - u) + vb * u
        if self.output_kind == "rgb_field":
            va = _broadcast_to_rgb(va)
            vb = _broadcast_to_rgb(vb)
        return va * (1.0 - u) + vb * u


@primitive
class Mix(Primitive):
    kind = "mix"
    output_kind = None
    summary = (
        "Polymorphic lerp(a, b, t). Works on matching scalar_t / scalar_field / "
        "rgb_field / palette pairs (palette × palette → palette is how you "
        "crossfade two colour schemes)."
    )
    Params = _MixParams

    @classmethod
    def compile(cls, params, topology, compiler):
        # `a` and `b` can be palette OR scalar/rgb. Try palette first; if that
        # fails, fall back to scalar_field expectation. Easiest: compile both,
        # then validate kinds together.
        a = _compile_unconstrained(params.a, "a", compiler)
        b = _compile_unconstrained(params.b, "b", compiler)
        t = compiler.compile_child(params.t, expect="scalar_t", path="t")
        if a.output_kind == "palette" and b.output_kind == "palette":
            return _CompiledMix(a, b, t, "palette")
        if a.output_kind == "palette" or b.output_kind == "palette":
            raise CompileError(
                "mix: cannot mix palette with non-palette; both `a` and `b` "
                "must be palette nodes for a palette lerp"
            )
        out = _broadcast_kind(a.output_kind, b.output_kind)
        return _CompiledMix(a, b, t, out)


def _compile_unconstrained(raw: Any, label: str, compiler: Compiler) -> CompiledNode:
    """Compile a child where the parent doesn't fix the kind up front.

    Used by `mix`: we accept palette × palette OR scalar/rgb pairs, so we
    can't pick an `expect` until both sides are compiled. We bypass the kind
    check and validate manually in the parent.
    """
    compiler._path.append(label)
    try:
        node = compiler._coerce_to_nodespec(raw)
        return compiler._compile_node(node)
    finally:
        compiler._path.pop()


# ---- remap, threshold ------------------------------------------------------


class _RemapParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any
    fn: Literal["sin", "abs", "sqrt", "pow", "step"] = "sin"
    arg: float = Field(2.0, description="Exponent for fn=pow; threshold for fn=step")


class _CompiledRemap(CompiledNode):
    def __init__(self, child: CompiledNode, fn: str, arg: float):
        self._child = child
        self._fn = fn
        self._arg = float(arg)
        self.output_kind = child.output_kind  # type: ignore[assignment]

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        is_arr = isinstance(v, np.ndarray)
        if self._fn == "sin":
            if is_arr:
                return np.sin(2.0 * np.pi * v) * 0.5 + 0.5
            return math.sin(2.0 * math.pi * v) * 0.5 + 0.5
        if self._fn == "abs":
            return np.abs(v) if is_arr else abs(v)
        if self._fn == "sqrt":
            if is_arr:
                return np.sqrt(np.clip(v, 0.0, None))
            return math.sqrt(max(0.0, v))
        if self._fn == "pow":
            if is_arr:
                return np.power(np.clip(v, 0.0, None), self._arg)
            return max(0.0, v) ** self._arg
        # step
        if is_arr:
            return (v >= self._arg).astype(np.float32)
        return 1.0 if v >= self._arg else 0.0


@primitive
class Remap(Primitive):
    kind = "remap"
    output_kind = None
    summary = "Apply a small fn to the input (sin / abs / sqrt / pow / step)."
    Params = _RemapParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_field", path="input")
        return _CompiledRemap(child, params.fn, params.arg)


class _ThresholdParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any
    t: float = Field(0.5, description="Cut-off; output is 1 when input >= t else 0")


class _CompiledThreshold(CompiledNode):
    def __init__(self, child: CompiledNode, t: float):
        self._child = child
        self._t = float(t)
        self.output_kind = child.output_kind  # type: ignore[assignment]

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        if isinstance(v, np.ndarray):
            return (v >= self._t).astype(np.float32)
        return 1.0 if v >= self._t else 0.0


@primitive
class Threshold(Primitive):
    kind = "threshold"
    output_kind = None
    summary = "Binary on/off at a cut-off."
    Params = _ThresholdParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_field", path="input")
        return _CompiledThreshold(child, params.t)


# ---------------------------------------------------------------------------
# Documentation generation
# ---------------------------------------------------------------------------


# Anchor recipes — small enough that the LLM picks up the pattern instantly,
# and they're the templates `EXAMPLE_TREES` / system prompt examples are
# built from.
EXAMPLE_TREES: dict[str, dict[str, Any]] = {
    "warm_drift": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 0.12, "wavelength": 1.5}},
            "palette": {
                "kind": "palette_stops",
                "params": {
                    "stops": [
                        {"pos": 0.0, "color": "#ff2000"},
                        {"pos": 0.5, "color": "#ff8000"},
                        {"pos": 1.0, "color": "#ffd060"},
                    ]
                },
            },
            "brightness": {
                "kind": "envelope",
                "params": {
                    "input": {"kind": "audio_band", "params": {"band": "low"}},
                    "attack_ms": 30,
                    "release_ms": 500,
                    "floor": 0.5,
                    "ceiling": 1.0,
                },
            },
        },
    },
    "fire_chase": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 1.5, "wavelength": 0.5}},
            "palette": "fire",
            "brightness": {
                "kind": "envelope",
                "params": {
                    "input": {"kind": "audio_band", "params": {"band": "low"}},
                    "attack_ms": 30,
                    "release_ms": 300,
                    "gain": 4.0,
                    "floor": 0.3,
                    "ceiling": 1.0,
                },
            },
        },
    },
    "pulse_red": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "constant", "params": {"value": 0.5}},
            "palette": "mono_ff0000",
            "brightness": {
                "kind": "lfo",
                "params": {"shape": "sin", "period_s": 0.8},
            },
        },
    },
    "sparkle_only": {
        "kind": "sparkles",
        "params": {"density": 0.04, "decay": 1.5, "seed": 7},
    },
    "axis_cross": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {
                "kind": "mul",
                "params": {
                    "a": {"kind": "wave", "params": {"axis": "x", "speed": 0.4, "shape": "cosine"}},
                    "b": {"kind": "wave", "params": {"axis": "y", "speed": 0.3, "shape": "cosine"}},
                },
            },
            "palette": "rainbow",
        },
    },
    "rainbow_sparkles": {
        "kind": "sparkles",
        "params": {
            "palette": "rainbow",
            "density": 3.0,
            "decay": 2.0,
            "spread": 1.0,
            "palette_center": 0.5,
        },
    },
    "chromatic_drift": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 0.2}},
            "palette": {
                "kind": "palette_hsv",
                "params": {"stops": [
                    {"pos": 0.0, "hue": 200.0},
                    {"pos": 1.0, "hue": 320.0},
                ]},
            },
        },
    },
}


ANTI_PATTERNS: list[str] = [
    "There is no top-level `bindings` — modulation lives ON the parameter as "
    "a node. To modulate brightness, set `palette_lookup.brightness` to an "
    "envelope/audio_band/lfo node, not a `bindings.brightness` block.",
    "`palette` is itself a node: bare strings (\"fire\") are sugar for "
    "`palette_named`. There is no `palette: \"red\"` — use `mono_ff0000`.",
    "`mix` is polymorphic; do not reach for a separate `palette_mix` — "
    "`mix(palette_a, palette_b, t)` is the palette crossfade.",
    "`mul(rgb_field, palette)` is rejected. Convert the palette to rgb_field "
    "first via `palette_lookup`.",
    "Discrete params (`axis`, `shape`, `band`, `direction`) are baked at compile "
    "time and cannot be modulated. Numeric params are `NumberOrNode` and accept "
    "either a literal or a scalar_t/scalar_field node.",
    "Audio is read via `audio_band` with band ∈ {low, mid, high} (rolling-"
    "normalised). Pick the band that matches the musical element you want to "
    "track (low=kick, mid=vocals/snare, high=hats). Wrap in `envelope` for "
    "smooth attack/release; raw `audio_band` is jagged on purpose.",
    "`mix.t` is the lerp factor — it is a `scalar_t` (one number per frame), "
    "not a `scalar_field`. Feed it `lfo`, `audio_band`, `envelope`, or a literal "
    "0..1 number; do NOT feed it `position`/`wave`/`noise` (those are per-LED).",
    "To split the install spatially (top half vs bottom half, etc.) use a "
    "`scalar_field` like `position` as the `palette_lookup.scalar` directly, or "
    "build it via `add`/`mul`/`mix` of two `scalar_field`s — `mix.t` cannot "
    "do per-LED splits because its blend factor is a single number.",
    "`wave.shape: cosine` *plateaus* color near peaks and troughs (its "
    "derivative is zero there), so dozens of adjacent LEDs end up the same "
    "colour and you get visible block artefacts on a smooth-palette sweep. "
    "Default is `sawtooth` — use it whenever you want flowing/scrolling colour. "
    "Pick `cosine` only when you actually want a breathing pulse on a mono "
    "palette (where the brightness up-down is the point and there's no "
    "colour gradient to band).",
]


def _kind_table_row(prim: type[Primitive]) -> str:
    pjson = json.dumps(
        _compact_params_schema(prim.Params), separators=(",", ":")
    )
    out_kind = prim.output_kind or "polymorphic"
    return f"  {prim.kind:18s} [{out_kind}]  {prim.summary}\n    params: {pjson}"


def _compact_params_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Compact JSON-schema-ish summary: per-field {type, default, enum, description}.

    Avoids the full pydantic dump (which inlines $defs and explodes the prompt
    with title/format/etc. fluff). The full schema is still available via
    `prim.Params.model_json_schema()` for the operator UI / GET /surface/primitives.
    """
    schema = model.model_json_schema()
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    out: dict[str, Any] = {}
    for name, field in props.items():
        entry: dict[str, Any] = {}
        if "type" in field:
            entry["type"] = field["type"]
        if "anyOf" in field:
            entry["any_of"] = [
                b.get("type", b.get("$ref", "any")) for b in field["anyOf"]
            ]
        if "enum" in field:
            entry["enum"] = field["enum"]
        if "default" in field:
            entry["default"] = field["default"]
        if name in required:
            entry["required"] = True
        if "description" in field:
            entry["desc"] = field["description"]
        out[name] = entry
    return out


def generate_docs(
    *,
    topology: Topology | None = None,
    audio_state: Any | None = None,
    engine_state: Any | None = None,
) -> str:
    """Build the prompt-ready CONTROL SURFACE block.

    `topology` / `audio_state` / `engine_state` are accepted for symmetry with
    callers that pass them through (and so future masters / palettes can read
    them), but the v1 docs are self-contained from the registry.
    """
    by_kind: dict[str, list[type[Primitive]]] = {
        "scalar_field": [],
        "scalar_t": [],
        "palette": [],
        "rgb_field": [],
        "polymorphic": [],
    }
    for prim in REGISTRY.values():
        key = prim.output_kind or "polymorphic"
        by_kind.setdefault(key, []).append(prim)

    sections: list[str] = []
    sections.append(
        "CONTROL SURFACE — primitives compose into a tree. Every node is "
        "{kind, params}. Numeric params accept either a literal or a node; "
        "discrete params (axis, shape, band, …) are literal-only. Bare numbers "
        "are sugar for `constant`; bare palette strings are sugar for "
        "`palette_named`. Strict validation: unknown keys fail with a "
        "structured error you can read on the next turn."
    )
    sections.append(
        "OUTPUT KINDS\n"
        "  scalar_field — per-LED scalar [0, 1] (spatial)\n"
        "  scalar_t     — single scalar/frame (time-only)\n"
        f"  palette      — {LUT_SIZE}-entry RGB LUT\n"
        "  rgb_field    — per-LED RGB; the layer leaf"
    )

    for header_kind, label in [
        ("scalar_field", "KIND: scalar_field"),
        ("scalar_t", "KIND: scalar_t"),
        ("palette", "KIND: palette"),
        ("rgb_field", "KIND: rgb_field"),
        ("polymorphic", "KIND: polymorphic combinators"),
    ]:
        prims = sorted(by_kind.get(header_kind, []), key=lambda p: p.kind)
        if not prims:
            continue
        rows = [_kind_table_row(p) for p in prims]
        sections.append(label + "\n" + "\n".join(rows))

    sections.append(
        "BLEND MODES (layer-level): normal, add, screen, multiply"
    )
    sections.append(
        "NAMED PALETTES (use as `palette: \"<name>\"` or via palette_named):\n"
        "  " + ", ".join(sorted(NAMED_PALETTES)) + ", mono_<hex>"
    )

    examples_block = "EXAMPLES\n"
    for name, tree in EXAMPLE_TREES.items():
        examples_block += f"  {name}: " + json.dumps(tree, separators=(",", ":")) + "\n"
    sections.append(examples_block.rstrip())

    sections.append(
        "ANTI-PATTERNS (the model keeps reaching for these — don't)\n"
        + "\n".join(f"  - {ap}" for ap in ANTI_PATTERNS)
    )

    return "\n\n".join(sections)


def primitives_json() -> dict[str, Any]:
    """JSON catalogue for `GET /surface/primitives`.

    Returns full pydantic model schemas, *without* shrinking — the operator
    UI builds form fields from these and doesn't share the agent's token
    budget.
    """
    out: dict[str, Any] = {}
    for kind, prim in REGISTRY.items():
        out[kind] = {
            "kind": kind,
            "output_kind": prim.output_kind or "polymorphic",
            "summary": prim.summary,
            "params_schema": prim.Params.model_json_schema(),
        }
    return out
