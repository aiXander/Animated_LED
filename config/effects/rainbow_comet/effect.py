class RainbowComet(Effect):
    """Rainbow comets circling the rig. Hue runs along each tail, bass lifts the
    brightness and every beat can jump the whole rainbow along the colour wheel."""

    EDGE = 0.0035       # soft leading edge of a head, in loop units (~6 LEDs)

    def init(self, ctx):
        n = ctx.n
        self.u = ctx.frames.u_loop
        # The HSV wheel as a table: it is cyclic, so mode="wrap" is seamless.
        self.wheel = hsv_to_rgb(np.arange(1024, dtype=np.float32) / 1024.0, 1.0, 1.0)
        self.phase = 0.0
        self.hue = 0.0          # eased hue offset
        self.hue_to = 0.0       # where the beats have pushed it
        self._q = np.empty(n, dtype=np.float32)
        self._r = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        sgn = 1.0 if p.direction == "Clockwise" else -1.0
        count = int(p.comets)
        self.phase = (self.phase + dt * float(p.speed) * sgn) % 1.0

        # Beats push a hue target; the hue eases after it instead of jumping.
        if ctx.audio.beat > 0:
            self.hue_to += float(p.beat_hue_shift)
        self.hue += (self.hue_to - self.hue) * (1.0 - float(np.exp(-dt / 0.08)))
        if self.hue > 1.0:
            self.hue -= 1.0
            self.hue_to -= 1.0

        # q = distance behind the lead head around the loop, in [-EDGE, 1-EDGE):
        # slightly negative just ahead of the head, for the soft leading edge.
        q, r, g = self._q, self._r, self._g
        np.subtract(self.phase, self.u, out=q)
        q *= sgn
        q += self.EDGE
        np.floor(q, out=g)
        q -= g
        q -= self.EDGE

        # r = the same distance per comet, in units of the comet spacing, [0, 1).
        np.multiply(q, count, out=r)
        np.floor(r, out=g)
        r -= g
        # Tail: 1 at the head, 0 at `tail_length` of the spacing behind it.
        np.multiply(r, -1.0 / max(float(p.tail_length), 1e-3), out=g)
        g += 1.0
        # Leading edge: ramps up over EDGE just ahead of the head (r near 1).
        r -= 1.0
        r *= 1.0 / (self.EDGE * count)
        r += 1.0
        np.maximum(g, r, out=g)
        clip01(g, out=g)
        base = float(p.base_brightness)
        g *= base + (1.0 - base) * min(1.0, float(ctx.audio.low))

        # Hue: the wheel turns with the head and stretches along the tail.
        np.multiply(q, -sgn * float(p.rainbow_stretch) * 1024.0, out=r)
        r += (self.phase + self.hue + 8.0) * 1024.0
        np.copyto(self._idx, r, casting="unsafe")
        np.take(self.wheel, self._idx, axis=0, out=self.out, mode="wrap")
        self.out *= g[:, None]
        return self.out
