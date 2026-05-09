"""Sandbox compile_effect — AST scan + restricted builtins + extraction."""

from __future__ import annotations

import pytest

from ledctl.surface.runtime import build_runtime_namespace
from ledctl.surface.sandbox import (
    MAX_SOURCE_BYTES,
    EffectCompileError,
    compile_effect,
)


def _ns() -> dict:
    return build_runtime_namespace("test_effect")


def test_imports_rejected():
    src = "import os\nclass X(Effect): pass\n"
    with pytest.raises(EffectCompileError, match="imports are forbidden"):
        compile_effect(src, "x", _ns())


def test_from_imports_rejected():
    src = "from os import path\nclass X(Effect): pass\n"
    with pytest.raises(EffectCompileError, match="imports are forbidden"):
        compile_effect(src, "x", _ns())


def test_size_cap():
    src = "class X(Effect):\n    pass\n" + ("# pad\n" * 5000)
    with pytest.raises(EffectCompileError, match="source too long"):
        compile_effect(src, "x", _ns())


def test_normal_class_accepted():
    src = """\
class MyEffect(Effect):
    def init(self, ctx):
        self.scratch = np.zeros(ctx.n, dtype=np.float32)

    def render(self, ctx):
        self.out[:] = 0.0
        return self.out
"""
    cls = compile_effect(src, "my_effect", _ns())
    assert cls.__name__ == "MyEffect"


def test_zero_classes_rejected():
    src = "x = 1\n"
    with pytest.raises(EffectCompileError, match="exactly one Effect subclass"):
        compile_effect(src, "x", _ns())


def test_two_classes_rejected():
    src = """\
class A(Effect):
    pass

class B(Effect):
    pass
"""
    with pytest.raises(EffectCompileError, match="exactly one Effect subclass"):
        compile_effect(src, "x", _ns())


def test_safe_builtins_eval_blocked():
    # `eval` is not in safe builtins, so calling it should raise NameError
    # at exec time -> wrapped into EffectCompileError by compile_effect.
    src = "X = eval\nclass MyEffect(Effect):\n    pass\n"
    with pytest.raises(EffectCompileError, match="module load failed"):
        compile_effect(src, "x", _ns())


def test_open_blocked():
    src = "X = open\nclass MyEffect(Effect):\n    pass\n"
    with pytest.raises(EffectCompileError, match="module load failed"):
        compile_effect(src, "x", _ns())


def test_max_source_bytes_constant_8k():
    assert MAX_SOURCE_BYTES == 8 * 1024
