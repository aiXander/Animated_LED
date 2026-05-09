"""Compile LLM-authored Python source into an Effect class.

The threat model is "LLM typo," not "malicious input" (see surface_v2_design_plan.md
§14). We:

  - reject `import` / `from … import …` at the AST level — everything the LLM
    needs is already in the runtime namespace,
  - run with stripped builtins (no `eval`, `open`, `__import__`, …),
  - cap the source size at 8 KB so a runaway response can't OOM the Pi,
  - extract the single `Effect` subclass defined at module top level.

There is **no per-frame sandbox cost**: AST scan + `compile()` happen once at
`write_effect` time. Hot-path render is just method calls.
"""

from __future__ import annotations

import ast
import builtins
import types
from typing import Any

from .base import Effect

MAX_SOURCE_BYTES = 8 * 1024


_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "int", "isinstance", "issubclass", "len", "list", "map", "max", "min",
    "pow", "range", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip", "type",
    # Exception types the LLM might use defensively:
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "ZeroDivisionError", "ArithmeticError",
    # Core sentinels:
    "True", "False", "None", "object", "property", "staticmethod", "classmethod",
    "super", "iter", "next", "hasattr", "getattr",  # getattr ok: no __dunder
)


def _build_safe_builtins() -> dict[str, Any]:
    out = {n: getattr(builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(builtins, n)}
    # `__build_class__` is the internal hook CPython uses when executing a
    # `class …:` statement. Without it `exec`'d code can't define classes.
    # It's not user-callable in any meaningful way — exposing it doesn't widen
    # the threat surface vs. the AST scan blocking imports already.
    out["__build_class__"] = builtins.__build_class__
    # `__name__` is read by some decorators / dataclass machinery.
    out["__name__"] = "ledctl_effect"
    return out


SAFE_BUILTINS = _build_safe_builtins()


_FORBIDDEN_NODES = (ast.Import, ast.ImportFrom)


class EffectCompileError(RuntimeError):
    """Raised when compile_effect rejects a source — surface to the LLM."""


def compile_effect(
    source: str,
    name: str,
    runtime_namespace: dict[str, Any],
) -> type[Effect]:
    """Validate, compile, and load a single Effect subclass from source.

    `runtime_namespace` is the dict of names to inject as module globals (np,
    Effect, helpers, rng, log, constants). Built by `runtime.build_runtime_namespace`.
    """
    if not isinstance(source, str):
        raise EffectCompileError("source must be a string")
    blen = len(source.encode("utf-8"))
    if blen > MAX_SOURCE_BYTES:
        raise EffectCompileError(
            f"source too long ({blen} bytes > {MAX_SOURCE_BYTES})"
        )

    try:
        tree = ast.parse(source, filename=f"<llm:{name}>")
    except SyntaxError as e:
        raise EffectCompileError(f"syntax error: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise EffectCompileError(
                "imports are forbidden — the runtime API is already in scope "
                "(np, Effect, hex_to_rgb, hsv_to_rgb, lerp, clip01, gauss, "
                "pulse, tri, wrap_dist, palette_lerp, named_palette, rng, log, "
                "PI, TAU, LUT_SIZE)"
            )
        # Dunder access (`x.__class__`, `obj.__globals__`, …) is reserved.
        # Effects don't need it; LLMs sometimes wander into it from training
        # data and it's a clean tertiary attack surface to close. Allow the
        # one we DO need (`__name__`) since some helpers read it.
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__") and attr != "__name__":
                raise EffectCompileError(
                    f"dunder attribute access disallowed: .{attr}"
                )

    try:
        code = compile(tree, f"<llm:{name}>", "exec")
    except SyntaxError as e:
        raise EffectCompileError(f"compile error: {e}") from e

    mod = types.ModuleType(f"effect_{name}")
    mod.__dict__.update(runtime_namespace)
    mod.__dict__["__builtins__"] = dict(SAFE_BUILTINS)
    try:
        exec(code, mod.__dict__)  # noqa: S102 — sandbox, see module docstring
    except Exception as e:
        raise EffectCompileError(f"module load failed: {type(e).__name__}: {e}") from e

    classes = [
        v
        for v in mod.__dict__.values()
        if isinstance(v, type)
        and issubclass(v, Effect)
        and v is not Effect
        and getattr(v, "__module__", None) == mod.__name__
    ]
    if len(classes) != 1:
        raise EffectCompileError(
            f"expected exactly one Effect subclass at module top level, got {len(classes)}"
        )
    return classes[0]
