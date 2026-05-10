"""Single-canonical playlist that drives the LIVE composition.

When started, an asyncio task walks the entries in order, each playing for
its declared `play_seconds` before crossfading to the next. Effects come
from the on-disk library (``config/effects/<name>/``). The playlist itself
persists to ``config/playlist.yaml`` so it survives restarts.

Out of scope for now: multiple playlists, manual jump-to-entry, shuffle.
The user said: "just a single, canonical playlist for now."
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .surface.persistence import EffectStore
from .surface.runtime import Runtime
from .surface.sandbox import EffectCompileError

log = logging.getLogger(__name__)

DEFAULT_PLAY_SECONDS = 120.0
MIN_PLAY_SECONDS = 5.0


@dataclass
class PlaylistEntry:
    name: str
    play_seconds: float = DEFAULT_PLAY_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "play_seconds": float(self.play_seconds)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlaylistEntry:
        return cls(
            name=str(d["name"]),
            play_seconds=max(MIN_PLAY_SECONDS, float(d.get("play_seconds", DEFAULT_PLAY_SECONDS))),
        )


@dataclass
class Playlist:
    """In-memory playlist + runtime state. Backed by a YAML file on disk."""

    path: Path
    entries: list[PlaylistEntry] = field(default_factory=list)
    running: bool = False
    current_index: int = 0
    started_at: float = 0.0
    _task: asyncio.Task | None = None
    _runtime: Runtime | None = None
    _store: EffectStore | None = None

    @classmethod
    def load(cls, path: Path) -> Playlist:
        p = cls(path=Path(path))
        if p.path.is_file():
            try:
                data = yaml.safe_load(p.path.read_text()) or {}
                raw = data.get("entries") or []
                p.entries = [PlaylistEntry.from_dict(e) for e in raw if e.get("name")]
            except Exception:
                log.exception("playlist: failed to load %s", p.path)
        return p

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [e.to_dict() for e in self.entries]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
        tmp.replace(self.path)

    def replace_entries(self, raw: list[dict[str, Any]]) -> None:
        self.entries = [PlaylistEntry.from_dict(e) for e in raw if e.get("name")]
        if self.current_index >= len(self.entries):
            self.current_index = 0
        self.save()

    def attach(self, runtime: Runtime, store: EffectStore) -> None:
        self._runtime = runtime
        self._store = store

    # ---- runtime control ---- #

    def start(self) -> None:
        if self.running or not self.entries:
            return
        if self._runtime is None or self._store is None:
            raise RuntimeError("playlist: attach() must be called before start()")
        self.running = True
        self.current_index = 0
        self.started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="ledctl-playlist")

    def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def state(self) -> dict[str, Any]:
        elapsed = time.time() - self.started_at if self.running else 0.0
        cur = None
        if self.entries and self.current_index < len(self.entries):
            cur = self.entries[self.current_index]
        return {
            "running": self.running,
            "current_index": self.current_index if self.running else -1,
            "current_name": cur.name if cur else None,
            "current_elapsed": round(elapsed, 2),
            "current_total": cur.play_seconds if cur else 0.0,
            "entries": [e.to_dict() for e in self.entries],
        }

    # ---- internal advance loop ---- #

    async def _loop(self) -> None:
        try:
            while self.running and self.entries:
                entry = self.entries[self.current_index]
                self._load_into_live(entry.name)
                self.started_at = time.time()
                # Sleep in 1 s slices so a stop()/replace_entries() has snappy reaction.
                end_at = self.started_at + max(MIN_PLAY_SECONDS, float(entry.play_seconds))
                while self.running and time.time() < end_at:
                    await asyncio.sleep(min(1.0, end_at - time.time()))
                if not self.running:
                    break
                # Advance — wrap to 0 to loop forever.
                if self.entries:
                    self.current_index = (self.current_index + 1) % len(self.entries)
                else:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("playlist loop crashed")
            self.running = False

    def _load_into_live(self, name: str) -> None:
        runtime = self._runtime
        store = self._store
        if runtime is None or store is None:
            return
        try:
            stored = store.load(name)
        except FileNotFoundError:
            log.warning("playlist: effect %r missing from library; skipping", name)
            return
        try:
            runtime.install_layer(
                "live",
                name=stored.name,
                summary=stored.summary,
                source=stored.source,
                param_schema=stored.param_schema,
                param_values=stored.param_values,
                blend="normal",
                opacity=1.0,
            )
        except (EffectCompileError, ValueError):
            log.exception("playlist: failed to install %r", name)


def default_playlist_path(config_dir: Path | None = None) -> Path:
    base = Path(config_dir) if config_dir is not None else Path("config")
    return base / "playlist.yaml"


__all__ = [
    "DEFAULT_PLAY_SECONDS",
    "MIN_PLAY_SECONDS",
    "Playlist",
    "PlaylistEntry",
    "default_playlist_path",
]
