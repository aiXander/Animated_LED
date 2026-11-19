"""Runtime — slots, layered composition, crossfade, master output stage, fence test."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import Runtime
from ledctl.surface.base import AudioView
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"

_PULSE = """\
class PulseMono(Effect):
    def init(self, ctx):
        self._scratch = np.zeros(ctx.n, dtype=np.float32)
    def render(self, ctx):
        col = hex_to_rgb(ctx.params.color)
        self.out[:] = col[None, :]
        return self.out
"""

_PARAMS_PULSE = [
    {"key": "color", "control": "color", "default": "#ff0000"},
]


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _audio_zero() -> AudioView:
    return AudioView()


def test_install_into_live_then_preview():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer(
        "live", name="pulse_mono", summary="", source=_PULSE,
        param_schema=_PARAMS_PULSE,
    )
    rt.install_layer(
        "preview", name="pulse_mono", summary="", source=_PULSE,
        param_schema=_PARAMS_PULSE,
    )
    snap = rt.snapshot()
    assert snap["live"]["layers"][0]["name"] == "pulse_mono"
    assert snap["preview"]["layers"][0]["name"] == "pulse_mono"


def test_render_returns_two_buffers_in_design_mode():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt.install_layer("preview", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    rt.mode = "design"
    live, sim = rt.render(wall_t=0.1, dt=1/60, t_eff=0.1, audio=_audio_zero())
    assert live.shape == (topo.pixel_count, 3)
    assert sim.shape == (topo.pixel_count, 3)
    # In design mode, sim and live are distinct buffers (content can match
    # when both slots run the same effect; the buffers themselves differ).
    assert sim is not live


def test_a_beat_between_two_preview_renders_still_reaches_the_preview():
    # A beat lasts one tick, and the preview renders only when a browser frame
    # is due. The beat must wait for the next preview render, not be lost.
    src = """\
class BeatLatch(Effect):
    def render(self, ctx):
        self.out[:] = ctx.audio.beat
        return self.out
"""
    rt = Runtime(_topo(), MasterControls())
    rt.install_layer("preview", name="beat_latch", summary="", source=src, param_schema=[])
    rt.mode = "design"
    rt.preview_half_rate = False
    rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=AudioView(beat=1.0),
              render_preview=False)                       # no browser frame due
    _, sim = rt.render(wall_t=1/60, dt=1/60, t_eff=1/60, audio=AudioView(beat=0.0))
    assert sim.max() == 1.0
    _, sim = rt.render(wall_t=2/60, dt=1/60, t_eff=2/60, audio=AudioView(beat=0.0))
    assert sim.max() == 0.0                               # and it is delivered once


def test_render_lives_share_buffer_in_live_mode():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    rt.mode = "live"
    live, sim = rt.render(wall_t=0.1, dt=1/60, t_eff=0.1, audio=_audio_zero())
    assert sim is live  # zero-copy in live mode


def test_promote_starts_crossfade():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.crossfade_seconds = 1.0
    # live = red
    src_red = _PULSE
    src_blue = _PULSE.replace("hex_to_rgb(ctx.params.color)", "hex_to_rgb('#0000ff')")
    rt.install_layer("live", name="red", summary="", source=src_red,
                     param_schema=[{"key": "color", "control": "color", "default": "#ff0000"}])
    rt.install_layer("preview", name="blue", summary="", source=src_blue, param_schema=[])
    rt._cf = None
    rt.promote()
    assert rt._cf is not None
    assert rt._cf.duration == 1.0


def test_master_brightness_scales_output():
    topo = _topo()
    rt = Runtime(topo, MasterControls(brightness=0.5))
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    live, _ = rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=_audio_zero())
    # Default red → R near 1.0 before brightness; after 0.5 brightness → ~0.5
    assert live[:, 0].max() <= 0.55
    assert live[:, 0].max() >= 0.45


def test_render_crash_does_not_kill_runtime():
    """Install a healthy effect, then swap its instance for one that always
    raises in render(). The runtime should catch, log, and zero the buffer."""
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    layer = rt.live.layers[0]

    class ExplodingEffect(layer.instance.__class__):
        def render(self, ctx):
            raise ValueError("boom")

    new_inst = ExplodingEffect()
    new_inst._setup(rt.n)
    new_inst.init(rt._build_init_ctx())
    layer.instance = new_inst

    live, _ = rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=_audio_zero())
    assert live.shape == (topo.pixel_count, 3)
    # Output is zeros after crash + masters applied.
    assert np.all(live == 0.0)
    assert layer.consecutive_failures >= 1


def test_layer_blend_normal_replaces_below():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    # Add a second layer (replace=False)
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=[{"key": "color", "control": "color", "default": "#0000ff"}],
                     replace=False)
    rt._cf = None
    assert len(rt.live.layers) == 2
    # Set the second layer's color to blue
    rt.live.layers[1].params.update({"color": "#0000ff"})
    live, _ = rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=_audio_zero())
    # Top layer is normal opacity 1.0 — should fully cover red layer with blue.
    assert live[:, 2].max() > 0.9   # B channel high
    assert live[:, 0].max() < 0.1   # R channel low


def test_remove_and_reorder_layer():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="a", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt.install_layer("live", name="b", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE, replace=False)
    rt.install_layer("live", name="c", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE, replace=False)
    assert [L.name for L in rt.live.layers] == ["a", "b", "c"]
    rt.reorder_layer("live", 0, 2)
    assert [L.name for L in rt.live.layers] == ["b", "c", "a"]
    rt.remove_layer("live", 1)
    assert [L.name for L in rt.live.layers] == ["b", "a"]


async def test_async_install_replaces_the_layer_it_was_aimed_at():
    """The compile runs on a worker thread; what the operator does to the deck
    meanwhile must not redirect the install onto another layer."""
    rt = Runtime(_topo(), MasterControls())
    for name in ("a", "b", "c"):
        rt.install_layer("preview", name=name, summary="", source=_PULSE,
                         param_schema=_PARAMS_PULSE, replace=False)
    rt.select_layer("preview", 0)

    def new(name):
        return rt.install_layer_async(
            "preview", name=name, summary="", source=_PULSE, param_schema=_PARAMS_PULSE)

    aimed_at_a = asyncio.ensure_future(new("new_a"))
    await asyncio.sleep(0)                 # compile is now in flight
    rt.reorder_layer("preview", 0, 2)      # a moves to the end
    rt.select_layer("preview", 0)          # operator selects b
    await aimed_at_a
    assert [L.name for L in rt.preview.layers] == ["b", "c", "new_a"]

    rt.select_layer("preview", 1)
    aimed_at_c = asyncio.ensure_future(new("late"))
    await asyncio.sleep(0)
    rt.remove_layer("preview", 1)          # c is removed mid-compile
    await aimed_at_c
    # Its target is gone: the layer is added, no neighbour is overwritten.
    assert [L.name for L in rt.preview.layers] == ["b", "new_a", "late"]



# Cheap but chatty: ~15 small numpy calls a frame, like a real effect. Each call
# is a point where another thread can take the interpreter lock.
_CHATTY = """\
class Chatty(Effect):
    def render(self, ctx):
        for _ in range(15):
            np.multiply(self.out, 0.5, out=self.out)
        return self.out
"""

_HEAVY = """\
class Heavy(Effect):
    def render(self, ctx):
        for _ in range(1500):
            np.sin(self.out, out=self.out)
        return self.out
"""


def _render_frames(rt, n):
    for i in range(n):
        rt.render(wall_t=i / 60, dt=1 / 60, t_eff=i / 60, audio=_audio_zero())


def test_render_budget_counts_the_layers_own_cpu_not_lock_waits():
    """The watchdog once timed render() on the wall clock, so a cheap effect was
    billed for every wait on the interpreter lock (the OSC listeners take it
    hundreds of times a second) and went dark at random."""
    import sys
    import threading

    rt = Runtime(_topo(), MasterControls())
    rt.install_layer("live", name="chatty", summary="", source=_CHATTY, param_schema=[])
    stop = threading.Event()

    def busy():
        while not stop.is_set():
            sum(range(2000))

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(0.001)  # as cli.py sets it
    threads = [threading.Thread(target=busy, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        _render_frames(rt, 40)  # the watchdog trips after 30 frames over budget
    finally:
        stop.set()
        for t in threads:
            t.join()
        sys.setswitchinterval(old_interval)
    assert rt.live.layers[0].enabled, rt.live.layers[0].perf.p95_ms

    # A layer that really spends its budget is still switched off.
    rt.install_layer("live", name="heavy", summary="", source=_HEAVY, param_schema=[])
    _render_frames(rt, 40)
    assert not rt.live.layers[0].enabled
