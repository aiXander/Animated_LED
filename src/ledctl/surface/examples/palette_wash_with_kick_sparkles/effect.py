class PaletteWashWithKickSparkles(Effect):
    """Background: a palette wash circling the rig along u_loop, breathing with
    the bass. Foreground: sparkles scattered on every kick, fading out.
    The canonical 'X plus Y' two-component pattern."""

    def init(self, ctx):
        self.u = ctx.frames.u_loop
        self._pal = None
        self.phase = 0.0
        self.spark = np.zeros(ctx.n, dtype=np.float32)   # sparkle level per LED
        self._f = np.empty(ctx.n, dtype=np.float32)
        self._idx = np.empty(ctx.n, dtype=np.int32)
        self._s3 = np.empty((ctx.n, 3), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # Palette there-and-back, so the wrap point has no seam.
            self.lut = np.concatenate([lut, lut[::-1]])
            self._pal = p.palette

        # Wash: whole palette cycles around the loop; mode="wrap" does the modulo.
        self.phase = (self.phase + dt * float(p.wash_speed)) % 1.0
        size = self.lut.shape[0]
        np.multiply(self.u, int(p.wash_repeats) * size, out=self._f)
        self._f += (1.0 - self.phase) * size
        np.copyto(self._idx, self._f, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")
        # Bass pump: the wash sits below full and the bass lifts it back up.
        self.out *= 1.0 - float(p.bass_pump) * (1.0 - min(1.0, float(ctx.audio.low)))

        # Sparkles: exponential fade, a fresh handful on each kick.
        self.spark *= float(np.exp(-dt / max(float(p.sparkle_decay), 1e-3)))
        beat = float(ctx.audio.beat)
        count = int(p.sparkle_count)
        if beat > 0.0 and count > 0:
            self.spark[rng.integers(0, ctx.n, size=count)] = beat
        np.multiply(self.spark[:, None], hex_to_rgb(p.sparkle_color), out=self._s3)
        self.out += self._s3
        return clip01(self.out, out=self.out)
