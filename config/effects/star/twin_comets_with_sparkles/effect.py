class TwinCometsWithSparkles(Effect):
    """Two comets, each in its own colour, run out from the hub along a leg and
    move on to the next leg when they reach the tip, shedding a trail of fading
    sparkles. An audio band lifts the heads and every kick throws a burst of
    sparkles around them."""

    BURST_REACH = 0.12      # how far from a head a kick scatters sparkles

    def init(self, ctx):
        n = ctx.n
        self.k = ctx.rig.n_legs
        self.c = ctx.frames.chain_index[None, :]           # (1, N) hub -> tip of own leg
        self.leg = ctx.frames.leg[None, :]                 # (1, N)
        self.head = 0.0                                    # the leader's distance out
        self.lap = 0                                       # tips the leader has reached
        self.pos = np.zeros((2, 1), dtype=np.float32)      # each comet's distance out
        self.on_leg = np.zeros((2, 1), dtype=np.int32)     # the leg each comet is on
        self.cols = np.zeros((2, 3), dtype=np.float32)
        self.spark = np.zeros(n, dtype=np.float32)         # sparkle level per LED
        self.col = np.zeros((n, 3), dtype=np.float32)      # colour of each LED's sparkle
        self._d = np.empty((2, n), dtype=np.float32)
        self._g = np.empty((2, n), dtype=np.float32)
        self._r = np.empty((2, n), dtype=np.float32)
        self._on = np.empty((2, n), dtype=bool)
        self._m = np.empty((2, n), dtype=bool)
        self._m2 = np.empty((2, n), dtype=bool)
        self._s3 = np.empty((n, 3), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        self.cols[0] = hex_to_rgb(p.leader_color)
        self.cols[1] = hex_to_rgb(p.follower_color)
        step = float(p.speed) * dt
        self.head += step
        if self.head >= 1.0:            # the leader reached a tip: on to the next leg
            self.head -= 1.0
            self.lap = (self.lap + 1) % self.k
        # The follower runs the same course `lead_offset` behind, half the legs
        # further round, so it is still finishing a leg when the leader starts one.
        f = self.head - float(p.lead_offset)
        behind = f < 0.0
        self.pos[0] = self.head
        self.pos[1] = f + 1.0 if behind else f
        self.on_leg[0] = self.lap
        self.on_leg[1] = (self.lap + self.k // 2 - (1 if behind else 0)) % self.k
        on = self._on
        np.equal(self.leg, self.on_leg, out=on)            # (2, N) each comet's own leg

        # d = how far each LED sits ahead of each head (negative = passed).
        d, g, r, m, m2 = self._d, self._g, self._r, self._m, self._m2
        np.subtract(self.c, self.pos, out=d)

        # Sparkles fade, then each head sheds new ones on the stretch it just swept.
        self.spark *= float(np.exp(-dt / max(float(p.sparkle_decay), 1e-3)))
        dens = float(p.sparkle_density)
        rng.random(out=r, dtype=np.float32)
        np.less(d, 0.0, out=m)
        np.greater_equal(d, -step, out=m2)
        m &= m2
        np.less(r, dens, out=m2)
        m &= m2
        m &= on
        for i in (0, 1):                # the two comets, never a loop over LEDs
            np.putmask(self.spark, m[i], 1.0)
            np.copyto(self.col, self.cols[i], where=m[i][:, None])

        beat = float(ctx.audio.beat)
        if beat > 0.0 and dens > 0.0:
            # Kick: a burst around each head, densest at the head itself.
            np.abs(d, out=g)
            g *= -1.0 / self.BURST_REACH
            g += 1.0
            g *= min(0.9, 2.0 * dens)
            g += r
            np.greater(g, 1.0, out=m)
            m &= on
            for i in (0, 1):
                np.putmask(self.spark, m[i], beat)
                np.copyto(self.col, self.cols[i], where=m[i][:, None])

        # Heads: gaussian glow lifted by the chosen band, plus the sparkles.
        amp = 0.6 + 0.4 * min(1.0, float(ctx.audio.bands[p.audio_band]))
        gauss(d, float(p.head_size), out=g)
        g *= on
        g *= amp
        np.matmul(g.T, self.cols, out=self.out)
        np.multiply(self.col, self.spark[:, None], out=self._s3)
        self.out += self._s3
        return clip01(self.out, out=self.out)
