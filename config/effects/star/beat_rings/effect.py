class BeatRings(Effect):
    """Every beat sends a soft ring out from the hub, at the same distance on
    every leg. Rings widen and fade as they travel. With no beat for a while, a
    quiet ring leaves on its own."""

    RINGS = 8

    def init(self, ctx):
        self.r = ctx.frames.radius[None, :]                             # (1, N)
        self.pos = np.full((self.RINGS, 1), 9.0, dtype=np.float32)      # ring radius; 9 = spent
        self.amp = np.zeros((self.RINGS, 1), dtype=np.float32)
        self.col = np.zeros((self.RINGS, 3), dtype=np.float32)
        self.nxt = 0
        self.idle = 0.0         # seconds since the last ring left
        self.hue = 0.0
        self._f = np.empty((self.RINGS, ctx.n), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        beat = float(ctx.audio.beat)
        self.idle += dt
        if (beat > 0.0 and self.idle > 0.15) or self.idle > float(p.idle_gap):
            i = self.nxt                    # reuse the oldest slot
            self.nxt = (i + 1) % self.RINGS
            self.pos[i] = 0.0
            self.amp[i] = beat if beat > 0.0 else 0.5
            self.hue = (self.hue + float(p.colour_step)) % 1.0
            c = palette_lerp(named_palette(p.palette), self.hue)
            self.col[i] = c / max(float(c.max()), 0.3)     # full colour; the ring sets brightness
            self.idle = 0.0
        self.pos += dt * float(p.speed)
        np.minimum(self.pos, 9.0, out=self.pos)

        # Per ring: fade in as it leaves the hub (no flash), fade out by `reach`.
        gain = float(p.brightness) * self.amp * np.clip(self.pos / 0.04, 0.0, 1.0)
        gain *= np.clip(1.0 - self.pos / float(p.reach), 0.0, 1.0)

        # (RINGS, N): a gaussian round each ring's radius, widening as it travels.
        f = self._f
        np.subtract(self.r, self.pos, out=f)
        f /= float(p.width) * (1.0 + 2.0 * self.pos)
        np.square(f, out=f)
        f *= -0.5
        np.exp(f, out=f)
        f *= gain
        np.matmul(f.T, self.col, out=self.out)      # sum the rings' colours per LED
        np.minimum(self.out, 1.0, out=self.out)
        return self.out
