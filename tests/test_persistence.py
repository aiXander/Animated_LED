"""Round-trip an effect through save → load → install → render."""

from __future__ import annotations

from pathlib import Path

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import EffectStore, Runtime, WriteEffectArgs
from ledctl.surface.base import AudioView
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def test_save_then_load_then_render(tmp_path: Path):
    store = EffectStore(tmp_path)
    src = """\
class MyEffect(Effect):
    def init(self, ctx):
        self.scratch = np.zeros(ctx.n, dtype=np.float32)
    def render(self, ctx):
        col = hex_to_rgb(ctx.params.color)
        self.out[:] = col[None, :]
        return self.out
"""
    args = WriteEffectArgs(
        name="my_effect",
        summary="test",
        code=src,
        params=[{"key": "color", "control": "color", "default": "#11aa33"}],
    )
    stored = store.save(args=args)
    assert (tmp_path / "my_effect" / "effect.py").is_file()
    assert (tmp_path / "my_effect" / "effect.yaml").is_file()
    assert stored.param_values["color"] == "#11aa33"

    loaded = store.load("my_effect")
    assert loaded.source == src
    assert loaded.param_schema[0]["key"] == "color"

    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer(
        "live", name=loaded.name, summary=loaded.summary, source=loaded.source,
        param_schema=loaded.param_schema, param_values=loaded.param_values,
    )
    rt._cf = None
    live, _ = rt.render(
        wall_t=0.0, dt=1/60, t_eff=0.0, audio=AudioView(),
    )
    assert live.shape == (topo.pixel_count, 3)
    # green-ish reference colour 0x11AA33
    assert live[:, 1].max() > live[:, 0].max()  # G > R


def test_install_examples_if_missing(tmp_path: Path):
    store = EffectStore(tmp_path)
    new = store.install_examples_if_missing()
    assert "pulse_mono" in new
    # Idempotent — second call installs nothing.
    new2 = store.install_examples_if_missing()
    assert new2 == []


def test_save_values_persists(tmp_path: Path):
    store = EffectStore(tmp_path)
    args = WriteEffectArgs(
        name="x",
        summary="",
        code="class X(Effect):\n    def render(self, ctx): return self.out\n",
        params=[{"key": "speed", "control": "slider",
                 "min": 0, "max": 1, "default": 0.3}],
    )
    store.save(args=args)
    store.save_values("x", {"speed": 0.7})
    loaded = store.load("x")
    assert loaded.param_values["speed"] == 0.7


def test_delete_removes_dir(tmp_path: Path):
    store = EffectStore(tmp_path)
    args = WriteEffectArgs(
        name="x",
        summary="",
        code="class X(Effect):\n    def render(self, ctx): return self.out\n",
        params=[],
    )
    store.save(args=args)
    assert store.exists("x")
    assert store.delete("x")
    assert not store.exists("x")
