import numpy as np

from ledctl.pixelbuffer import PixelBuffer


def test_clear_zeros():
    pb = PixelBuffer(8)
    pb.rgb[:] = 0.5
    pb.clear()
    assert (pb.rgb == 0.0).all()


def test_to_uint8_no_gamma_clips_and_rounds():
    pb = PixelBuffer(4)
    pb.rgb[:] = np.array([[0.0, 0.5, 1.0]] * 4, dtype=np.float32)
    out = pb.to_uint8(gamma=1.0)
    assert out.dtype == np.uint8
    # 0.5 * 255 + 0.5 = 128.0 -> 128 with rounding
    assert (out[:, 0] == 0).all()
    assert (out[:, 1] == 128).all()
    assert (out[:, 2] == 255).all()


def test_to_uint8_clips_overflows():
    pb = PixelBuffer(2)
    pb.rgb[:] = np.array([[-0.5, 1.5, 0.5]] * 2, dtype=np.float32)
    out = pb.to_uint8(gamma=1.0)
    assert (out[:, 0] == 0).all()
    assert (out[:, 1] == 255).all()


def test_gamma_darkens_midtones():
    pb = PixelBuffer(2)
    pb.rgb[:] = np.array([[0.5, 0.5, 0.5]] * 2, dtype=np.float32)
    # to_uint8 reuses an internal buffer; copy before calling again.
    linear = pb.to_uint8(gamma=1.0).copy()
    gamma_corrected = pb.to_uint8(gamma=2.2)
    # gamma 2.2 on 0.5 ≈ 0.218 -> 56
    assert int(linear[0, 0]) == 128
    assert int(gamma_corrected[0, 0]) < 70
    assert int(gamma_corrected[0, 0]) > 40


def test_gamma_follows_the_power_curve():
    """Gamma goes through a lookup table; it must stay within one output code
    of the exact curve over the whole range, out-of-range input included."""
    rng = np.random.default_rng(0)
    pb = PixelBuffer(2000)
    pb.rgb[:] = rng.uniform(-0.2, 1.2, size=(2000, 3)).astype(np.float32)
    exact = np.rint(np.clip(pb.rgb.astype(np.float64), 0.0, 1.0) ** 2.2 * 255.0)
    got = pb.to_uint8(gamma=2.2).astype(np.float64)
    assert np.abs(got - exact).max() <= 1.0
