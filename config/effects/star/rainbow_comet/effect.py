class RainbowComet(Effect):
    """Rainbow comets on one path through the whole star: out along a leg to
    its tip, back to the hub, then out along the next leg. Hue runs along each
    tail, bass lifts the brightness and every beat nudges the rainbow along
    the colour wheel."""

    def init(self, ctx):
        n = ctx.n
        k = ctx.rig.n_legs
        leg, c = ctx.frames.leg, ctx.frames.chain_index
        # Every LED lies on the path twice: on the way out and on the way back.
        self.u_out = ((leg + 0.5 * c) / k).astype(np.float32)
        self.u_in = ((leg + 1.0 - 0.5 * c) / k).astype(np.float32)
        self.edge = 3.0 / n     # soft leading edge of a head: ~6 LEDs of path
        # The HSV wheel as a table: it is cyclic, so mode="wrap" is seamless.
        self.wheel = hsv_to_rgb(np.arange(1024, dtype=np.float32) / 1024.0, 1.0, 1.0)
        self.phase = 0.0
        self.hue = 0.0          # eased hue offset
        self.hue_to = 0.0       # where the beats have pushed it
        self._q = np.empty(n, dtype=np.float32)
        self._r = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def _behind(self, u, sgn, out, tmp):
        """Distance behind the lead head along the path, in [-edge, 1 - edge):
        slightly negative just ahead of the head, for the soft leading edge."""
        np.subtract(self.phase, u, out=out)
        out *= sgn
        out += self.edge
        np.floor(out, out=tmp)
        out -= tmp
        out -= self.edge

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        sgn = 1.0 if p.direction == "Clockwise" else -1.0
        count = int(p.comets)
        self.phase = (self.phase + dt * float(p.speed) * sgn) % 1.0

        # Beats push a hue target; the hue eases after it instead of jumping.
        if ctx.audio.beat > 0:
            self.hue_to += float(p.beat_hue_shift)
        self.hue += (self.hue_to - self.hue) * (1.0 - float(np.exp(-dt / 0.4)))
        if self.hue > 1.0:
            self.hue -= 1.0
            self.hue_to -= 1.0

        # q for both passes over each LED; the pass nearer behind a head wins.
        q, r, g = self._q, self._r, self._g
        self._behind(self.u_out, sgn, q, g)
        self._behind(self.u_in, sgn, r, g)
        np.minimum(q, r, out=q)

        # r = the same distance per comet, in units of the comet spacing, [0, 1).
        np.multiply(q, count, out=r)
        np.floor(r, out=g)
        r -= g
        # Tail: 1 at the head, 0 at `tail_length` of the spacing behind it.
        np.multiply(r, -1.0 / max(float(p.tail_length), 1e-3), out=g)
        g += 1.0
        # Leading edge: ramps up over `edge` just ahead of the head (r near 1).
        r -= 1.0
        r *= 1.0 / (self.edge * count)
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
