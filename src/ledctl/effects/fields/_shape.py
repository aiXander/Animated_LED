"""Shared 1-D shape evaluation. Used by `scroll` and `radial`.

Given a per-LED `phase` array (already wrapped to fract in [0, 1)), produce a
scalar in [0, 1] that the palette will look up.
"""

from typing import Literal

import numpy as np

ShapeName = Literal["cosine", "sawtooth", "pulse", "gauss"]


def apply_shape(
    phase: np.ndarray,
    shape: str,
    softness: float,
    width: float,
    out: np.ndarray,
) -> None:
    """Evaluate `shape` on a fract-phase array and write into `out`.

    Shapes:
      cosine   — smooth wave; `softness` lerps toward a hard square (0 = bands, 1 = wave).
      sawtooth — ramps 0→1 across the cycle; turns the palette into a gradient.
      pulse    — 50%-duty square, ignores `softness` and `width` (binary on/off).
      gauss    — single peak per cycle with exp falloff of `width` (smaller = sharper comet).
    """
    if shape == "cosine":
        smooth = (np.cos(2.0 * np.pi * phase) + 1.0) * 0.5
        if softness >= 1.0:
            np.copyto(out, smooth.astype(np.float32, copy=False))
        elif softness <= 0.0:
            np.copyto(out, (smooth > 0.5).astype(np.float32))
        else:
            hard = (smooth > 0.5).astype(np.float32)
            np.copyto(
                out,
                (softness * smooth + (1.0 - softness) * hard).astype(
                    np.float32, copy=False
                ),
            )
    elif shape == "sawtooth":
        np.copyto(out, phase.astype(np.float32, copy=False))
    elif shape == "pulse":
        np.copyto(out, (phase < 0.5).astype(np.float32))
    elif shape == "gauss":
        # Distance from a peak at phase=0 (after wrap).
        d = phase - np.round(phase)
        np.copyto(
            out,
            np.exp(-(d * d) / max(width * width, 1e-9)).astype(
                np.float32, copy=False
            ),
        )
    else:
        raise ValueError(f"unknown shape {shape!r}")
