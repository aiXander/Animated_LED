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
    linear = pb.to_uint8(gamma=1.0)
    gamma_corrected = pb.to_uint8(gamma=2.2)
    # gamma 2.2 on 0.5 ≈ 0.218 -> 56
    assert int(linear[0, 0]) == 128
    assert int(gamma_corrected[0, 0]) < 70
    assert int(gamma_corrected[0, 0]) > 40
