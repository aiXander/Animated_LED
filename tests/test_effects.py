from pathlib import Path

import numpy as np
import pytest

from ledctl.audio.state import AudioState
from ledctl.config import load_config
from ledctl.effects import (
    NoiseEffect,
    NoiseParams,
    RadialEffect,
    RadialParams,
    ScrollEffect,
    ScrollParams,
    SparkleEffect,
    SparkleParams,
)
from ledctl.effects.modulator import (
    Bindings,
    Envelope,
    ModulatorSpec,
    raw_value,
)
from ledctl.effects.palette import (
    NAMED_PALETTES,
    PaletteSpec,
    PaletteStop,
    compile_lut,
    sample_lut,
)
from ledctl.effects.registry import get_effect_class, list_effects
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


@pytest.fixture
def topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _zeros(topo: Topology) -> np.ndarray:
    return np.zeros((topo.pixel_count, 3), dtype=np.float32)


# ---- registry ----


def test_registry_has_all_field_generators():
    names = set(list_effects())
    assert {"scroll", "radial", "sparkle", "noise"} == names
    assert get_effect_class("scroll") is ScrollEffect


# ---- palette ----


def test_named_palette_endpoints_match_first_and_last_stop():
    lut = compile_lut(PaletteSpec(name="fire"))
    assert lut.shape == (256, 3)
    # First named stop is black, last is light yellow.
    assert lut[0].sum() < 0.05
    assert lut[-1, 0] > 0.9 and lut[-1, 1] > 0.9


def test_palette_string_shorthand_resolves_to_named():
    spec = PaletteSpec.model_validate("rainbow")
    assert spec.name == "rainbow"
    assert spec.stops is None


def test_palette_mono_hex_is_solid_colour():
    lut = compile_lut(PaletteSpec(name="mono_ff7000"))
    expected = np.array([0xFF, 0x70, 0x00], dtype=np.float32) / 255.0
    assert np.allclose(lut[0], expected, atol=1e-5)
    assert np.allclose(lut[-1], expected, atol=1e-5)


def test_palette_unknown_name_rejected():
    with pytest.raises(ValueError):
        PaletteSpec(name="not-a-real-palette")


def test_palette_requires_exactly_one_of_name_or_stops():
    with pytest.raises(ValueError):
        PaletteSpec()
    with pytest.raises(ValueError):
        PaletteSpec(
            name="fire",
            stops=[
                PaletteStop(pos=0.0, color="#ff0000"),
                PaletteStop(pos=1.0, color="#0000ff"),
            ],
        )


def test_palette_custom_stops_interp():
    spec = PaletteSpec(
        stops=[
            PaletteStop(pos=0.0, color="#000000"),
            PaletteStop(pos=1.0, color="#ffffff"),
        ]
    )
    lut = compile_lut(spec)
    mid = lut[128]
    assert 0.4 < mid[0] < 0.6


def test_palette_named_palettes_all_compile():
    for name in NAMED_PALETTES:
        lut = compile_lut(PaletteSpec(name=name))
        assert lut.shape == (256, 3)
        assert (lut >= 0.0).all() and (lut <= 1.0 + 1e-5).all()


def test_sample_lut_hue_shift_wraps():
    lut = compile_lut(PaletteSpec(name="rainbow"))
    t = np.array([0.0], dtype=np.float32)
    rotated = sample_lut(lut, t, hue_shift=0.5)
    direct = sample_lut(lut, np.array([0.5], dtype=np.float32))
    assert np.allclose(rotated, direct, atol=1e-5)


# ---- modulator ----


def test_envelope_brightness_default_smoothing():
    spec = ModulatorSpec(source="const", value=1.0)
    env = Envelope(spec=spec, slot="brightness")
    # Brightness defaults: 30 ms attack, 500 ms release.
    assert env.attack_ms == 30.0
    assert env.release_ms == 500.0


def test_envelope_attack_then_release():
    spec = ModulatorSpec(
        source="const", value=0.0, attack_ms=100.0, release_ms=1000.0,
    )
    env = Envelope(spec=spec, slot="brightness")
    env.step(0.0, 0.0)
    # After ~one attack tau the envelope reaches 1 - 1/e ≈ 0.63 of the step.
    v_attack = env.step(1.0, 0.1)
    assert 0.55 < v_attack < 0.70
    # Hold near 1, then release.
    for i in range(1, 11):
        env.step(1.0, 0.1 + i * 0.05)
    after_release = env.step(0.0, 0.1 + 0.5 + 1.0)
    assert 0.30 < after_release < 0.45


def test_envelope_floor_ceiling_mapping():
    spec_hi = ModulatorSpec(source="const", value=1.0, floor=0.5, ceiling=1.0)
    env_hi = Envelope(spec=spec_hi, slot="brightness")
    env_hi.step(1.0, 0.0)
    v_hi = env_hi.step(1.0, 1.0)
    assert abs(v_hi - 1.0) < 0.01

    spec_lo = ModulatorSpec(source="const", value=0.0, floor=0.5, ceiling=1.0)
    env_lo = Envelope(spec=spec_lo, slot="brightness")
    v_lo = env_lo.step(0.0, 0.0)
    assert abs(v_lo - 0.5) < 0.01


def test_raw_value_audio_band():
    # Modulators read the rolling-window-normalised field, not the raw level.
    state = AudioState(
        rms=0.3, low=0.5, mid=0.7, high=0.1,
        rms_norm=0.3, low_norm=0.5, mid_norm=0.7, high_norm=0.1,
    )
    spec = ModulatorSpec(source="audio.low")
    assert raw_value(spec, 0.0, state) == 0.5


def test_raw_value_audio_no_state_returns_zero():
    spec = ModulatorSpec(source="audio.rms")
    assert raw_value(spec, 0.0, None) == 0.0


def test_raw_value_lfo_sin_cycles():
    spec = ModulatorSpec(source="lfo.sin", period_s=1.0)
    a = raw_value(spec, 0.0, None)
    b = raw_value(spec, 0.5, None)
    # Half-cycle apart, sin output is symmetric around 0.5.
    assert abs((a + b) - 1.0) < 1e-5


# ---- scroll ----


def test_scroll_bounded_and_moves(topo: Topology):
    eff = ScrollEffect(
        ScrollParams(speed=1.0, wavelength=0.5, palette=PaletteSpec(name="rainbow")),
        topo,
    )
    a, b = _zeros(topo), _zeros(topo)
    eff.render(0.0, a)
    eff.render(0.4, b)
    assert (a >= 0.0).all() and (a <= 1.0 + 1e-5).all()
    assert not np.allclose(a, b)


def test_scroll_static_when_speed_zero(topo: Topology):
    eff = ScrollEffect(
        ScrollParams(speed=0.0, palette=PaletteSpec(name="fire")), topo
    )
    a, b = _zeros(topo), _zeros(topo)
    eff.render(0.0, a)
    eff.render(2.5, b)
    assert np.allclose(a, b)


def test_scroll_cross_phase_offsets_y_rows(topo: Topology):
    plain = ScrollEffect(
        ScrollParams(
            speed=1.0,
            cross_phase=(0.0, 0.0, 0.0),
            palette=PaletteSpec(name="rainbow"),
        ),
        topo,
    )
    shifted = ScrollEffect(
        ScrollParams(
            speed=1.0,
            cross_phase=(0.0, 0.25, 0.0),
            palette=PaletteSpec(name="rainbow"),
        ),
        topo,
    )
    a, b = _zeros(topo), _zeros(topo)
    plain.render(0.0, a)
    shifted.render(0.0, b)
    top_i = 100
    bot_i = 900 + 100
    assert np.allclose(
        topo.normalised_positions[top_i, 0],
        topo.normalised_positions[bot_i, 0],
        atol=1e-4,
    )
    assert np.allclose(a[top_i], a[bot_i], atol=1e-3)
    assert np.linalg.norm(b[top_i] - b[bot_i]) > 0.05


def test_scroll_brightness_binding_scales_output(topo: Topology):
    state = AudioState(rms=0.5, rms_norm=0.5)
    topo.audio_state = state
    bindings = Bindings(
        brightness=ModulatorSpec(
            source="audio.rms", floor=0.0, ceiling=1.0, gain=1.0,
            attack_ms=0.0, release_ms=0.0,
        )
    )
    eff = ScrollEffect(
        ScrollParams(
            speed=0.0, palette=PaletteSpec(name="white"), bindings=bindings,
        ),
        topo,
    )
    out = _zeros(topo)
    eff.render(0.0, out)
    # White palette × brightness 0.5 → all channels ≈ 0.5.
    assert np.allclose(out, 0.5, atol=1e-3)


def test_scroll_brightness_floor_when_silent(topo: Topology):
    # Mirrors the boot-stack ask: RMS 0 → brightness floor 0.5.
    state = AudioState(rms=0.0)
    topo.audio_state = state
    bindings = Bindings(
        brightness=ModulatorSpec(
            source="audio.rms", floor=0.5, ceiling=1.0, gain=4.0,
            attack_ms=0.0, release_ms=0.0,
        )
    )
    eff = ScrollEffect(
        ScrollParams(
            speed=0.0, palette=PaletteSpec(name="white"), bindings=bindings,
        ),
        topo,
    )
    out = _zeros(topo)
    eff.render(0.0, out)
    assert np.allclose(out, 0.5, atol=1e-3)


def test_scroll_speed_binding_overrides_static(topo: Topology):
    bindings = Bindings(
        speed=ModulatorSpec(
            source="const", value=1.0, floor=0.0, ceiling=1.0,
            attack_ms=0.0, release_ms=0.0,
        )
    )
    eff = ScrollEffect(
        ScrollParams(
            speed=0.0, palette=PaletteSpec(name="rainbow"), bindings=bindings,
        ),
        topo,
    )
    a, b = _zeros(topo), _zeros(topo)
    eff.render(0.0, a)
    eff.render(0.5, b)
    assert not np.allclose(a, b)


# ---- radial ----


def test_radial_bounded_and_moves(topo: Topology):
    eff = RadialEffect(
        RadialParams(speed=0.5, palette=PaletteSpec(name="ice")), topo
    )
    a, b = _zeros(topo), _zeros(topo)
    eff.render(0.0, a)
    eff.render(0.5, b)
    assert (a >= 0.0).all() and (a <= 1.0 + 1e-5).all()
    assert not np.allclose(a, b)


# ---- sparkle ----


def test_sparkle_bounded_and_lights_some_pixels(topo: Topology):
    eff = SparkleEffect(
        SparkleParams(
            density=2.0, decay=0.5, seed=7, palette=PaletteSpec(name="white"),
        ),
        topo,
    )
    out = _zeros(topo)
    eff.render(0.0, out)
    eff.render(0.1, out)
    assert (out >= 0.0).all() and (out <= 1.0 + 1e-5).all()
    assert out.sum() > 0.0


def test_sparkle_seed_reproducible(topo: Topology):
    a = SparkleEffect(
        SparkleParams(density=1.0, seed=42, palette=PaletteSpec(name="white")),
        topo,
    )
    b = SparkleEffect(
        SparkleParams(density=1.0, seed=42, palette=PaletteSpec(name="white")),
        topo,
    )
    out_a, out_b = _zeros(topo), _zeros(topo)
    a.render(0.0, out_a)
    a.render(0.5, out_a)
    b.render(0.0, out_b)
    b.render(0.5, out_b)
    assert np.allclose(out_a, out_b)


# ---- noise ----


def test_noise_bounded_and_moves(topo: Topology):
    eff = NoiseEffect(
        NoiseParams(speed=0.5, scale=0.5, palette=PaletteSpec(name="ocean"), seed=3),
        topo,
    )
    a, b = _zeros(topo), _zeros(topo)
    eff.render(0.0, a)
    eff.render(0.5, b)
    assert (a >= 0.0).all() and (a <= 1.0 + 1e-5).all()
    assert not np.allclose(a, b)


def test_noise_static_when_speed_zero(topo: Topology):
    eff = NoiseEffect(
        NoiseParams(speed=0.0, palette=PaletteSpec(name="rainbow"), seed=1),
        topo,
    )
    a, b = _zeros(topo), _zeros(topo)
    eff.render(0.0, a)
    eff.render(1.0, b)
    assert np.allclose(a, b)


# ---- validation ----


def test_invalid_hex_in_palette_rejected():
    with pytest.raises(ValueError):
        PaletteStop(pos=0.5, color="not-hex")
