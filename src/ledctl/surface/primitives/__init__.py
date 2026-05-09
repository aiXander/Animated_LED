"""Primitive registration entry-point.

Importing this subpackage causes every primitive's `@primitive` decorator to
run, populating the global `REGISTRY`. Add a new file here and import it
below — the doc generator and REST primitive catalogue pick it up
automatically.
"""

from __future__ import annotations

from . import (  # noqa: F401  (import for registration side-effect)
    combinators,
    palette,
    particles,
    recipes,  # noqa: F401  (registers compound recipes)
    rgb_field,
    scalar_field,
    scalar_t,
)
