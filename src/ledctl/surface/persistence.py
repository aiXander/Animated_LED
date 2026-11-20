"""Disk persistence for effects.

Layout:
    config/effects/star/<slug>/effect.py     ← Python source, real .py file
    config/effects/star/<slug>/effect.yaml   ← metadata + param schema + current values

`config/effects/double-line/` beside it is a frozen copy of the effects written
for the old rectangle rig. Nothing loads it.

Bundled examples live under `src/ledctl/surface/examples/<slug>/...` and
copy themselves into the library on every start if not already present.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import yamlio
from .schema import WriteEffectArgs

log = logging.getLogger(__name__)

EXAMPLES_DIR = Path(__file__).parent / "examples"
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def write_atomic(path: Path, text: str) -> None:
    """Write-then-rename, so a reader on another thread (the library listing,
    a playlist step, the deferred value saver) never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


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
    """Filesystem CRUD for one library folder (`config/effects/star/`).

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
        meta = yamlio.load(yml.read_text()) or {}
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
                old = yamlio.load((d / "effect.yaml").read_text()) or {}
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
        write_atomic(d / "effect.py", args.code)
        write_atomic(d / "effect.yaml", yamlio.dump(meta))
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
            meta = yamlio.load(yml.read_text()) or {}
        except Exception:
            return
        old = dict(meta.get("param_values") or {})
        merged = {**old, **values}
        if merged == old:
            # Nothing was tuned (a promote, a knob put back): leave the file
            # alone. The library is tracked in git, and a rewrite that only
            # bumps `updated_at` is a change to commit and a pull to refuse.
            return
        meta["param_values"] = merged
        meta["updated_at"] = time.time()
        write_atomic(yml, yamlio.dump(meta))

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
            meta = yamlio.load(yml.read_text()) or {}
        except Exception:
            meta = {}
        meta["name"] = new
        meta["updated_at"] = time.time()
        write_atomic(yml, yamlio.dump(meta))
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


class DeferredValueSaver:
    """Write slider tweaks to disk off the event loop, at most every `delay` s.

    A knob drag PATCHes many times a second. Saving each one inline meant a
    YAML read + write on the render thread per PATCH, i.e. dropped frames for
    as long as the operator kept dragging. Here a PATCH only records the
    newest values per effect; one worker-thread write follows `delay` seconds
    after the first unsaved change, so a tuning session costs one write per
    effect every `delay` seconds and nothing is written while idle.

    Until that write the disk is behind the running layer: whatever reads or
    rewrites an effect file flushes first. A power cut loses at most the last
    `delay` seconds of tuning; a clean shutdown flushes.

    Event loop only, except `flush`, which any thread may call.
    """

    def __init__(self, store: EffectStore, delay: float = 10.0):
        self.store = store
        self.delay = float(delay)
        self._pending: dict[str, dict[str, Any]] = {}
        # Values whose delay has passed, waiting for a writer. One shared
        # dict, so whichever writer runs next takes the newest values: a
        # queued worker can never write an older snapshot after a flush.
        self._due: dict[str, dict[str, Any]] = {}
        self._due_lock = threading.Lock()
        self._timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()
        self._write_lock = threading.Lock()

    def schedule(self, name: str, values: dict[str, Any]) -> None:
        self._pending[name] = dict(values)
        if self._timer is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(self.delay, self._fire)

    def _fire(self) -> None:
        self._timer = None
        if not self._pending:
            return
        self._release()
        task = asyncio.create_task(asyncio.to_thread(self._write))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def flush(self) -> None:
        """Write everything pending now, in the calling thread. Used before an
        operation that rewrites the same files (save, rename, delete, star)
        and on shutdown."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._release()
        self._write()

    async def flush_async(self) -> None:
        """`flush` for a request handler: the file write runs on a worker thread."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._release()
        await asyncio.to_thread(self._write)

    def _release(self) -> None:
        pending, self._pending = self._pending, {}
        with self._due_lock:
            self._due.update(pending)

    def _write(self) -> None:
        with self._write_lock:
            with self._due_lock:
                due, self._due = self._due, {}
            for name, values in due.items():
                try:
                    self.store.save_values(name, values)
                except Exception:
                    log.exception("could not save param values for %r", name)


async def install_from_library(
    runtime: Any, store: EffectStore, value_saver: DeferredValueSaver | None,
    slot: str, name: str, **place: Any,
) -> None:
    """Load library effect `name` into `slot` ("live" crossfades, "preview"
    cuts). `place` is `install_layer_async`'s blend / opacity / index / replace.

    The one path for the load endpoints, the playlist and the MIDI pads: the
    file read and the compile run on worker threads, so the render loop keeps
    ticking. Raises FileNotFoundError (not in the library) or the compile's
    EffectCompileError / ValueError.
    """
    if value_saver is not None:
        # Knob tuning from the last few seconds may not be on disk yet.
        await value_saver.flush_async()
    stored = await asyncio.to_thread(store.load, name)
    await runtime.install_layer_async(
        slot, name=stored.name, summary=stored.summary, source=stored.source,
        param_schema=stored.param_schema, param_values=stored.param_values, **place,
    )
