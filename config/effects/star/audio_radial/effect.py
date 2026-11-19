class AudioRadial(Effect):
    """Palette rings travelling out from the hub, the same on every leg. One
    audio band drives the brightness (optionally reaching outward from the hub)
    and every beat pushes the rings along."""

    def init(self, ctx):
        self.r = ctx.frames.radius                # 0 at the hub, 1 at the tips
        self._pal = None
        self.phase = 0.0
        self.env = 0.0
        self._f = np.empty(ctx.n, dtype=np.float32)
        self._idx = np.empty(ctx.n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # Palette there-and-back, so the wrap point has no seam.
            self.lut = np.concatenate([lut, lut[::-1]])
            self._pal = p.palette
        dt = float(ctx.dt)
        self.env = max(self.env * float(np.exp(-dt / 0.5)), float(ctx.audio.beat))
        speed = float(p.ring_speed) * (1.0 + float(p.beat_surge) * self.env)
        self.phase = (self.phase + dt * speed) % 1.0

        # Ring coordinate -> LUT index; mode="wrap" does the modulo.
        size = self.lut.shape[0]
        np.multiply(self.r, float(p.ring_count) * size, out=self._f)
        self._f += (1.0 - self.phase) * size
        np.copyto(self._idx, self._f, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")

        # Brightness: the band level, reaching outward from the hub by `spread`.
        level = float(ctx.audio.bands[p.audio_band])
        spread = float(p.audio_spread)
        np.multiply(self.r, -spread, out=self._f)
        self._f += level * (1.0 + spread)
        clip01(self._f, out=self._f)
        floor = float(p.brightness_floor)
        self._f *= 1.0 - floor
        self._f += floor
        self.out *= self._f[:, None]
        return self.out
