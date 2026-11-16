"""YAML load/dump through libyaml when PyYAML was built with it.

`yaml.safe_load` / `yaml.safe_dump` always use the pure-Python parser, which
is roughly ten times slower: 2.5 ms per effect file on a laptop, far more on
the Pi, and those calls sit on paths the operator triggers mid-show (slider
saves, library listing, playlist loads).
"""

from __future__ import annotations

from typing import Any

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_Dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


def load(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a safe loader either way


def dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False)


__all__ = ["dump", "load"]
