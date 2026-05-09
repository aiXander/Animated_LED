"""The single LED control surface.

One package owns the entire effect / palette / modulator vocabulary.
Everything the engine needs to render a layer, the agent needs to describe
a layer, and the operator UI needs to build a form lives here:

  - colour utilities (`shapes.hex_to_rgb01`)
  - shape utilities (cosine / sawtooth / pulse / gauss as 1-D pure fns)
  - the primitive registry (`registry.REGISTRY`, `registry.primitive`)
  - every primitive's Params + compile() under `primitives/`
  - named palettes (`palettes.NAMED_PALETTES`)
  - the spec types (`spec.NodeSpec`, `spec.LayerSpec`, `spec.UpdateLedsSpec`)
  - the compiler (`compiler.Compiler`, `compile_layers`)
  - `docs.generate_docs()` → the prompt-ready CONTROL SURFACE block
  - example trees + anti-patterns the LLM is taught from

Adding a new visual idea is a one-place change: write a `@primitive` class
in the appropriate file under `primitives/`, re-export nothing, the agent
prompt and operator UI both pick it up via `generate_docs()` /
`GET /surface/primitives` automatically.

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

This package never imports from `engine.py` or `mixer.py` — they import
us — so the dependency direction stays clean.
"""

from __future__ import annotations

from . import primitives as _primitives  # noqa: F401  (registers all leaves)
from .compiler import (  # noqa: F401
    CompiledLayer,
    CompileError,
    Compiler,
    _compile_unconstrained,
    _format_nodespec_error,
    compile_layers,
    compile_unconstrained,
)
from .docs import (  # noqa: F401
    ANTI_PATTERNS,
    EXAMPLE_TREES,
    _compact_params_schema,
    _kind_table_row,
    generate_docs,
    primitives_json,
)
from .palettes import (  # noqa: F401
    LUT_SIZE,
    NAMED_PALETTES,
    _bake_lut,
    _lut_from_hsv_stops,
    _lut_from_named,
    _lut_from_stops,
    set_lut_size,
)
from .registry import (  # noqa: F401
    REGISTRY,
    CompiledNode,
    OutputKind,
    Primitive,
    _broadcast_kind,
    _broadcast_to_rgb,
    _primitives_producing,
    broadcast_kind,
    broadcast_to_rgb,
    primitive,
    primitives_producing,
)

# Order matters: shapes/palettes/spec/registry first (they're foundation),
# then the compiler (needs spec + registry), then primitives (each calls
# `@primitive` at import time so the REGISTRY is populated), then docs.
from .shapes import (  # noqa: F401  (public API re-export)
    _apply_shape,
    _clip_scalar,
    _hsv_to_rgb01,
    apply_shape,
    clip_scalar,
    hex_to_rgb01,
    hsv_to_rgb01,
)
from .spec import LayerSpec, NodeSpec, UpdateLedsSpec  # noqa: F401

__all__ = [
    # spec
    "LayerSpec",
    "NodeSpec",
    "UpdateLedsSpec",
    # registry
    "REGISTRY",
    "CompiledNode",
    "OutputKind",
    "Primitive",
    "primitive",
    # compiler
    "CompileError",
    "CompiledLayer",
    "Compiler",
    "compile_layers",
    # palettes
    "LUT_SIZE",
    "NAMED_PALETTES",
    "set_lut_size",
    # docs / catalogue
    "EXAMPLE_TREES",
    "ANTI_PATTERNS",
    "generate_docs",
    "primitives_json",
    # utilities
    "hex_to_rgb01",
]
