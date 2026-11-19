class HubEmber(Effect):
    """A hot hub cooling toward the tips. Slow noise drifts outward so the glow
    never sits still, each leg flickering on its own, and a beat pushes the
    heat a little further down the legs."""

    def init(self, ctx):
        n = ctx.n
        r = ctx.frames.radius
        leg = ctx.frames.leg
        self.r = r
        # Two noise waves along each leg. The `leg` term shifts them per leg,
        # so legs flicker apart on any number of legs.
        self.a1 = (TAU * (2.0 * r + 0.37 * leg)).astype(np.float32)
        self.a2 = (TAU * (5.0 * r + 0.61 * leg)).astype(np.float32)
        self.p1 = self.p2 = 0.0     # one wrapped clock per wave, never ctx.t
        self.env = 0.0              # beat envelope
        self._pal = None
        self._h = np.empty(n, dtype=np.float32)
        self._s = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            self.lut = named_palette(p.palette)     # colour table, read with np.take
            self._pal = p.palette
        self.env = max(self.env * float(np.exp(-dt / 0.8)), float(ctx.audio.beat))
        self.p1 = (self.p1 + dt * 0.11 * float(p.drift)) % 1.0
        self.p2 = (self.p2 + dt * 0.23 * float(p.drift)) % 1.0
        h, s = self._h, self._s
        np.subtract(self.a1, TAU * self.p1, out=h)  # minus: the wave moves outward
        np.sin(h, out=h)
        np.subtract(self.a2, TAU * self.p2, out=s)
        np.sin(s, out=s)
        h += s
        h *= 0.5 * float(p.flicker)
        h += 1.0                                    # noise around 1
        # Heat: 1 at the hub, 0 at `reach`; a beat pushes the reach outward.
        reach = float(p.reach) * (1.0 + float(p.beat_swell) * self.env)
        np.multiply(self.r, -1.0 / reach, out=s)
        s += 1.0
        h *= s
        np.clip(h, 0.0, 1.0, out=h)
        h *= LUT_SIZE - 1
        np.copyto(self._idx, h, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="clip")
        self.out *= float(p.glow)
        return self.out
