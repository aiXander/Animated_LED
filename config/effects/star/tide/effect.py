class Tide(Effect):
    """Water rising from the hub along every leg and falling back, with a
    bright line of foam at its edge. The legs run out of step, like waves
    reaching different parts of a shore. Bass pushes the water a little further."""

    EDGE = 0.02         # softness of the water's edge (fraction of the radius)
    FOAM = 0.07         # how far the foam's glow reaches back into the water

    def init(self, ctx):
        n = ctx.n
        self.r = ctx.frames.radius
        self.leg = ctx.frames.leg
        k = ctx.rig.n_legs
        self.leg_off = np.arange(k, dtype=np.float32) / k
        self.phase = 0.0
        self._pal = None
        self._lvl = np.empty(n, dtype=np.float32)   # the water's edge on each LED's leg
        self._d = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # Brightness comes from the water below, so lift the palette's
            # dark tones to full colour (a black entry stays black).
            self.lut = lut / np.maximum(lut.max(axis=1, keepdims=True), 0.3)
            self._pal = p.palette
        self.phase = (self.phase + dt / float(p.period)) % 1.0

        # One water level per leg, then spread to that leg's LEDs.
        lo, hi = float(p.low_tide), float(p.high_tide)
        lvl = 0.5 - 0.5 * np.cos(TAU * (self.phase + float(p.leg_offset) * self.leg_off))
        lvl = lo + (hi - lo) * lvl + 0.1 * float(p.bass_push) * float(ctx.audio.low)
        np.take(lvl.astype(np.float32), self.leg, out=self._lvl)

        d, g = self._d, self._g
        np.subtract(self._lvl, self.r, out=d)       # depth under the edge; < 0 is dry
        np.multiply(d, 1.0 / self.EDGE, out=g)
        np.clip(g, 0.0, 1.0, out=g)                 # 0 dry -> 1 wet
        np.maximum(d, 0.0, out=d)
        d *= -1.0 / self.FOAM
        np.exp(d, out=d)                            # 1 at the edge, fading with depth

        # Colour: the pale end of the palette at the foam, deeper tones below.
        body = float(p.body)
        self._idx[:] = (0.35 + 0.65 * d) * (LUT_SIZE - 1)
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="clip")
        d *= 1.0 - body
        d += body
        g *= d
        self.out *= g[:, None]
        return self.out
