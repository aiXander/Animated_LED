import numpy as np


def hex_to_rgb01(s: str) -> np.ndarray:
    """Parse a #rrggbb (or rrggbb) hex colour into a float32 RGB array in [0, 1]."""
    raw = s.lstrip("#")
    if len(raw) != 6:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}")
    try:
        r = int(raw[0:2], 16)
        g = int(raw[2:4], 16)
        b = int(raw[4:6], 16)
    except ValueError as e:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}") from e
    return np.array([r / 255.0, g / 255.0, b / 255.0], dtype=np.float32)
