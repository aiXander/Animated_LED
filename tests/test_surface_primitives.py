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
        "constant", "palette_named", "palette_stops",
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


def test_audio_band_reads_lmh_value():
    """The external feed publishes low/mid/high already auto-scaled in [0, 1].
    audio_band reads them straight off AudioState, no extra normalisation."""
    state = AudioState(low=0.5, mid=0.7, high=0.1)
    cls = REGISTRY["audio_band"]
    params = cls.Params.model_validate({"band": "low"})
    node = cls.compile(params, None, None)
    assert node.render(RenderContext(audio=state)) == 0.5
    params = cls.Params.model_validate({"band": "mid"})
    node = cls.compile(params, None, None)
    assert node.render(RenderContext(audio=state)) == 0.7


def test_audio_band_rejects_rms_and_peak():
    """RMS / peak are deliberately not exposed — pick a frequency band."""
    cls = REGISTRY["audio_band"]
    for forbidden in ("rms", "peak"):
        try:
            cls.Params.model_validate({"band": forbidden})
        except Exception:
            continue
        raise AssertionError(f"band={forbidden!r} should be rejected")


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


# ---- frames (Phase B) ------------------------------------------------------


def test_topology_exposes_named_frames(topo: Topology):
    """The frame builder fills `topology.derived` at config-load time."""
    expected = {
        "x", "y", "z", "signed_x", "signed_y", "signed_z",
        "radius", "angle", "u_loop", "u_loop_signed",
        "side_top", "side_bottom", "side_signed",
        "axial_dist", "axial_signed", "corner_dist",
        "strip_id", "chain_index", "distance",
    }
    assert expected.issubset(set(topo.derived))


def test_u_loop_walks_clockwise_from_top_centre(topo: Topology):
    """`u_loop` should be 0 at the top-centre chain head, ~0.25 at the
    outer right edge, ~0.5 at the bottom centre, ~0.75 at the outer left
    edge, ~1 wrapping back to top centre."""
    u = topo.derived["u_loop"]
    pos = topo.normalised_positions

    # Top-right strip: pixel 450 (offset start) is at (0, 1) — top centre.
    assert u[450] == 0.0
    # Top-right strip ends at the outer right edge — should be ~0.25.
    assert 0.24 < u[899] < 0.26

    # Bottom-right strip is walked reversed, so its outer end (pixel 1799,
    # at x=1, y=-1) is right after top-right ends — ~0.25.
    assert 0.24 < u[1799] < 0.26
    # And its centre end (pixel 1350) sits at the half-way point.
    assert 0.49 < u[1350] < 0.51

    # Bottom-left walked forward: centre end (pixel 900) at ~0.5, outer at ~0.75.
    assert 0.49 < u[900] < 0.51
    assert 0.74 < u[1349] < 0.76

    # Top-left walked reversed: outer (pixel 449, at x=-1, y=1) ~0.75, centre back to 1.
    assert 0.74 < u[449] < 0.76
    assert u[0] == 1.0

    # And the two co-located top-centre LEDs (pixels 0 and 450) sit at u=0 and u=1.
    assert pos[0, 0] == pos[450, 0] == 0.0
    assert pos[0, 1] == pos[450, 1]


def test_axial_signed_matches_x_under_centre_normalisation(topo: Topology):
    # axial_signed and signed_x are both x in [-1, 1]; the rig has no z so
    # they should agree exactly modulo float casting.
    np.testing.assert_allclose(
        topo.derived["axial_signed"],
        topo.derived["signed_x"],
    )


def test_side_top_and_side_bottom_partition_the_install(topo: Topology):
    s_top = topo.derived["side_top"]
    s_bot = topo.derived["side_bottom"]
    # No LED is on both sides; together they cover everyone except y=0.
    assert ((s_top + s_bot) <= 1.0).all()
    # On the dev rig, exactly half the LEDs are on each row.
    assert int(s_top.sum()) == 900
    assert int(s_bot.sum()) == 900


def test_frame_primitive_renders_named_axis(topo: Topology):
    """The new `frame` primitive picks any named axis from the topology."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "palette_lookup",
            "params": {
                "scalar": {"kind": "frame", "params": {"axis": "u_loop"}},
                "palette": "rainbow",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    out = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    assert out.shape == (topo.pixel_count, 3)


def test_wave_axis_accepts_u_loop(topo: Topology):
    """A wave along `u_loop` runs around the perimeter without complaint."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "palette_lookup",
            "params": {
                "scalar": {
                    "kind": "wave",
                    "params": {
                        "axis": "u_loop",
                        "speed": 0.3,
                        "wavelength": 1.0,
                    },
                },
                "palette": "rainbow",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    out = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    # Two LEDs co-located at top centre (offsets 0 and 450) should match.
    np.testing.assert_allclose(out[0], out[450], atol=1e-5)


def test_unknown_axis_raises_compile_error(topo: Topology):
    from ledctl.surface import CompileError, Compiler, LayerSpec, NodeSpec

    layer = LayerSpec(node=NodeSpec(
        kind="palette_lookup",
        params={
            "scalar": {"kind": "frame", "params": {"axis": "not_a_frame"}},
            "palette": "rainbow",
        },
    ))
    with pytest.raises(CompileError) as exc:
        Compiler(topo).compile_layers([layer])
    assert "not_a_frame" in str(exc.value)


def test_generate_docs_includes_frames_block():
    from ledctl.surface import generate_docs

    docs = generate_docs()
    assert "FRAMES" in docs
    assert "u_loop" in docs
    assert "axial_dist" in docs


# ---- beat-aware time primitives (Phase C) ---------------------------------


def test_no_hardcoded_bpm_primitives_exposed():
    """The LLM must never see bpm/clock/tempo primitives — they hardcode
    a tempo and produce drift. Beat-sync goes through `audio_beat()`."""
    from ledctl.surface import REGISTRY

    for kind in ("bpm_clock", "beat_count", "audio_bpm"):
        assert kind not in REGISTRY, (
            f"{kind!r} must not be in the primitive registry; the LLM "
            "would otherwise hardcode a BPM. Use audio_beat / "
            "beat_envelope / beat_index instead."
        )


def test_beat_index_increments_on_each_audio_beat_and_wraps_mod_n():
    """beat_index counts real `/audio/beat` rising edges, not a hardcoded
    BPM clock. mod_n=4 wraps 0→1→2→3→0."""
    from ledctl.surface import REGISTRY

    cls = REGISTRY["beat_index"]
    compiled = cls.compile(cls.Params(mod_n=4), topology=None, compiler=None)
    masters = MasterControls()
    state = AudioState()
    ctx = RenderContext(t=0.0, wall_t=0.0, audio=state, masters=masters)
    # First read establishes baseline at the current count.
    assert compiled.render(ctx) == 0.0
    for expected in (1, 2, 3, 0, 1, 2, 3, 0):
        state.beat_count += 1
        assert int(compiled.render(ctx)) == expected


def test_step_select_picks_value_by_beat_index(topo: Topology):
    """The classic direction-flip pattern: alternate +1/-1 each beat.

    Index source is `beat_index`, driven by the live audio_beat counter
    (no hardcoded BPM)."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "palette_lookup",
            "params": {
                "scalar": {
                    "kind": "wave",
                    "params": {
                        "axis": "u_loop",
                        "speed": {
                            "kind": "step_select",
                            "params": {
                                "index": {
                                    "kind": "beat_index",
                                    "params": {"mod_n": 2},
                                },
                                "values": [0.5, -0.5],
                            },
                        },
                        "wavelength": 1.0,
                    },
                },
                "palette": "rainbow",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    out = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, audio=AudioState(), masters=MasterControls())
    )
    assert out.shape == (topo.pixel_count, 3)


# ---- particle primitives (Phase D) ----------------------------------------


def test_comet_head_brighter_than_tail(topo: Topology):
    """The pixel under the comet head should outshine pixels far behind it."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "comet",
            "params": {
                "axis": "u_loop",
                "speed": 0.0,  # static so we know exactly where the head is
                "head_size": 0.05,
                "trail_length": 0.3,
                "palette": "white",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    out = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    # u_loop=0 lives at pixel 450 (top centre, top_right strip start).
    head_brightness = out[450].max()
    # u_loop=0.5 (bottom centre) is half-way around — should be ~dark.
    far_brightness = out[1350].max()
    assert head_brightness > 0.5
    assert head_brightness > far_brightness * 5


def test_comet_trigger_resets_head_to_spawn_position(topo: Topology):
    """Beat-triggered comet: head walks outward from spawn_position on each
    `audio_beat()` rising edge. With axis=axial_dist + spawn_position=0,
    the head sits at the rig centre on each beat then sweeps outward."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "comet",
            "params": {
                "axis": "axial_dist",
                "spawn_position": 0.0,
                "speed": 1.0,  # 1 axial-unit / sec
                "head_size": 0.04,
                "trail_length": 0.0,  # head only — easier to assert on
                "trigger": {"kind": "audio_beat", "params": {}},
                "palette": "white",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    masters = MasterControls()
    state = AudioState()

    # No beats yet → legacy continuous mode (head walks from t=0).
    pre = compiled[0].node.render(
        RenderContext(t=10.0, wall_t=10.0, audio=state, masters=masters)
    ).copy()
    pre_max = pre.max()

    # First beat → reset. Sample one frame later → head sits ~at axial=0
    # (centre column LEDs). Pixels at axial=0 should now dominate.
    state.beat_count += 1
    on = compiled[0].node.render(
        RenderContext(t=10.001, wall_t=10.001, audio=state, masters=masters)
    ).copy()
    # Quarter-beat later, no new beat: head has walked outward by ~0.25.
    later = compiled[0].node.render(
        RenderContext(t=10.25, wall_t=10.25, audio=state, masters=masters)
    ).copy()

    # Centre-column LEDs (low axial_dist) and outer LEDs (high axial_dist):
    from ledctl.surface.frames import build_frames

    frames = build_frames(
        normalised_positions=topo.normalised_positions,
        leds=[],
        strips=topo.strips,
        pixel_count=topo.pixel_count,
    )
    ax = frames["axial_dist"]
    centre_mask = ax < 0.05
    outer_mask = ax > 0.5
    # Right after the beat, centre LEDs should be brightest.
    assert on[centre_mask].max() > 0.5
    assert on[centre_mask].max() > on[outer_mask].max() * 3
    # A quarter-second later the head has moved away from centre — the
    # mid-axis pixels should now be the bright ones.
    mid_mask = (ax > 0.2) & (ax < 0.35)
    assert later[mid_mask].max() > 0.5
    # Sanity: pre-beat continuous mode produced *some* output too (it's not
    # a no-op without triggers).
    assert pre_max > 0.0


def test_chase_dots_count_controls_distinct_peaks(topo: Topology):
    """A 4-dot chase should give 4 distinct peaks at t=0."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "chase_dots",
            "params": {
                "axis": "u_loop",
                "count": 4,
                "width": 0.02,
                "speed": 0.0,  # static
                "palette": "white",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    out = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    # 4 dots × gaussian width 0.02 over 1800 LEDs lights up tens of pixels
    # near each peak. Verify there are at least 4 separate bright clusters
    # by counting distinct rising edges through the >0.5 threshold.
    bright = out[:, 0]  # white palette → R=G=B
    above = bright > 0.5
    rising_edges = int(np.sum(above[1:] & ~above[:-1]))
    assert rising_edges == 4, (
        f"expected exactly 4 dot peaks, got {rising_edges} rising edges "
        f"(total bright LEDs = {int(above.sum())})"
    )


def test_ripple_emits_at_rate_and_uses_pool(topo: Topology):
    """Ripple's Poisson emission produces concurrent rings under high rate."""
    from ledctl.surface import compile_layers

    layer = LayerSpec.model_validate({
        "node": {
            "kind": "ripple",
            "params": {
                # axial_dist spans [0, 1] across the rig (centre column → outer
                # edge), so a ripple emitted at age=0 reaches LEDs immediately.
                "axis": "axial_dist",
                "rate": 50.0,
                "speed": 0.5,
                "decay_s": 1.0,
                "palette": "ice",
                "seed": 7,
            },
        },
    })
    compiled = compile_layers([layer], topo)
    masters = MasterControls()
    # Step a few frames so the Poisson process emits multiple rings.
    last = None
    for i in range(60):
        t = i / 60.0
        last = compiled[0].node.render(
            RenderContext(t=t, wall_t=t, masters=masters)
        ).copy()
    assert last is not None
    assert (last.sum(axis=1) > 0.05).any(), (
        f"ripple produced no visible output across 60 frames (max={last.max()})"
    )


def test_ripple_seed_reproducible(topo: Topology):
    from ledctl.surface import compile_layers

    def render_seq(seed):
        layer = LayerSpec.model_validate({
            "node": {
                "kind": "ripple",
                "params": {
                    "axis": "axial_dist",
                    "rate": 10.0,
                    "speed": 0.4,
                    "decay_s": 1.0,
                    "palette": "ice",
                    "seed": seed,
                },
            },
        })
        compiled = compile_layers([layer], topo)
        masters = MasterControls()
        out = None
        for i in range(20):
            t = i / 60.0
            out = compiled[0].node.render(
                RenderContext(t=t, wall_t=t, masters=masters)
            ).copy()
        return out

    a = render_seq(42)
    b = render_seq(42)
    np.testing.assert_array_equal(a, b)


# ---- recipes (Phase E) ----------------------------------------------------


def test_breathing_recipe_compiles_and_renders(topo: Topology):
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "breathing",
            "params": {
                "palette": "warm",
                "period_s": 4.0,
                "floor": 0.3,
            },
        },
    })
    compiled = compile_layers([layer], topo)
    out = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, masters=MasterControls())
    )
    assert out.shape == (topo.pixel_count, 3)
    # Floor ≥ 0.3 means the install should never go fully dark.
    assert out.max() > 0.0


def test_strobe_recipe_flashes_on_audio_beat(topo: Topology):
    """The strobe recipe is beat-driven: dark before any beat lands, bright
    on each beat trigger, decayed shortly after."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "strobe",
            "params": {
                "decay_s": 0.1,
                "shape": "exp",
                "palette": "white",
            },
        },
    })
    compiled = compile_layers([layer], topo)
    masters = MasterControls()
    state = AudioState()

    # Before any beat: stays dark (envelope sits at ~0).
    pre = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, audio=state, masters=masters)
    ).copy()
    assert pre.max() < 0.05

    # Beat fires → next frame is bright.
    state.beat_count += 1
    on = compiled[0].node.render(
        RenderContext(t=0.005, wall_t=0.005, audio=state, masters=masters)
    ).copy()
    assert on.max() > 0.5

    # ~0.5 s later (>> decay_s) without further beats: faded back to dark.
    off = compiled[0].node.render(
        RenderContext(t=0.5, wall_t=0.5, audio=state, masters=masters)
    ).copy()
    assert off.max() < 0.05


# ---- audio_beat / beat_envelope + ripple trigger --------------------------


def test_audio_beat_returns_zero_until_packets_arrive():
    """No audio bridge → silent. First read establishes baseline."""
    from ledctl.surface import REGISTRY

    cls = REGISTRY["audio_beat"]
    compiled = cls.compile(cls.Params(), topology=None, compiler=None)
    masters = MasterControls()
    # Without any audio_state, primitive returns 0 for every render.
    assert compiled.render(RenderContext(t=0.0, wall_t=0.0, masters=masters)) == 0.0
    assert compiled.render(RenderContext(t=0.1, wall_t=0.1, masters=masters)) == 0.0


def test_audio_beat_detects_rising_edges_in_beat_count():
    from ledctl.surface import REGISTRY

    cls = REGISTRY["audio_beat"]
    compiled = cls.compile(cls.Params(), topology=None, compiler=None)
    state = AudioState()
    masters = MasterControls()

    # First read: baseline established at beat_count=0; returns 0.
    ctx = RenderContext(t=0.0, wall_t=0.0, audio=state, masters=masters)
    assert compiled.render(ctx) == 0.0

    # One beat lands between frames → render returns 1.
    state.beat_count += 1
    assert compiled.render(ctx) == 1.0

    # Same count next frame → 0 (no new beat).
    assert compiled.render(ctx) == 0.0

    # Two beats land before the next render → returns 2 (no drops).
    state.beat_count += 2
    assert compiled.render(ctx) == 2.0


def test_beat_envelope_retriggers_and_decays():
    """beat_envelope: 1.0 right after a beat, decays toward 0 by decay_s."""
    from ledctl.surface import REGISTRY

    cls = REGISTRY["beat_envelope"]
    compiled = cls.compile(
        cls.Params(decay_s=0.2, hold_s=0.0, shape="exp"), None, None
    )
    masters = MasterControls()
    state = AudioState()

    # First read with no beats yet → 0 (envelope idle).
    ctx0 = RenderContext(t=0.0, wall_t=0.0, audio=state, masters=masters)
    assert compiled.render(ctx0) == 0.0

    # A beat fires, sample immediately → near 1.
    state.beat_count += 1
    ctx1 = RenderContext(t=0.001, wall_t=0.001, audio=state, masters=masters)
    assert compiled.render(ctx1) > 0.95

    # Half the decay later (no new beat) → between ~0.05 and 0.5 (exp curve).
    ctx2 = RenderContext(t=0.1, wall_t=0.1, audio=state, masters=masters)
    mid = compiled.render(ctx2)
    assert 0.05 < mid < 0.5

    # Well past decay_s → effectively zero.
    ctx3 = RenderContext(t=0.6, wall_t=0.6, audio=state, masters=masters)
    assert compiled.render(ctx3) < 0.02

    # New beat retriggers to ~1 again.
    state.beat_count += 1
    ctx4 = RenderContext(t=0.601, wall_t=0.601, audio=state, masters=masters)
    assert compiled.render(ctx4) > 0.95


def test_beat_envelope_square_shape_holds_then_drops():
    from ledctl.surface import REGISTRY

    cls = REGISTRY["beat_envelope"]
    compiled = cls.compile(
        cls.Params(decay_s=0.2, hold_s=0.05, shape="square"), None, None
    )
    masters = MasterControls()
    state = AudioState()
    # First read establishes baseline.
    compiled.render(
        RenderContext(t=0.0, wall_t=0.0, audio=state, masters=masters)
    )
    state.beat_count += 1
    # Within hold_s → full on.
    on = compiled.render(
        RenderContext(t=0.001, wall_t=0.001, audio=state, masters=masters)
    )
    assert on == 1.0
    # Past hold_s → square shape goes straight to 0.
    off = compiled.render(
        RenderContext(t=0.1, wall_t=0.1, audio=state, masters=masters)
    )
    assert off == 0.0


def test_ripple_trigger_emits_one_per_rising_edge(topo: Topology):
    """Wire `audio_beat()` into ripple.trigger and confirm an upstream beat
    produces a fresh ripple even with `rate=0` (no Poisson process running)."""
    layer = LayerSpec.model_validate({
        "node": {
            "kind": "ripple",
            "params": {
                "axis": "axial_dist",
                "rate": 0.0,
                "trigger": {"kind": "audio_beat", "params": {}},
                "speed": 0.5,
                "decay_s": 1.5,
                "palette": "ice",
                "seed": 1,
            },
        },
    })
    compiled = compile_layers([layer], topo)
    state = AudioState()
    masters = MasterControls()

    # No beats yet → no rings, nothing draws.
    out0 = compiled[0].node.render(
        RenderContext(t=0.0, wall_t=0.0, audio=state, masters=masters)
    ).copy()
    assert out0.max() < 1e-6

    # Upstream onset arrives.
    state.beat_count += 1
    # One frame later, the ripple should be near radius=0 (just born).
    out1 = compiled[0].node.render(
        RenderContext(t=0.05, wall_t=0.05, audio=state, masters=masters)
    ).copy()
    assert out1.max() > 0.05, (
        f"audio_beat → ripple chain produced no light (max={out1.max()})"
    )
