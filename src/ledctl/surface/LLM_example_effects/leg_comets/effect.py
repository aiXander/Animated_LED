class LegComets(Effect):
    """Comets that each own one leg and a distance along it. A comet leaves the
    hub, runs to the tip with a fading tail, then hops to another leg and
    starts again. A beat gives them all a short push."""

    COMETS = 6
    EDGE = 0.01         # soft leading edge of a head (fraction of the radius)

    def init(self, ctx):
        n_c = self.COMETS
        self.k = ctx.rig.n_legs
        self.r = ctx.frames.radius[None, :]                         # (1, N)
        self.leg = ctx.frames.leg[None, :]                          # (1, N)
        self.c_leg = np.arange(n_c).reshape(n_c, 1) % self.k        # the leg each comet is on
        self.c_pos = (1.3 * rng.random((n_c, 1))).astype(np.float32)   # its head's distance out
        self.c_col = np.zeros((n_c, 3), dtype=np.float32)
        self.env = 0.0
        self._pal = None
        self._f = np.empty((n_c, ctx.n), dtype=np.float32)
        self._g = np.empty((n_c, ctx.n), dtype=np.float32)
        self._on = np.empty((n_c, ctx.n), dtype=bool)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            self.c_col[:] = palette_lerp(named_palette(p.palette), rng.random(self.COMETS))
            self._pal = p.palette
        self.env = max(self.env * float(np.exp(-dt / 0.5)), float(ctx.audio.beat))
        tail = float(p.tail)
        self.c_pos += dt * float(p.speed) * (1.0 + float(p.beat_push) * self.env)

        # A comet whose tail has left the tip hops to another leg, back at the hub.
        done = self.c_pos[:, 0] > 1.0 + tail
        if done.any():
            hop = rng.integers(1, max(self.k, 2), size=(int(done.sum()), 1))
            self.c_leg[done] = (self.c_leg[done] + hop) % self.k
            self.c_pos[done] = 0.0

        # (COMETS, N): how far each LED sits behind each head. The tail fades
        # over `tail`; ahead of the head a short soft edge.
        f, g = self._f, self._g
        np.subtract(self.c_pos, self.r, out=f)
        np.multiply(f, 1.0 / self.EDGE, out=g)
        g += 1.0
        f *= -1.0 / tail
        f += 1.0
        np.minimum(f, g, out=f)
        np.clip(f, 0.0, 1.0, out=f)
        f *= f
        # The leg mask: a comet lights only LEDs on its own leg.
        np.equal(self.leg, self.c_leg, out=self._on)
        f *= self._on
        np.matmul(f.T, self.c_col, out=self.out)    # sum the comets' colours per LED
        np.minimum(self.out, 1.0, out=self.out)
        return self.out
