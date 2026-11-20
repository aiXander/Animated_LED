"""The MIDI control surface: raw controller messages in, operator actions out.

A sender (WZRD in its LEDs mode, or `scripts/midi_relay.py`) forwards the
controller's raw messages over `/ws/midi`; this class maps them to the same
things the operator UI does and says which lights the controller should show.
The mapping (`bindings`: hardware id -> target id) is the preset until the
operator edits it, then `config/midi.yaml`.

Event loop only: `handle` runs in the WebSocket handler, never on a thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import yamlio
from ..masters import BOUNDS
from ..surface.persistence import install_from_library, write_atomic
from . import apc_mini

log = logging.getLogger(__name__)

FILENAME = "midi.yaml"
# A fader takes over its target once it is this close to the target's value
# (as a share of the fader's travel), or passes it.
PICKUP = 0.05
EFFECT = "effect:"
_FX = ("sparkle", "glitch", "strobe", "trails")


@dataclass(frozen=True)
class Target:
    id: str
    name: str       # in the picker and the tooltip
    label: str      # a word or two, drawn on the control
    help: str       # the tooltip
    kind: str       # "fader" | "button"
    group: str      # heading in the picker


@dataclass(frozen=True)
class _Fader:
    """How a fader target reads and writes: the value at the bottom, the
    middle and the top of the fader's travel (two straight lines, so a master
    whose neutral is 1 sits at the middle)."""
    lo: float
    mid: float
    hi: float
    get: Callable[[], float]
    set: Callable[[float], None]

    def value(self, pos: float) -> float:
        pos = min(1.0, max(0.0, pos))
        if pos <= 0.5:
            return self.lo + (self.mid - self.lo) * 2.0 * pos
        return self.mid + (self.hi - self.mid) * (2.0 * pos - 1.0)

    def pos(self) -> float:
        v = min(self.hi, max(self.lo, float(self.get())))
        if v <= self.mid:
            return 0.5 * (v - self.lo) / (self.mid - self.lo)
        return 0.5 + 0.5 * (v - self.mid) / (self.hi - self.mid)


# field, name (picker and tooltip), label (on the drawing), help
_MASTERS = [
    ("brightness", "Master brightness", "bright",
     "Master brightness of the LEDs. Middle = normal; the top half pushes the brightest "
     "part of the effect toward full."),
    ("speed", "Master speed", "speed",
     "Master speed of every effect. Middle = normal, bottom = frozen, top = 3x."),
    ("audio_reactivity", "Audio reactivity", "audio",
     "How strongly effects react to the music. Middle = normal, bottom = not at all."),
    ("saturation", "Saturation", "sat",
     "Colour saturation. Top = full colour, bottom = black and white."),
    ("audio_swell", "Audio swell", "swell",
     "Audio swell: how far the LEDs dim when the room goes quiet."),
    ("sparkle", "Sparkle", "sparkle",
     "Sparkle: white sparks over the effect, with a burst on every beat."),
    ("glitch", "Glitch", "glitch",
     "Glitch: stretches of LEDs freeze, jump, swap colours or drop out, more often as it "
     "rises and on the beat."),
    ("strobe", "Strobe", "strobe",
     "Strobe: the whole rig flashes, 2 times a second near the bottom up to 11 at the top."),
    ("trails", "Trails", "trails",
     "Trails: moving light leaves a tail that fades slowly, up to a few seconds at the top."),
]
# key in the WZRD settings, name, label, help
_WZRD = [
    ("colour", "Wall tint", "wall tint",
     "Wall tint: how strongly WZRD's wall colours tint the LEDs. Bottom = the effect's own "
     "colours, top = fully the wall's."),
    ("wash", "Wall wash", "wall wash",
     "Wall wash: how much of the wall's own light replaces the effect. Top = the effect "
     "is gone."),
    ("led_floor", "LEDs when the wall is dark", "wall floor",
     "LED brightness while the wall is dark. Top = ignore how bright the wall is."),
]
# id, name, label, help
_BUTTONS = [
    ("blackout", "Blackout", "blackout",
     "Blackout: press for all LEDs off, press again to bring them back. Lit while dark."),
    ("playlist", "Playlist start / stop", "playlist",
     "Start or stop the playlist. Lit while it runs. Picking an effect on a pad stops it."),
    ("flash", "Flash (hold)", "flash", "Flash: every LED at full while held."),
    ("fx.clear", "Master effects off", "fx off",
     "Master effects off: sparkle, glitch, strobe and trails back to 0."),
    ("effect.prev", "Previous effect", "prev",
     "Previous effect: the pad before the one that is live."),
    ("effect.next", "Next effect", "next",
     "Next effect: the pad after the one that is live."),
]
_CROSSFADE_HELP = ("Crossfade time when the live effect changes: 0 = a hard cut, "
                   "middle = 2 seconds, top = 10.")


class MidiSurface:
    def __init__(self, *, engine: Any, runtime: Any, playlist: Any, store: Any,
                 value_saver: Any = None, wzrd: Any = None, path: Path | None = None,
                 mark_dirty: Callable[[], None] = lambda: None):
        self.engine = engine
        self.runtime = runtime
        self.playlist = playlist
        self.store = store
        self.value_saver = value_saver
        self.wzrd = wzrd
        self.path = path
        self.mark_dirty = mark_dirty
        self.controls = apc_mini.controls()
        self._by_hw = {c["hw"]: c for c in self.controls}
        self.targets, self._faders = self._build_targets()
        self.bindings: dict[str, str] = {}
        self.custom = False          # True once the operator's mapping is on disk
        self.rev = 0                 # bumped when the mapping changes
        self.clients = 0
        self.last: dict[str, Any] | None = None    # the control touched last
        self._seq = 0
        self.fader_pos: dict[str, float] = {}      # hardware positions seen
        self._held: dict[str, str] = {}            # pressed control -> its target
        self._wanted: str | None = None            # effect to load next
        self._loading: str | None = None
        self._failed: set[str] = set()             # effects that would not load
        self._loader: asyncio.Task | None = None
        self._wzrd_save: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()
        self.load()

    # ---- targets ----

    def _build_targets(self) -> tuple[dict[str, Target], dict[str, _Fader]]:
        targets: dict[str, Target] = {}
        faders: dict[str, _Fader] = {}

        def master(field: str) -> _Fader:
            lo, hi = BOUNDS[field]
            mid = 1.0 if hi > 1.0 else (lo + hi) / 2.0
            return _Fader(lo, mid, hi,
                          lambda: getattr(self.engine.masters, field),
                          lambda v: self.engine.set_masters(**{field: v}))

        for field, name, label, text in _MASTERS:
            tid = f"master.{field}"
            group = "Master effects" if field in _FX else "Masters"
            targets[tid] = Target(tid, name, label, text, "fader", group)
            faders[tid] = master(field)
        targets["crossfade"] = Target(
            "crossfade", "Crossfade time", "x-fade", _CROSSFADE_HELP, "fader", "Masters")
        faders["crossfade"] = _Fader(
            0.0, 2.0, 10.0, lambda: self.runtime.crossfade_seconds,
            lambda v: setattr(self.runtime, "crossfade_seconds", float(v)))
        if self.wzrd is not None:
            for key, name, label, text in _WZRD:
                tid = f"wzrd.{key}"
                targets[tid] = Target(tid, name, label, text, "fader", "WZRD input")
                faders[tid] = _Fader(
                    0.0, 0.5, 1.0, lambda k=key: getattr(self.wzrd.settings, k),
                    lambda v, k=key: self._set_wzrd(k, v))
        for tid, name, label, text in _BUTTONS:
            targets[tid] = Target(tid, name, label, text, "button", "Actions")
        return targets, faders

    def target(self, tid: str) -> Target | None:
        """The target `tid` names, an `effect:<name>` included; None = unknown."""
        if tid.startswith(EFFECT):
            name = tid[len(EFFECT):]
            return Target(
                tid, name.replace("_", " "), name,
                f"Effect {name!r}: crossfade the LEDs to it. Yellow = ready, green = live, "
                "red = it would not load.", "button", "Effects")
        return self.targets.get(tid)

    def _set_wzrd(self, key: str, value: float) -> None:
        # A fader sends a change per frame; the settings file is written once
        # it has rested for a second, on a worker thread.
        self.wzrd.update_settings({key: round(float(value), 4)}, save=False)
        if self._wzrd_save is not None:
            self._wzrd_save.cancel()
        self._wzrd_save = asyncio.get_running_loop().call_later(1.0, self._save_wzrd)

    def _save_wzrd(self) -> None:
        self._wzrd_save = None
        path = self.wzrd.settings_path
        if path is not None:
            self._spawn(asyncio.to_thread(self.wzrd.settings.save, path))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- the mapping ----

    def _preset(self) -> dict[str, str]:
        """Starred effects first, then the rest by name."""
        names = self.store.list()
        starred = []
        for n in names:
            try:
                if self.store.load(n).starred:
                    starred.append(n)
            except Exception:  # noqa: BLE001 - an unreadable effect is just not starred
                log.debug("midi: could not read %r for the preset", n)
        ordered = starred + [n for n in names if n not in starred]
        return apc_mini.default_bindings(ordered)

    def load(self) -> None:
        """The operator's mapping from disk, else the preset. Entries that no
        longer validate (a renamed target) are dropped, not fatal."""
        saved = None
        if self.path is not None and self.path.is_file():
            try:
                saved = (yamlio.load(self.path.read_text()) or {}).get("bindings")
            except Exception:  # noqa: BLE001
                log.exception("midi: could not read %s; using the preset", self.path)
        self.custom = isinstance(saved, dict)
        raw = saved if self.custom else self._preset()
        self.bindings = {
            str(hw): str(t) for hw, t in raw.items() if self._problem(str(hw), str(t)) is None
        }
        self._failed.clear()
        self.rev += 1

    def _problem(self, hw: str, tid: str) -> str | None:
        """Why `hw` cannot be bound to `tid`, or None when it can."""
        control = self._by_hw.get(hw)
        if control is None:
            return f"no control {hw!r} on the {apc_mini.NAME}"
        if control.get("reserved"):
            return f"{control['label']} belongs to WZRD"
        target = self.target(tid)
        if target is None:
            return f"unknown target {tid!r}"
        if target.kind != control["kind"]:
            return (f"{target.label!r} needs a {target.kind}, "
                    f"{control['label']} is a {control['kind']}")
        return None

    def patch_bindings(self, patch: dict[str, str | None]) -> None:
        """Bind (`hw: target`) or clear (`hw: None`) controls. All or nothing:
        raises ValueError before changing anything."""
        for hw, tid in patch.items():
            if tid is None:
                if hw not in self._by_hw:
                    raise ValueError(f"no control {hw!r} on the {apc_mini.NAME}")
                continue
            problem = self._problem(hw, tid)
            if problem is None and tid.startswith(EFFECT) \
                    and not self.store.exists(tid[len(EFFECT):]):
                problem = f"no effect {tid[len(EFFECT):]!r} in the library"
            if problem:
                raise ValueError(problem)
        for hw, tid in patch.items():
            if tid is None:
                self.bindings.pop(hw, None)
            else:
                self.bindings[hw] = tid
        self._failed.clear()
        self.custom = True
        self.rev += 1

    def save(self) -> None:
        """Write the mapping (blocking: call on a worker thread)."""
        if self.path is not None:
            write_atomic(self.path, yamlio.dump(
                {"device": apc_mini.ID, "bindings": dict(self.bindings)}))

    def reset(self) -> None:
        """Back to the preset; the saved mapping is deleted (blocking)."""
        if self.path is not None:
            self.path.unlink(missing_ok=True)
        self.load()

    # ---- messages from the controller ----

    def handle(self, data: bytes) -> None:
        """One raw MIDI message. Anything that is not a note or a control
        change, or is too short, is ignored."""
        if len(data) < 3:
            return
        kind, number, value = data[0] & 0xF0, data[1] & 0x7F, data[2] & 0x7F
        if kind == 0xB0:
            hw, pos = f"cc:{number}", value / 127.0
        elif kind in (0x90, 0x80):
            hw, pos = f"note:{number}", 1.0 if kind == 0x90 and value > 0 else 0.0
        else:
            return
        control = self._by_hw.get(hw)
        if control is None:
            return
        self._seq += 1
        self.last = {"hw": hw, "pos": round(pos, 3), "seq": self._seq}
        tid = self.bindings.get(hw)
        if control["kind"] == "fader":
            if tid is not None and not control.get("reserved"):
                self._ride(hw, self._faders[tid], pos)
            self.fader_pos[hw] = pos
        elif pos > 0.0:
            if tid is not None and not control.get("reserved"):
                self._held[hw] = tid
                self._press(tid)
        else:
            # The release goes to the target the press went to, even if the
            # mapping changed while the button was down.
            held = self._held.pop(hw, None)
            if held == "flash" and "flash" not in self._held.values():
                self.runtime.flash = False

    def _ride(self, hw: str, fader: _Fader, pos: float) -> None:
        """Soft takeover: the fader moves its target only from the target's
        current value, so a mode switch or an on-screen change never makes
        the value jump to wherever the fader happens to sit."""
        cur = fader.pos()
        last = self.fader_pos.get(hw)
        near = abs(pos - cur) <= PICKUP
        was_on = last is not None and (abs(last - cur) <= PICKUP or (last - cur) * (pos - cur) < 0)
        if near or was_on:
            fader.set(fader.value(pos))

    def _press(self, tid: str) -> None:
        rt = self.runtime
        if tid.startswith(EFFECT):
            self._go(tid[len(EFFECT):])
        elif tid in ("effect.prev", "effect.next"):
            names = self.pad_effects()
            if names:
                live = {layer.name for layer in rt.live.layers}
                at = next((i for i, n in enumerate(names) if n in live), None)
                step = 1 if tid == "effect.next" else -1
                self._go(names[0 if at is None else (at + step) % len(names)])
        elif tid == "blackout":
            rt.blackout = not rt.blackout
        elif tid == "playlist":
            if self.playlist.running:
                self.playlist.stop()
            elif self.playlist.entries:
                self.playlist.start()
        elif tid == "flash":
            rt.flash = True
        elif tid == "fx.clear":
            self.engine.set_masters(**dict.fromkeys(_FX, 0.0))
        self.mark_dirty()

    def pad_effects(self) -> list[str]:
        """The effects on the controller in reading order (top row first),
        each once: what previous / next step through."""
        ordered = sorted(
            (c["at"][1], c["at"][0], self.bindings[c["hw"]][len(EFFECT):])
            for c in self.controls
            if self.bindings.get(c["hw"], "").startswith(EFFECT)
        )
        return list(dict.fromkeys(name for _, _, name in ordered))

    def _go(self, name: str) -> None:
        """Crossfade LIVE to `name`. A press during a load replaces whatever
        was queued: the last pad pressed wins, and loads never overlap."""
        self.playlist.stop()
        self._wanted = name
        if self._loader is None or self._loader.done():
            self._loader = self._spawn(self._load_wanted())

    async def _load_wanted(self) -> None:
        while self._wanted is not None:
            name, self._wanted = self._wanted, None
            self._loading = name
            try:
                await install_from_library(
                    self.runtime, self.store, self.value_saver, "live", name)
                self._failed.discard(name)
            except Exception as e:  # noqa: BLE001 - a bad effect must not end the loader
                log.warning("midi: could not load %r: %s", name, e)
                self._failed.add(name)
            finally:
                self._loading = None
                self.mark_dirty()

    def released(self) -> None:
        """The last sender went away: nothing may stay held."""
        self._held.clear()
        self.runtime.flash = False

    # ---- lights and payloads ----

    def lights(self) -> dict[int, int]:
        """note -> light value for every control ledctl owns that has a light."""
        rt = self.runtime
        live = {layer.name for layer in rt.live.layers}
        out: dict[int, int] = {}
        for c in self.controls:
            if c["led"] is None or c.get("reserved"):
                continue
            tid = self.bindings.get(c["hw"])
            if tid is None:
                on = None
            elif tid.startswith(EFFECT):
                name = tid[len(EFFECT):]
                if name == self._loading or name == self._wanted:
                    out[int(c["hw"][5:])] = apc_mini.PAD_GREEN_BLINK if c["led"] == "pad" \
                        else apc_mini.SINGLE_ON
                    continue
                if name in self._failed and c["led"] == "pad":
                    out[int(c["hw"][5:])] = apc_mini.PAD_RED
                    continue
                on = name in live
            else:
                on = {"blackout": rt.blackout, "playlist": self.playlist.running,
                      "flash": rt.flash}.get(tid, False)
            if c["led"] == "pad":
                value = apc_mini.PAD_OFF if on is None else (
                    apc_mini.PAD_GREEN if on else apc_mini.PAD_YELLOW)
            else:
                value = apc_mini.SINGLE_ON if on else apc_mini.SINGLE_OFF
            out[int(c["hw"][5:])] = value
        return out

    def state(self) -> dict[str, Any]:
        """The small, fast-changing part, sent with every /ws/state push."""
        return {
            "clients": self.clients,
            "rev": self.rev,
            "last": self.last,
            "faders": {hw: round(p, 3) for hw, p in self.fader_pos.items()},
            "values": {hw: round(self._faders[t].pos(), 3)
                       for hw, t in self.bindings.items() if t in self._faders},
            "lights": {f"note:{n}": v for n, v in self.lights().items() if v},
        }

    def describe(self) -> dict[str, Any]:
        """Everything the mapping view draws: the controller, the mapping, and
        what a control can be bound to."""
        effects = self.store.list()
        return {
            "device": {"id": apc_mini.ID, "name": apc_mini.NAME,
                       "cols": apc_mini.COLS, "rows": apc_mini.ROWS,
                       "controls": self.controls},
            "bindings": dict(self.bindings),
            "custom": self.custom,
            "rev": self.rev,
            "targets": [vars(t) for t in self.targets.values()]
                       + [vars(self.target(EFFECT + n)) for n in effects],
        }
