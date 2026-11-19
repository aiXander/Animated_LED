class SlowBreathRadial(Effect):
    """A slow breath that swells at the hub and rolls out to the tips, in
    palette colours that lie along the legs and drift with time. Bass deepens
    the breath a little; nothing flashes."""

    def init(self, ctx):
        n = ctx.n
        self.r = ctx.frames.radius
        self.phase = 0.0        # the breath
        self.drift = 0.0        # the colour
        self._pal = None
        self._a = np.empty(n, dtype=np.float32)
        self._c = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # The breath sets the brightness, so lift the palette's dark tones
            # to full colour (a black entry stays black).
            lut = lut / np.maximum(lut.max(axis=1, keepdims=True), 0.3)
            # Palette there-and-back, so the wrap point has no seam.
            self.lut = np.concatenate([lut, lut[::-1]])
            self._pal = p.palette
        self.phase = (self.phase + dt / float(p.period)) % 1.0
        self.drift = (self.drift + dt * float(p.colour_drift) / 60.0) % 1.0

        # Brightness: one cosine wave travelling outward. 0 = breathed out.
        a, c = self._a, self._c
        np.multiply(self.r, -TAU * float(p.waves), out=a)
        a += TAU * self.phase
        np.cos(a, out=a)
        a *= 0.5
        a += 0.5
        floor = float(p.floor)
        peak = min(1.0, float(p.peak) + float(p.bass_lift) * float(ctx.audio.low))
        a *= max(0.0, peak - floor)
        a += floor

        # Colour: the palette along the legs, drifting.
        size = self.lut.shape[0]
        np.multiply(self.r, 0.5 * float(p.colour_spread) * size, out=c)
        c += self.drift * size
        np.copyto(self._idx, c, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")
        self.out *= a[:, None]
        return self.out
