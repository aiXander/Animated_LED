class TwinCometsWithSparkles(Effect):
    """Two comets sweep along the rig, one per row, each in its own colour and
    shedding a trail of fading sparkles. An audio band lifts the heads and every
    kick throws a burst of sparkles around them."""

    BURST_REACH = 0.12      # how far from a head a kick scatters sparkles (x units)

    def init(self, ctx):
        n = ctx.n
        self.x = ctx.frames.x                          # [0, 1] left -> right
        self.top = ctx.frames.side_top[:, None]        # (N, 1) row masks
        self.bot = ctx.frames.side_bottom[:, None]
        self.bot1 = ctx.frames.side_bottom
        self.head = 0.0
        self.spark = np.zeros(n, dtype=np.float32)     # sparkle level per LED
        self.col = np.zeros((n, 3), dtype=np.float32)  # each LED's row colour
        self._cols = None
        self._d = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._r = np.empty(n, dtype=np.float32)
        self._m = np.empty(n, dtype=bool)
        self._m2 = np.empty(n, dtype=bool)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        cols = (p.leader_color, p.follower_color)
        if cols != self._cols:
            np.multiply(self.top, hex_to_rgb(cols[0]), out=self.col)
            self.col += self.bot * hex_to_rgb(cols[1])
            self._cols = cols
        step = float(p.speed) * dt
        self.head = (self.head + step) % 1.0

        # d = signed distance from each LED to its own row's head, wrapped to
        # +-0.5 (negative = the head has already passed). Bottom head trails by
        # `lead_offset`.
        d, g = self._d, self._g
        np.multiply(self.bot1, float(p.lead_offset), out=d)
        d += self.x
        d -= self.head
        np.rint(d, out=g)
        d -= g

        # Sparkles fade, then each head sheds new ones on the stretch it just swept.
        self.spark *= float(np.exp(-dt / max(float(p.sparkle_decay), 1e-3)))
        dens = float(p.sparkle_density)
        rng.random(out=self._r, dtype=np.float32)
        np.less(d, 0.0, out=self._m)
        np.greater_equal(d, -step, out=self._m2)
        self._m &= self._m2
        np.less(self._r, dens, out=self._m2)
        self._m &= self._m2
        np.putmask(self.spark, self._m, 1.0)

        beat = float(ctx.audio.beat)
        if beat > 0.0 and dens > 0.0:
            # Kick: a burst around each head, densest at the head itself.
            np.abs(d, out=g)
            g *= -1.0 / self.BURST_REACH
            g += 1.0
            g *= min(0.9, 2.0 * dens)
            g += self._r
            np.greater(g, 1.0, out=self._m)
            np.putmask(self.spark, self._m, beat)

        # Heads: gaussian glow lifted by the chosen band, plus the sparkles.
        amp = 0.6 + 0.4 * min(1.0, float(ctx.audio.bands[p.audio_band]))
        gauss(d, float(p.head_size), out=g)
        g *= amp
        g += self.spark
        np.multiply(self.col, g[:, None], out=self.out)
        return clip01(self.out, out=self.out)
