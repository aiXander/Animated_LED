from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from .config import AppConfig, StripConfig

if TYPE_CHECKING:
    from .audio.state import AudioState


@dataclass(frozen=True)
class LEDInfo:
    global_index: int
    strip_id: str
    local_index: int
    position: tuple[float, float, float]


@dataclass
class Topology:
    """Spatial model of the install.

    Holds, for each LED, its global pixel-buffer index and a 3D position in
    metres. Effects don't read this directly — they read `normalised_positions`
    so a "left → right" effect doesn't depend on strip count, length, or
    reversal. The mapping from chain-order (local_index) to space respects the
    per-strip `reversed` flag.
    """

    leds: list[LEDInfo]
    positions: np.ndarray  # (N, 3) float32, metres
    normalised_positions: np.ndarray  # (N, 3) float32, in [-1, 1] per axis
    bbox_min: np.ndarray  # (3,) float32
    bbox_max: np.ndarray  # (3,) float32
    pixel_count: int
    strips: list[StripConfig]
    # Optional audio analysis snapshot, set by Engine.attach_audio. Effects
    # that want audio reactivity read it via `self.topology.audio_state`.
    audio_state: "AudioState | None" = field(default=None, repr=False)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "Topology":
        if not cfg.strips:
            raise ValueError("config has no strips")

        total = max(s.pixel_offset + s.pixel_count for s in cfg.strips)
        # Sparse construction — fill by global index so any gaps stay zeroed.
        positions = np.zeros((total, 3), dtype=np.float32)
        leds: list[LEDInfo | None] = [None] * total

        for strip in cfg.strips:
            start = np.asarray(strip.geometry.start, dtype=np.float32)
            end = np.asarray(strip.geometry.end, dtype=np.float32)
            n = strip.pixel_count
            # local_index 0 is the first LED in the data chain.
            # Without reversed: local_index 0 -> start, local_index n-1 -> end.
            if n > 1:
                t = np.linspace(0.0, 1.0, n, dtype=np.float32)
            else:
                t = np.zeros(1, dtype=np.float32)
            if strip.reversed:
                t = 1.0 - t
            seg = start[None, :] + t[:, None] * (end - start)[None, :]
            for i in range(n):
                gi = strip.pixel_offset + i
                positions[gi] = seg[i]
                leds[gi] = LEDInfo(
                    global_index=gi,
                    strip_id=strip.id,
                    local_index=i,
                    position=(float(seg[i, 0]), float(seg[i, 1]), float(seg[i, 2])),
                )

        # Replace any unfilled holes (gaps in pixel_offset coverage) with placeholders.
        for gi in range(total):
            if leds[gi] is None:
                leds[gi] = LEDInfo(
                    global_index=gi,
                    strip_id="",
                    local_index=-1,
                    position=(0.0, 0.0, 0.0),
                )

        bbox_min = positions.min(axis=0)
        bbox_max = positions.max(axis=0)
        center = (bbox_min + bbox_max) / 2.0
        extent = (bbox_max - bbox_min) / 2.0
        # Avoid divide-by-zero on flat axes (e.g. all z=0 in a 2D layout).
        safe_extent = np.where(extent == 0, 1.0, extent).astype(np.float32)
        normalised = ((positions - center) / safe_extent).astype(np.float32)

        return cls(
            leds=[led for led in leds if led is not None],
            positions=positions,
            normalised_positions=normalised,
            bbox_min=bbox_min.astype(np.float32),
            bbox_max=bbox_max.astype(np.float32),
            pixel_count=total,
            strips=list(cfg.strips),
        )
