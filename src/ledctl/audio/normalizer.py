"""Per-feature rolling-window auto-gain.

The mic level out of the analyser depends on so many things — input device,
PortAudio gain, distance from speakers, room treatment — that a fixed scale
maps "loud music" to anywhere between 0.05 and 0.7. To make audio bindings
"just work" the dynamic range has to be inferred from what the mic is
actually hearing, not pinned to a hand-tuned `gain` per install.

`RollingNormalizer` keeps a circular buffer of the last `window_s` of values
and tracks a high-percentile estimate as the working ceiling. Live samples
are divided by that ceiling, so the recent dynamic range maps to ~[0, 1]
regardless of absolute mic level.

Why a percentile and not max:
- max would lock the ceiling to a single transient (a door slam, a clipped
  block) for the full window and crush the rest of the signal.
- 95th percentile means a brief spike doesn't move the ceiling much; only
  sustained loud content (>5% of the window) lifts it.

Behaviour at the edges:
- Sudden spike louder than the current ceiling: the live value triggers a
  snap-up of the ceiling so the normalised output stays in [0, 1] instead
  of clipping for the duration of the recompute interval. The percentile
  itself only catches up if the loud level persists in the buffer.
- Quiet stretch: as old loud samples scroll out of the window, the
  percentile drops and the ceiling decays naturally — sensitivity returns.
- Silence / no signal yet: a `min_ceiling` floor stops us from amplifying
  pure mic noise to full scale (and prevents division-by-zero).
"""

import numpy as np


class RollingNormalizer:
    """Maintains a rolling buffer of recent feature values and exposes a
    `step(x)` method that returns x normalised to the inferred ceiling.

    Designed to be called once per audio block (i.e. at `samplerate /
    blocksize` Hz). The percentile recompute is rate-limited so the cost
    is independent of block rate above ~10 Hz.
    """

    def __init__(
        self,
        window_s: float = 60.0,
        update_rate_hz: float = 100.0,
        min_ceiling: float = 0.02,
        percentile: float = 95.0,
    ):
        if window_s <= 0.0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        if update_rate_hz <= 0.0:
            raise ValueError(f"update_rate_hz must be > 0, got {update_rate_hz}")
        if not 0.0 < percentile <= 100.0:
            raise ValueError(f"percentile must be in (0, 100], got {percentile}")
        n = max(8, int(round(window_s * update_rate_hz)))
        self._buf = np.zeros(n, dtype=np.float32)
        self._idx = 0
        self._filled = 0
        self.window_s = float(window_s)
        self.min_ceiling = float(min_ceiling)
        self.percentile = float(percentile)
        self._ceiling = float(min_ceiling)
        # ~10 Hz percentile refresh — np.percentile on a few thousand floats
        # is cheap, but doing it every audio block is wasted CPU. The snap-up
        # branch in step() handles between-recompute spikes.
        self._recompute_every = max(1, int(round(update_rate_hz / 10.0)))
        self._frames_since = 0

    @property
    def ceiling(self) -> float:
        return self._ceiling

    @property
    def buffer_size(self) -> int:
        return int(self._buf.size)

    @property
    def filled(self) -> int:
        return int(self._filled)

    def reset(self) -> None:
        self._buf.fill(0.0)
        self._idx = 0
        self._filled = 0
        self._ceiling = self.min_ceiling
        self._frames_since = 0

    def step(self, x: float) -> float:
        """Append `x` to the rolling buffer and return its normalised value
        in [0, 1].

        Negative inputs are clamped to 0 (audio features are non-negative
        magnitudes; a NaN slipping through would otherwise poison the buffer).
        """
        v = float(x)
        if not np.isfinite(v) or v < 0.0:
            v = 0.0
        self._buf[self._idx] = v
        self._idx = (self._idx + 1) % self._buf.size
        if self._filled < self._buf.size:
            self._filled += 1

        self._frames_since += 1
        if self._frames_since >= self._recompute_every:
            view = self._buf[: self._filled]
            p = float(np.percentile(view, self.percentile)) if view.size else 0.0
            self._ceiling = max(p, self.min_ceiling)
            self._frames_since = 0

        # Snap the working ceiling up if the live value already exceeds it,
        # so a sudden loud transient doesn't clip until the next recompute.
        if v > self._ceiling:
            self._ceiling = v

        c = self._ceiling
        if c <= 0.0:
            return 0.0
        out = v / c
        if out > 1.0:
            return 1.0
        return out
