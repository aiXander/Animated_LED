"""Polymorphic combinators: mix / mul / add / screen / max / min / remap / threshold.

Output kind is resolved at compile time from the children — `_broadcast_kind`
implements the rules and rejects palette-in-scalar/rgb slots."""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ...masters import RenderContext
from ..registry import (
    REGISTRY,
    CompiledNode,
    Primitive,
    broadcast_kind,
    broadcast_to_rgb,
    primitive,
)
from ..shapes import clip_scalar

# --- generic binary scaffolding ---------------------------------------------


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
            va = broadcast_to_rgb(va)
            vb = broadcast_to_rgb(vb)
        return self._op(va, vb)


def _make_binary(kind: str, op_name: str, op):
    from ..compiler import compile_unconstrained

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
        a = compile_unconstrained(params.a, "a", compiler)
        b = compile_unconstrained(params.b, "b", compiler)
        out_kind = broadcast_kind(a.output_kind, b.output_kind)
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
    return (
        np.maximum(a, b)
        if isinstance(a, np.ndarray) or isinstance(b, np.ndarray)
        else max(a, b)
    )


def _np_min(a, b):
    return (
        np.minimum(a, b)
        if isinstance(a, np.ndarray) or isinstance(b, np.ndarray)
        else min(a, b)
    )


_make_binary("add", "addition", _np_add)
_make_binary("mul", "multiplication", _np_mul)
_make_binary("screen", "screen blend", _np_screen)
_make_binary("max", "elementwise max", _np_max)
_make_binary("min", "elementwise min", _np_min)


# --- mix ---------------------------------------------------------------------


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
        u = clip_scalar(u, 0.0, 1.0)
        if self.output_kind == "palette":
            return va * (1.0 - u) + vb * u
        if self.output_kind == "rgb_field":
            va = broadcast_to_rgb(va)
            vb = broadcast_to_rgb(vb)
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
        from ..compiler import CompileError, compile_unconstrained

        a = compile_unconstrained(params.a, "a", compiler)
        b = compile_unconstrained(params.b, "b", compiler)
        t = compiler.compile_child(params.t, expect="scalar_t", path="t")
        if a.output_kind == "palette" and b.output_kind == "palette":
            return _CompiledMix(a, b, t, "palette")
        if a.output_kind == "palette" or b.output_kind == "palette":
            raise CompileError(
                "mix: cannot mix palette with non-palette; both `a` and `b` "
                "must be palette nodes for a palette lerp"
            )
        out = broadcast_kind(a.output_kind, b.output_kind)
        return _CompiledMix(a, b, t, out)


# --- remap -------------------------------------------------------------------


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
        child = compiler.compile_child(
            params.input, expect="scalar_field", path="input"
        )
        return _CompiledRemap(child, params.fn, params.arg)


# --- threshold ---------------------------------------------------------------


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
        child = compiler.compile_child(
            params.input, expect="scalar_field", path="input"
        )
        return _CompiledThreshold(child, params.t)
