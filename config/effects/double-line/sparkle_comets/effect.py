class SparkleComets(Effect):
    """Comets shoot outward from the rig centre along all four strips, their
    trails breaking up into twinkling glitter. Bass pulses the brightness and
    every beat flashes white glints across the whole rig."""

    EDGE = 0.008        # soft leading edge of a head (fraction of a half-rig)

    def init(self, ctx):
        n = ctx.n
        self.d = ctx.frames.axial_dist             # 0 at the centre -> 1 at the ends
        self.pos = (0.5 * ctx.frames.axial_signed + 0.5).astype(np.float32)
        self._pal = None
        self.col = np.zeros((n, 3), dtype=np.float32)   # static colour per LED
        # Per-LED twinkle: random phase, and a rate in 16ths so the twinkle
        # clock can wrap at 16*TAU without any LED jumping.
        self.tw_ph = rng.uniform(0.0, TAU, n).astype(np.float32)
        self.tw_rate = (rng.integers(10, 23, n) / 16.0).astype(np.float32)
        self.tw_t = 0.0
        self.phase = 0.0
        self.env = 0.0
        self._r = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._tw = np.empty(n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            # Colour depends on position only: rebuild it when the palette changes.
            self.col[:] = palette_lerp(named_palette(p.palette), self.pos)
            self._pal = p.palette
        self.env = max(self.env * float(np.exp(-dt / 0.18)), float(ctx.audio.beat))
        self.phase = (self.phase + 0.5 * dt * float(p.speed)) % 1.0

        # r = distance behind the nearest head, in units of the comet spacing.
        count = int(p.comets)
        r, g, tw = self._r, self._g, self._tw
        np.multiply(self.d, -count, out=r)
        r += self.phase * count
        np.floor(r, out=g)
        r -= g
        # Exponential trail (longer `trail_length` = slower decay), rescaled to
        # reach exactly 0 where the next head begins.
        k = 1.0 / (float(p.trail_length) * 0.1 + 0.01)
        tail_end = float(np.exp(-k))
        np.multiply(r, -k, out=g)
        np.exp(g, out=g)
        g -= tail_end
        g *= 1.0 / (1.0 - tail_end)
        r -= 1.0
        r *= 1.0 / (self.EDGE * count)
        r += 1.0
        np.maximum(g, r, out=g)

        # Glitter: each LED glints on its own clock (half-rectified sine, squared).
        self.tw_t = (self.tw_t + dt * float(p.twinkle_speed)) % (16.0 * TAU)
        np.multiply(self.tw_rate, self.tw_t, out=tw)
        tw += self.tw_ph
        np.sin(tw, out=tw)
        np.maximum(tw, 0.0, out=tw)
        tw *= tw
        s = float(p.sparkle)
        np.multiply(tw, 2.5 * s, out=r)
        r += 1.0 - s
        g *= r
        g *= 1.0 - float(p.bass_pulse) * (1.0 - min(1.0, float(ctx.audio.low)))
        np.multiply(self.col, g[:, None], out=self.out)

        # Beat: the glinting LEDs flash white everywhere, then decay.
        burst = float(p.beat_burst) * self.env
        if burst > 0.004:
            tw *= tw
            tw *= burst
            self.out += tw[:, None]
        return clip01(self.out, out=self.out)
