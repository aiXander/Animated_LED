"""Disk persistence for effects.

Layout:
    config/effects/<slug>/effect.py     ← Python source, real .py file
    config/effects/<slug>/effect.yaml   ← metadata + param schema + current values

Bundled examples live under `src/ledctl/surface/examples/<slug>/...` and
copy themselves into `config/effects/` on first boot if not already present.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .schema import WriteEffectArgs

EXAMPLES_DIR = Path(__file__).parent / "examples"
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def _validate_name(name: str) -> str:
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"effect name {name!r} must be snake_case [a-z][a-z0-9_]{{0,40}}"
        )
    return name


@dataclass
class StoredEffect:
    name: str
    summary: str
    source: str
    param_schema: list[dict[str, Any]]
    param_values: dict[str, Any]
    starred: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0


class EffectStore:
    """Filesystem CRUD for `config/effects/`.

    Pure I/O — no engine state. The runtime calls into this layer to load/save.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- listing / read ---- #

    def list(self) -> list[str]:
        names: list[str] = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and (d / "effect.py").is_file() and (d / "effect.yaml").is_file():
                names.append(d.name)
        return names

    def exists(self, name: str) -> bool:
        d = self.root / _validate_name(name)
        return (d / "effect.py").is_file() and (d / "effect.yaml").is_file()

    def load(self, name: str) -> StoredEffect:
        _validate_name(name)
        d = self.root / name
        py = d / "effect.py"
        yml = d / "effect.yaml"
        if not py.is_file() or not yml.is_file():
            raise FileNotFoundError(f"no saved effect {name!r} at {d}")
        source = py.read_text()
        meta = yaml.safe_load(yml.read_text()) or {}
        return StoredEffect(
            name=name,
            summary=str(meta.get("summary", "")),
            source=source,
            param_schema=list(meta.get("params") or []),
            param_values=dict(meta.get("param_values") or {}),
            starred=bool(meta.get("starred", False)),
            created_at=float(meta.get("created_at", 0.0)),
            updated_at=float(meta.get("updated_at", 0.0)),
        )

    # ---- write ---- #

    def save(
        self,
        *,
        args: WriteEffectArgs,
        param_values: dict[str, Any] | None = None,
    ) -> StoredEffect:
        _validate_name(args.name)
        d = self.root / args.name
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        param_schema = [p.model_dump() for p in args.params]
        # Default values come from the schema; merge in any overrides (used
        # when v1.1 lands param auto-merge).
        defaults = {p["key"]: p.get("default") for p in param_schema}
        if param_values:
            for k, v in param_values.items():
                if k in defaults:
                    defaults[k] = v
        existed = (d / "effect.yaml").exists()
        created_at = now
        if existed:
            try:
                old = yaml.safe_load((d / "effect.yaml").read_text()) or {}
                created_at = float(old.get("created_at", now))
            except Exception:
                pass
        meta: dict[str, Any] = {
            "name": args.name,
            "summary": args.summary,
            "source": "agent",
            "created_at": created_at,
            "updated_at": now,
            "params": param_schema,
            "param_values": defaults,
        }
        (d / "effect.py").write_text(args.code)
        (d / "effect.yaml").write_text(
            yaml.safe_dump(meta, sort_keys=False, default_flow_style=False)
        )
        return StoredEffect(
            name=args.name,
            summary=args.summary,
            source=args.code,
            param_schema=param_schema,
            param_values=defaults,
            created_at=created_at,
            updated_at=now,
        )

    def save_values(self, name: str, values: dict[str, Any]) -> None:
        """Persist current operator values into effect.yaml (no source change)."""
        _validate_name(name)
        d = self.root / name
        yml = d / "effect.yaml"
        if not yml.is_file():
            return
        try:
            meta = yaml.safe_load(yml.read_text()) or {}
        except Exception:
            return
        old = dict(meta.get("param_values") or {})
        old.update(values)
        meta["param_values"] = old
        meta["updated_at"] = time.time()
        yml.write_text(yaml.safe_dump(meta, sort_keys=False, default_flow_style=False))

    def delete(self, name: str) -> bool:
        _validate_name(name)
        d = self.root / name
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True

    def rename(self, old: str, new: str) -> StoredEffect:
        """Rename a saved effect on disk: move the directory + rewrite the
        `name` field in effect.yaml. Returns the freshly-loaded record."""
        _validate_name(old)
        _validate_name(new)
        if old == new:
            return self.load(old)
        src = self.root / old
        dst = self.root / new
        if not src.is_dir():
            raise FileNotFoundError(f"no saved effect {old!r} at {src}")
        if dst.exists():
            raise ValueError(f"an effect named {new!r} already exists")
        src.rename(dst)
        yml = dst / "effect.yaml"
        try:
            meta = yaml.safe_load(yml.read_text()) or {}
        except Exception:
            meta = {}
        meta["name"] = new
        meta["updated_at"] = time.time()
        yml.write_text(yaml.safe_dump(meta, sort_keys=False, default_flow_style=False))
        return self.load(new)

    # ---- bundled examples ---- #

    def install_examples_if_missing(self) -> list[str]:
        """Copy bundled examples into the on-disk store if absent.

        Returns names of newly installed effects.
        """
        installed: list[str] = []
        if not EXAMPLES_DIR.is_dir():
            return installed
        for sub in sorted(EXAMPLES_DIR.iterdir()):
            if not sub.is_dir():
                continue
            name = sub.name
            if not _NAME_PATTERN.match(name):
                continue
            if self.exists(name):
                continue
            src_py = sub / "effect.py"
            src_yml = sub / "effect.yaml"
            if not src_py.is_file() or not src_yml.is_file():
                continue
            dest = self.root / name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_py, dest / "effect.py")
            shutil.copyfile(src_yml, dest / "effect.yaml")
            installed.append(name)
        return installed
