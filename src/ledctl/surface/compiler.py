"""Walker that turns a NodeSpec tree into CompiledNodes against a Topology."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..topology import Topology
from .registry import (
    REGISTRY,
    CompiledNode,
    OutputKind,
    primitives_producing,
)
from .spec import LayerSpec, NodeSpec


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


@dataclass
class CompiledLayer:
    node: CompiledNode
    blend: str
    opacity: float


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
                raise CompileError(
                    _format_nodespec_error(e, raw), self._path
                ) from e
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
                    e.raw_message,
                    list(self._path),
                    expected_kind=e.expected_kind,
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
        if expected == "scalar_field" and got == "scalar_t":
            return
        suggestion = primitives_producing(expected)
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


def compile_layers(
    layers: list[LayerSpec], topology: Topology
) -> list[CompiledLayer]:
    """Compile a list of LayerSpecs against a topology."""
    return Compiler(topology).compile_layers(layers)


def compile_unconstrained(raw: Any, label: str, compiler: Compiler) -> CompiledNode:
    """Compile a child where the parent doesn't fix the kind up front.

    Used by `mix` (palette × palette OR scalar/rgb pairs) and the polymorphic
    binary combinators. We bypass the kind check and validate manually in
    the parent.
    """
    compiler._path.append(label)
    try:
        node = compiler._coerce_to_nodespec(raw)
        return compiler._compile_node(node)
    finally:
        compiler._path.pop()


_compile_unconstrained = compile_unconstrained
