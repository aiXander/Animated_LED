"""WZRD input: the C-LED wire contract and the output stage built on it."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface.runtime import Runtime
from ledctl.topology import Topology
from ledctl.wzrd import WzrdListener, WzrdSettings, WzrdStage
from ledctl.wzrd.state import centre_colours

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"
CENTRES = centre_colours(12)


def _colors(level: float, white: float, bins: dict[int, tuple]) -> list[float]:
    """A 50-float `/wzrd/colors` packet; bins not listed send their centre colour."""
    out = [level, white]
    for i in range(12):
        out += [float(x) for x in bins.get(i, (*CENTRES[i], 0.0))]
    return out


# The WZRD spec's five test vectors: (level, white, bins with weight).
VECTORS = [
    (1.0, 0.0, {0: (1, 0, 0, 0.5), 8: (0, 0, 1, 0.5)}),      # half red, half blue
    (1.0, 0.5, {0: (1, 0, 0, 1.0)}),                          # all (1, .5, .5)
    (1.0, 0.0, {1: (1, 0.735, 0, 1.0)}),                      # all (1, .5, 0)
    (1.0, 0.0, {0: (1, 0.537, 0, 0.5), 1: (1, 0.537, 0, 0.5)}),
    (0.0, 0.0, {}),                                           # all black
]


def test_cled_packets_are_parsed_or_dropped():
    lis = WzrdListener()
    for level, white, bins in VECTORS:
        lis._on_colors("/wzrd/colors", *_colors(level, white, bins))
        _, got_level, got_white, got_bins = lis.latest_colors
        assert (got_level, got_white) == (level, white)
        assert got_bins.shape == (12, 4)
        for i in range(12):
            want = bins.get(i, (*CENTRES[i], 0.0))
            assert got_bins[i] == pytest.approx(want, abs=1e-3)
    lis._on_masters("/wzrd/masters", 1.5, 0.25)
    assert lis.latest_masters[1:] == (1.5, 0.25)
    assert lis.invalid == 0

    before = (lis.latest_colors, lis.latest_masters)
    good = _colors(*VECTORS[0])
    lis._on_colors("/wzrd/colors", *good[:-1])                         # short
    lis._on_colors("/wzrd/colors", *good[:7], float("nan"), *good[8:])  # NaN
    lis._on_masters("/wzrd/masters", 1.0)                              # short
    lis._on_masters("/wzrd/masters", float("inf"), 1.0)
    assert lis.invalid == 4
    assert (lis.latest_colors, lis.latest_masters) == before


def test_output_stage_recolours_and_follows_wzrd_masters():
    topo = Topology.from_config(load_config(DEV))
    n = topo.pixel_count
    frame = np.zeros((n, 3), dtype=np.float32)
    frame[0::4] = (1, 0, 0)     # red
    frame[1::4] = (0, 1, 1)     # cyan
    frame[2::4] = (1, 1, 1)     # white; 3::4 stays black

    def run(rt: Runtime) -> np.ndarray:
        buf = frame.copy()
        if rt.output_hook is not None:
            rt.output_hook.tick(0.0)
        rt._apply_master_output(buf, 0.0)
        return buf

    plain = run(Runtime(topo, MasterControls(brightness=2.0)))

    lis = WzrdListener()
    lis._on_colors("/wzrd/colors", *_colors(*VECTORS[0]))    # a red-and-blue wall
    lis._on_masters("/wzrd/masters", 1.0, 1.0)
    stage = WzrdStage(topo, lis, WzrdSettings(listen=False))
    rt = Runtime(topo, MasterControls(brightness=2.0))
    rt.output_hook = stage
    np.testing.assert_array_equal(run(rt), plain)            # listen off = no hook

    # The wall's palette lies from hub to tip on every leg: red on the inner
    # half, blue on the outer half, whatever colour the effect drew there.
    radius = topo.derived["radius"]
    inner, outer = radius < 0.4, radius > 0.6
    assert {int(x) for x in topo.derived["leg"][inner]} == {0, 1, 2}
    lit = frame.max(axis=1) > 0                              # red, cyan and white alike
    cyan = np.arange(n) % 4 == 1

    stage.settings = WzrdSettings(listen=True, colour=1.0)
    out = run(rt)
    np.testing.assert_allclose(out[inner & lit] - [1, 0, 0], 0, atol=1e-3)
    np.testing.assert_allclose(out[outer & lit] - [0, 0, 1], 0, atol=1e-3)
    np.testing.assert_array_equal(out[3::4], 0.0)            # black stays black

    stage.settings = WzrdSettings(listen=True, colour=1.0, tint_white=False)
    out = run(rt)
    np.testing.assert_array_equal(out[2::4], 1.0)            # white stays white
    np.testing.assert_allclose(out[inner & cyan] - [1, 0, 0], 0, atol=1e-3)

    stage.settings = WzrdSettings(listen=True, colour=0.0, wash=1.0)
    out = run(rt)                                            # the wall alone, black LEDs too
    np.testing.assert_allclose(out[inner] - [1, 0, 0], 0, atol=1e-3)
    np.testing.assert_allclose(out[outer] - [0, 0, 1], 0, atol=1e-3)

    stage.settings = WzrdSettings(listen=True)
    lis._on_masters("/wzrd/masters", 0.0, 1.0)               # WZRD BLACKOUT, coupled
    assert not run(rt).any()


def test_settings_saved_under_the_old_level_names_still_load(tmp_path):
    # The Pi's config/wzrd_input.yaml predates the renames and still carries
    # `spread`, which is gone. An unknown key used to fail validation, and
    # `load` then fell back to defaults: listen off.
    path = tmp_path / "wzrd_input.yaml"
    path.write_text("listen: true\nspread: 1.0\nfollow_level: 0.7\nlevel_full: 0.05\n")
    s = WzrdSettings.load(path)
    assert s.listen
    assert (s.mapping, s.wash, s.tint_white) == ("palette", 0.0, True)
    assert s.led_floor == pytest.approx(0.3)
    assert (s.wall_dark, s.wall_bright) == (0.0, 0.05)
