"""Per-primitive unit tests for the new control surface."""

from pathlib import Path

import numpy as np
import pytest

from ledctl.audio.state import AudioState
from ledctl.config import load_config
from ledctl.masters import MasterControls, RenderContext
from ledctl.surface import (
    LUT_SIZE,
    NAMED_PALETTES,
    REGISTRY,
    CompileError,
    LayerSpec,
    NodeSpec,
    _lut_from_hsv_stops,
    _lut_from_named,
    _lut_from_stops,
    compile_layers,
    hex_to_rgb01,
)
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


@pytest.fixture
def topo() -> Topology:
    return Topology.from_config(load_config(DEV))


@pytest.fixture
def ctx() -> RenderContext:
    return RenderContext(t=0.0, wall_t=0.0, audio=None, masters=MasterControls())


def _wrap_palette_lookup(scalar: dict, palette: object = "white", **extra) -> NodeSpec:
    params = {"scalar": scalar, "palette": palette}
    params.update(extra)
    return NodeSpec(kind="palette_lookup", params=params)


# ---- registry & docs ----


def test_registry_contains_core_primitives():
    expected = {
        "wave", "radial", "noise", "sparkles", "lfo", "audio_band",
        "envelope", "constant", "palette_named", "palette_stops",
        "palette_hsv", "palette_lookup", "solid", "mix", "mul", "add",
        "screen", "max", "min", "remap", "threshold", "trail", "position",
        "gradient", "clamp", "range_map",
    }
    assert expected.issubset(set(REGISTRY))


# ---- palettes ----


def test_named_palette_endpoints_match_first_and_last_stop():
    lut = _lut_from_named("fire")
    assert lut.shape == (LUT_SIZE, 3)
    assert lut[0].sum() < 0.05
    assert lut[-1, 0] > 0.9 and lut[-1, 1] > 0.9


def test_palette_named_mono_hex_is_solid_colour():
    lut = _lut_from_named("mono_ff7000")
    expected = np.array([0xFF, 0x70, 0x00], dtype=np.float32) / 255.0
    assert np.allclose(lut[0], expected, atol=1e-5)
    assert np.allclose(lut[-1], expected, atol=1e-5)


def test_palette_unknown_name_rejected_at_compile(topo: Topology):
    spec = LayerSpec(
        node=_wrap_palette_lookup(
            {"kind": "constant", "params": {"value": 0.5}},
            palette="not-a-real-palette",
        )
    )
    with pytest.raises(CompileError) as ei:
        compile_layers([spec], topo)
    assert "unknown palette" in str(ei.value)


def test_palette_stops_interp():
    lut = _lut_from_stops([
        {"pos": 0.0, "color": "#000000"},
        {"pos": 1.0, "color": "#ffffff"},
    ])
    assert 0.4 < lut[LUT_SIZE // 2, 0] < 0.6


def test_palette_lookup_respects_configured_lut_size(topo: Topology):
    # The palette LUT size is configurable (`output.lut_size`). On a smooth
    # 2-stop black->white gradient across 1800 pixels, the per-LED quantization
    # ceiling is exactly LUT_SIZE: each LED snaps to one of LUT_SIZE entries.
    # Bumping the LUT past the default must lift that ceiling, restoring
    # smoothness when an operator turns the knob up to chase visible banding.
    from ledctl.surface import set_lut_size

    def distinct_reds() -> int:
        layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
            {"kind": "gradient", "params": {"axis": "x"}},
            palette={"kind": "palette_stops", "params": {"stops": [
                {"pos": 0.0, "color": "#000000"},
                {"pos": 1.0, "color": "#ffffff"},
            ]}},
        ))], topo)
        out = layers[0].node.render(
            RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
        )
        return len(np.unique(out[:, 0]))

    original = LUT_SIZE
    try:
        set_lut_size(256)
        assert distinct_reds() <= 256
        set_lut_size(1024)
        assert distinct_reds() > 256
    finally:
        set_lut_size(original)


def test_palette_named_palettes_all_compile():
    for name in NAMED_PALETTES:
        lut = _lut_from_named(name)
        assert lut.shape == (LUT_SIZE, 3)
        assert (lut >= 0.0).all() and (lut <= 1.0 + 1e-5).all()


def test_rainbow_uses_hsv_uniform_brightness():
    # rainbow now bakes via HSV interp at S=V=1, so every entry should sit on
    # the saturated chromatic surface — max-channel == 1 across the LUT, no
    # muddy/dim midpoints between complementary colours that the old RGB
    # lerp produced (e.g. red->cyan midpoint was grey 0.5,0.5,0.5).
    lut = _lut_from_named("rainbow")
    max_channel = lut.max(axis=1)
    min_channel = lut.min(axis=1)
    assert max_channel.min() > 0.99, (
        f"rainbow has dim entry: min(max-channel)={max_channel.min():.3f}"
    )
    assert min_channel.min() < 0.01, (
        f"rainbow desaturated: max(min-channel)={min_channel.max():.3f} "
        f"— a saturated colour should always have at least one near-zero channel"
    )


def test_palette_hsv_endpoint_colours_match_hue_degrees():
    lut = _lut_from_hsv_stops([
        {"pos": 0.0, "hue": 0.0},
        {"pos": 1.0, "hue": 360.0},
    ])
    assert np.allclose(lut[0], [1.0, 0.0, 0.0], atol=1e-3)   # red at hue 0
    assert np.allclose(lut[LUT_SIZE // 6], [1.0, 1.0, 0.0], atol=2e-2)   # yellow ~60
    assert np.allclose(lut[LUT_SIZE // 3], [0.0, 1.0, 0.0], atol=2e-2)   # green ~120
    assert np.allclose(lut[LUT_SIZE // 2], [0.0, 1.0, 1.0], atol=2e-2)   # cyan ~180
    assert np.allclose(lut[(2 * LUT_SIZE) // 3], [0.0, 0.0, 1.0], atol=2e-2)  # blue ~240


def test_palette_hsv_signed_hue_picks_direction():
    # 0->180 sweeps through yellow/green; 0->-180 sweeps through magenta/blue.
    forward = _lut_from_hsv_stops([
        {"pos": 0.0, "hue": 0.0}, {"pos": 1.0, "hue": 180.0},
    ])
    reverse = _lut_from_hsv_stops([
        {"pos": 0.0, "hue": 0.0}, {"pos": 1.0, "hue": -180.0},
    ])
    mid = LUT_SIZE // 2
    # forward midpoint is hue 90 (yellow-green) — green > blue
    assert forward[mid, 1] > forward[mid, 2]
    # reverse midpoint is hue -90 = 270 (magenta-blue) — blue > green
    assert reverse[mid, 2] > reverse[mid, 1]


def test_palette_hsv_compiles_via_primitive(topo: Topology):
    layers = compile_layers([LayerSpec(node=NodeSpec(
        kind="palette_lookup",
        params={
            "scalar": {"kind": "constant", "params": {"value": 0.0}},
            "palette": {"kind": "palette_hsv", "params": {"stops": [
                {"pos": 0.0, "hue": 240.0},
                {"pos": 1.0, "hue": 360.0},
            ]}},
        },
    ))], topo)
    out = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    # constant 0 → first stop (hue 240 = blue)
    assert np.allclose(out[0], [0.0, 0.0, 1.0], atol=2e-2)


def test_palette_hsv_val_below_one_dims_palette():
    bright = _lut_from_hsv_stops([
        {"pos": 0.0, "hue": 0.0, "val": 1.0},
        {"pos": 1.0, "hue": 360.0, "val": 1.0},
    ])
    dim = _lut_from_hsv_stops([
        {"pos": 0.0, "hue": 0.0, "val": 0.5},
        {"pos": 1.0, "hue": 360.0, "val": 0.5},
    ])
    assert dim.max() < bright.max()
    assert np.allclose(dim, bright * 0.5, atol=1e-3)


def test_invalid_hex_in_stops_rejected(topo: Topology):
    spec = LayerSpec(node=NodeSpec(
        kind="palette_lookup",
        params={
            "scalar": {"kind": "constant", "params": {"value": 0.0}},
            "palette": {
                "kind": "palette_stops",
                "params": {"stops": [
                    {"pos": 0.0, "color": "not-hex"},
                    {"pos": 1.0, "color": "#ffffff"},
                ]},
            },
        },
    ))
    with pytest.raises(CompileError):
        compile_layers([spec], topo)


# ---- modulators ----


def test_constant_renders_value(ctx: RenderContext):
    cls = REGISTRY["constant"]
    params = cls.Params.model_validate({"value": 0.42})
    node = cls.compile(params, None, None)
    assert node.render(ctx) == pytest.approx(0.42)
    assert node.output_kind == "scalar_t"


def test_lfo_sin_cycles(ctx: RenderContext):
    cls = REGISTRY["lfo"]
    params = cls.Params.model_validate({"shape": "sin", "period_s": 1.0})
    node = cls.compile(params, None, None)
    a = node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    b = node.render(RenderContext(t=0.5, wall_t=0.5, masters=MasterControls()))
    assert abs((a + b) - 1.0) < 1e-5


def test_audio_band_returns_zero_when_no_state(ctx: RenderContext):
    cls = REGISTRY["audio_band"]
    params = cls.Params.model_validate({"band": "low"})
    node = cls.compile(params, None, None)
    assert node.render(ctx) == 0.0


def test_audio_band_reads_norm_value():
    state = AudioState(low=0.5, mid=0.7, high=0.1,
                       low_norm=0.5, mid_norm=0.7, high_norm=0.1)
    cls = REGISTRY["audio_band"]
    params = cls.Params.model_validate({"band": "low"})
    node = cls.compile(params, None, None)
    assert node.render(RenderContext(audio=state)) == 0.5


def test_audio_band_rejects_rms_and_peak():
    """RMS (too coarse, just loudness) and peak (too noisy, single-sample
    spikes) are deliberately not exposed for visual modulation."""
    cls = REGISTRY["audio_band"]
    for forbidden in ("rms", "peak"):
        try:
            cls.Params.model_validate({"band": forbidden})
        except Exception:
            continue
        raise AssertionError(f"band={forbidden!r} should be rejected")


def test_envelope_attack_then_release(topo: Topology):
    spec = LayerSpec(node=_wrap_palette_lookup(
        {"kind": "constant", "params": {"value": 0.0}},
        palette="white",
        brightness={
            "kind": "envelope",
            "params": {
                "input": {"kind": "audio_band", "params": {"band": "low"}},
                "attack_ms": 100.0,
                "release_ms": 1000.0,
            },
        },
    ))
    layers = compile_layers([spec], topo)
    state = AudioState()
    state.low_norm = 0.0
    ctx = RenderContext(t=0.0, wall_t=0.0, audio=state, masters=MasterControls())
    layers[0].node.render(ctx)
    # source switches to 1.0 at wall_t=0; sample at wall_t=0.1 should be
    # ~63% of the way to 1 (one tau).
    state.low_norm = 1.0
    ctx2 = RenderContext(t=0.1, wall_t=0.1, audio=state, masters=MasterControls())
    out2 = layers[0].node.render(ctx2)
    assert out2.max() > 0.55 and out2.max() < 0.70


def test_envelope_floor_ceiling(topo: Topology):
    spec = LayerSpec(node=_wrap_palette_lookup(
        {"kind": "constant", "params": {"value": 1.0}},
        palette="white",
        brightness={
            "kind": "envelope",
            "params": {
                "input": {"kind": "constant", "params": {"value": 0.0}},
                "attack_ms": 0.0,
                "release_ms": 0.0,
                "floor": 0.5,
                "ceiling": 1.0,
            },
        },
    ))
    layers = compile_layers([spec], topo)
    out = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    # source = 0 → maps to floor 0.5
    assert np.allclose(out, 0.5, atol=1e-3)


# ---- scalar fields ----


def test_wave_bounded_and_moves(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "wave", "params": {"speed": 1.0, "wavelength": 0.5}},
        palette="rainbow",
    ))], topo)
    a = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = layers[0].node.render(
        RenderContext(t=0.4, wall_t=0.4, masters=MasterControls())
    ).copy()
    assert (a >= 0.0).all() and (a <= 1.0 + 1e-5).all()
    assert not np.allclose(a, b)


def test_wave_static_when_speed_zero(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "wave", "params": {"speed": 0.0}},
        palette="fire",
    ))], topo)
    a = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = layers[0].node.render(
        RenderContext(t=2.5, wall_t=2.5, masters=MasterControls())
    ).copy()
    assert np.allclose(a, b)


def test_wave_cross_phase_offsets_y_rows(topo: Topology):
    plain = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "wave", "params": {"speed": 1.0, "cross_phase": [0, 0, 0]}},
        palette="rainbow",
    ))], topo)
    shifted = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "wave", "params": {"speed": 1.0, "cross_phase": [0, 0.25, 0]}},
        palette="rainbow",
    ))], topo)
    a = plain[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = shifted[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    top_i = 100
    bot_i = 1000
    assert np.allclose(
        topo.normalised_positions[top_i, 0],
        topo.normalised_positions[bot_i, 0],
        atol=1e-4,
    )
    assert np.allclose(a[top_i], a[bot_i], atol=1e-3)
    assert np.linalg.norm(b[top_i] - b[bot_i]) > 0.05


def test_radial_bounded_and_moves(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "radial", "params": {"speed": 0.5}}, palette="ice",
    ))], topo)
    a = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = layers[0].node.render(
        RenderContext(t=0.5, wall_t=0.5, masters=MasterControls())
    ).copy()
    assert (a >= 0.0).all() and (a <= 1.0 + 1e-5).all()
    assert not np.allclose(a, b)


def test_sparkles_default_white_lights_some_pixels(topo: Topology):
    # Default `sparkles` is monochrome white — works as a layer leaf directly.
    layers = compile_layers([LayerSpec(node=NodeSpec(
        kind="sparkles",
        params={"density": 2.0, "decay": 0.5, "seed": 7},
    ))], topo)
    layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    out = layers[0].node.render(RenderContext(t=0.1, wall_t=0.1, masters=MasterControls()))
    assert out.shape == (topo.pixel_count, 3)
    assert (out >= 0.0).all() and (out <= 1.0 + 1e-5).all()
    assert out.sum() > 0.0


def test_sparkles_seed_reproducible(topo: Topology):
    def _run() -> np.ndarray:
        layers = compile_layers([LayerSpec(node=NodeSpec(
            kind="sparkles",
            params={"density": 1.0, "seed": 42},
        ))], topo)
        layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
        return layers[0].node.render(
            RenderContext(t=0.5, wall_t=0.5, masters=MasterControls())
        ).copy()

    assert np.allclose(_run(), _run())


def test_noise_bounded_and_moves(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "noise", "params": {"speed": 0.5, "scale": 0.5, "seed": 3}},
        palette="ocean",
    ))], topo)
    a = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = layers[0].node.render(
        RenderContext(t=0.5, wall_t=0.5, masters=MasterControls())
    ).copy()
    assert (a >= 0.0).all() and (a <= 1.0 + 1e-5).all()
    assert not np.allclose(a, b)


def test_noise_static_when_speed_zero(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "noise", "params": {"speed": 0.0, "seed": 1}},
        palette="rainbow",
    ))], topo)
    a = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = layers[0].node.render(
        RenderContext(t=1.0, wall_t=1.0, masters=MasterControls())
    ).copy()
    assert np.allclose(a, b)


def test_sparkles_lights_some_coloured_pixels(topo: Topology):
    layers = compile_layers([LayerSpec(node=NodeSpec(
        kind="sparkles",
        params={
            "palette": "rainbow",
            "density": 5.0,
            "decay": 0.5,
            "spread": 1.0,
            "seed": 11,
        },
    ))], topo)
    layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    out = layers[0].node.render(
        RenderContext(t=0.1, wall_t=0.1, masters=MasterControls())
    )
    assert out.shape == (topo.pixel_count, 3)
    assert (out >= 0.0).all() and (out <= 1.0 + 1e-5).all()
    # Some pixels lit
    lit = np.any(out > 0.0, axis=1)
    assert lit.sum() > 0
    # With full-spread rainbow we expect more than one distinct hue across lit pixels
    if lit.sum() >= 4:
        lit_rgb = out[lit]
        assert lit_rgb.std(axis=0).sum() > 0.0


def test_sparkles_zero_spread_samples_single_palette_position(topo: Topology):
    layers = compile_layers([LayerSpec(node=NodeSpec(
        kind="sparkles",
        params={
            "palette": "rainbow",
            "density": 5.0,
            "decay": 0.0,
            "spread": 0.0,
            "palette_center": 0.0,  # red end of rainbow
            "seed": 3,
        },
    ))], topo)
    layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    out = layers[0].node.render(
        RenderContext(t=0.2, wall_t=0.2, masters=MasterControls())
    )
    lit = np.any(out > 0.0, axis=1)
    assert lit.sum() > 0
    lit_rgb = out[lit]
    # Every lit pixel is the same colour (red), so per-channel std across lit
    # pixels should be ~0 (modulo floating-point).
    assert lit_rgb.std(axis=0).max() < 1e-5
    # Confirm it's actually red-ish (rainbow @ pos 0 is #ff0000).
    assert lit_rgb[:, 0].min() > 0.9
    assert lit_rgb[:, 1].max() < 0.05


def test_sparkles_palette_seed_reproducible(topo: Topology):
    def _run() -> np.ndarray:
        layers = compile_layers([LayerSpec(node=NodeSpec(
            kind="sparkles",
            params={
                "palette": "rainbow",
                "density": 2.0,
                "decay": 0.3,
                "spread": 1.0,
                "seed": 42,
            },
        ))], topo)
        layers[0].node.render(
            RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
        )
        return layers[0].node.render(
            RenderContext(t=0.5, wall_t=0.5, masters=MasterControls())
        ).copy()

    assert np.allclose(_run(), _run())


def test_sparkles_stacks_additively_with_underlay(topo: Topology):
    # An underlay solid red plus a sparkles layer (blend=add) should be at
    # least as bright as the underlay everywhere, and brighter where lit.
    layers = compile_layers(
        [
            LayerSpec(node=NodeSpec(
                kind="solid", params={"rgb": [0.2, 0.0, 0.0]}
            )),
            LayerSpec(
                node=NodeSpec(
                    kind="sparkles",
                    params={
                        "palette": "ice",
                        "density": 5.0,
                        "decay": 0.0,
                        "spread": 0.5,
                        "palette_center": 0.8,
                        "seed": 9,
                    },
                ),
                blend="add",
            ),
        ],
        topo,
    )
    # warm up sprinkle state then sample
    for layer in layers:
        layer.node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    underlay = layers[0].node.render(
        RenderContext(t=0.1, wall_t=0.1, masters=MasterControls())
    ).copy()
    overlay = layers[1].node.render(
        RenderContext(t=0.1, wall_t=0.1, masters=MasterControls())
    ).copy()
    assert overlay.shape == underlay.shape == (topo.pixel_count, 3)
    lit = np.any(overlay > 0.0, axis=1)
    assert lit.sum() > 0  # sparkles contributed something
    # Where sparkles didn't light, overlay is zero (so add leaves the underlay alone)
    assert overlay[~lit].sum() == 0.0


def test_position_axis_is_static_field(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "position", "params": {"axis": "x"}},
        palette="rainbow",
    ))], topo)
    a = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    ).copy()
    b = layers[0].node.render(
        RenderContext(t=10.0, wall_t=10.0, masters=MasterControls())
    ).copy()
    assert np.allclose(a, b)


def test_solid_uniform_colour(topo: Topology):
    layers = compile_layers([LayerSpec(node=NodeSpec(
        kind="solid", params={"rgb": [1.0, 0.5, 0.0]}
    ))], topo)
    out = layers[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    assert np.allclose(out[0], [1.0, 0.5, 0.0])
    assert (out == out[0]).all()


# ---- combinators ----


def test_mix_palettes(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "constant", "params": {"value": 0.0}},
        palette={
            "kind": "mix",
            "params": {
                "a": "fire",
                "b": "ice",
                "t": {"kind": "constant", "params": {"value": 0.0}},
            },
        },
    ))], topo)
    out = layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    # t=0 → take palette `fire` at scalar=0 (which is black)
    assert np.allclose(out[0], 0.0, atol=1e-3)


def test_mul_scalar_field_and_scalar_t(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "mul", "params": {
            "a": {"kind": "wave", "params": {"speed": 0.0, "wavelength": 1.0}},
            "b": 0.5,
        }},
        palette="white",
    ))], topo)
    out = layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    # The result must still be RGB float32 (N, 3)
    assert out.shape == (topo.pixel_count, 3)
    assert (out <= 1.0).all()


def test_mix_palette_with_scalar_rejected(topo: Topology):
    spec = LayerSpec(node=NodeSpec(kind="mix", params={
        "a": "fire",
        "b": {"kind": "constant", "params": {"value": 0.5}},
        "t": 0.5,
    }))
    with pytest.raises(CompileError, match="palette"):
        compile_layers([spec], topo)


# ---- string + numeric sugar ----


def test_bare_number_becomes_constant(topo: Topology):
    """A bare numeric in `speed` is sugar for a constant scalar_t node."""
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "wave", "params": {"speed": 0.7}},
        palette="fire",
    ))], topo)
    # Should compile and render without error
    out = layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    assert out.shape == (topo.pixel_count, 3)


def test_bare_string_palette_becomes_palette_named(topo: Topology):
    layers = compile_layers([LayerSpec(node=_wrap_palette_lookup(
        {"kind": "constant", "params": {"value": 0.0}},
        palette="fire",
    ))], topo)
    out = layers[0].node.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()))
    # `fire` at scalar=0 is near-black
    assert out[0].sum() < 0.1


# ---- strict validation ----


def test_unknown_param_key_rejected(topo: Topology):
    spec = LayerSpec(node=NodeSpec(
        kind="wave", params={"axis": "x", "scroll_phase": [0, 0, 0]}
    ))
    with pytest.raises(CompileError) as ei:
        compile_layers([spec], topo)
    msg = str(ei.value)
    assert "scroll_phase" in msg or "Extra" in msg


def test_unknown_kind_rejected(topo: Topology):
    spec = LayerSpec(node=NodeSpec(kind="not_a_real_thing", params={}))
    with pytest.raises(CompileError) as ei:
        compile_layers([spec], topo)
    assert "unknown primitive kind" in str(ei.value)


def test_kind_mismatch_palette_in_scalar_slot(topo: Topology):
    spec = LayerSpec(node=_wrap_palette_lookup(
        {"kind": "palette_named", "params": {"name": "fire"}},
        palette="fire",
    ))
    with pytest.raises(CompileError, match="expected scalar_field"):
        compile_layers([spec], topo)


def test_layer_root_must_be_rgb_field(topo: Topology):
    spec = LayerSpec(node=NodeSpec(kind="wave", params={}))
    with pytest.raises(CompileError, match="rgb_field"):
        compile_layers([spec], topo)


# ---- LLM-failure-mode recovery: flattened params on a NodeSpec ----


def test_nodespec_recovers_when_params_flattened_with_missing_params_key():
    # Model emitted siblings of `kind` and forgot `params` entirely.
    raw = {"kind": "wave", "axis": "x", "speed": 0.2, "wavelength": 1.5}
    node = NodeSpec.model_validate(raw)
    assert node.kind == "wave"
    assert node.params == {"axis": "x", "speed": 0.2, "wavelength": 1.5}


def test_nodespec_recovers_when_params_is_truncated_string():
    # Exact pattern from the failing tool call: `params` is a string fragment
    # AND the actual params are flattened as siblings.
    raw = {
        "kind": "wave",
        "params": "{axis:",
        "speed": 0.2,
        "shape": "cosine",
        "wavelength": 1,
        "softness": 1,
    }
    node = NodeSpec.model_validate(raw)
    assert node.kind == "wave"
    # The truncated string is dropped; siblings become the real params.
    assert node.params == {
        "speed": 0.2,
        "shape": "cosine",
        "wavelength": 1,
        "softness": 1,
    }


def test_nodespec_strict_path_unchanged_when_well_formed():
    # No siblings, real params dict — recovery validator must not touch this.
    raw = {"kind": "wave", "params": {"axis": "x", "speed": 0.5}}
    node = NodeSpec.model_validate(raw)
    assert node.params == {"axis": "x", "speed": 0.5}


def test_nodespec_does_not_silently_merge_extras_with_real_params():
    # Both a real params dict AND extras is a likely typo, not the known
    # flattening pattern — the strict `extra_forbidden` error must still fire.
    from pydantic import ValidationError as PydanticValidationError

    raw = {
        "kind": "wave",
        "params": {"axis": "x"},
        "speed": 0.5,  # typo — should fail loudly
    }
    with pytest.raises(PydanticValidationError):
        NodeSpec.model_validate(raw)


def test_flattened_child_node_compiles_via_recovery(topo: Topology):
    # End-to-end: the malformed shape from the actual tool call compiles
    # cleanly through the recovery path. The wave's `axis` defaults to "x"
    # since the original `axis` value was lost in the truncated string.
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "palette_lookup",
            "params": {
                "scalar": {
                    "kind": "wave",
                    "params": "{axis:",
                    "speed": 0.2,
                    "shape": "cosine",
                    "wavelength": 1,
                    "softness": 1,
                },
                "palette": "rainbow",
                "brightness": 0.7,
            },
        },
        "blend": "normal",
    })
    compiled = compile_layers([layer], topo)
    assert len(compiled) == 1


# ---- color helpers ----


def test_hex_to_rgb01_rejects_bad_hex():
    with pytest.raises(ValueError):
        hex_to_rgb01("not-hex")
    with pytest.raises(ValueError):
        hex_to_rgb01("#1234")
