class AuroraDrift(Effect):
    """Curtains of light drifting along the legs: two slow waves move against
    each other, and where they meet a soft band glows. Every leg runs out of
    step with the next, and the colours wander through the palette. Treble
    adds a faint shimmer."""

    def init(self, ctx):
        n = ctx.n
        r = ctx.frames.radius
        leg = ctx.frames.leg
        # Each wave's phase at each LED; the `leg` term keeps the legs apart.
        self.a1 = (TAU * (1.3 * r + 0.37 * leg)).astype(np.float32)
        self.a2 = (TAU * (2.1 * r + 0.61 * leg)).astype(np.float32)
        self.c0 = (0.35 * r + 0.23 * leg).astype(np.float32)   # colour position
        self.p1 = self.p2 = self.p3 = 0.0   # one wrapped clock per wave
        self._pal = None
        self._w1 = np.empty(n, dtype=np.float32)
        self._w2 = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # The bands set the brightness, so lift the palette's dark tones
            # to full colour (a black entry stays black).
            lut = lut / np.maximum(lut.max(axis=1, keepdims=True), 0.3)
            # Palette there-and-back, so the wrap point has no seam.
            self.lut = np.concatenate([lut, lut[::-1]])
            self._pal = p.palette
        sp = float(p.speed)
        self.p1 = (self.p1 + dt * 0.050 * sp) % 1.0     # drifts outward
        self.p2 = (self.p2 + dt * 0.031 * sp) % 1.0     # drifts inward
        self.p3 = (self.p3 + dt * 0.011 * sp) % 1.0     # colour

        w1, w2 = self._w1, self._w2
        scale = float(p.band_scale)
        np.multiply(self.a1, scale, out=w1)
        w1 -= TAU * self.p1
        np.sin(w1, out=w1)
        w1 *= 0.5
        w1 += 0.5
        np.multiply(self.a2, scale, out=w2)
        w2 += TAU * self.p2
        np.sin(w2, out=w2)
        w2 *= 0.5
        w2 += 0.5

        # Colour: palette position along the leg, pushed about by the first wave.
        size = self.lut.shape[0]
        self._idx[:] = (self.c0 * float(p.colour_spread) + self.p3 + 0.12 * w1) * size
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")

        # Brightness: a band glows where both waves are high. The second
        # line widens the bands so they fade in and out slowly.
        w1 *= w2
        np.subtract(2.0, w1, out=w2)
        w1 *= w2
        floor = float(p.floor)
        peak = float(p.peak) * (1.0 + float(p.shimmer) * float(ctx.audio.high))
        w1 *= max(0.0, min(1.0, peak) - floor)
        w1 += floor
        self.out *= w1[:, None]
        return self.out
