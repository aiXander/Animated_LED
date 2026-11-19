class PaletteNebula(Effect):
    """A twisted plasma field coloured through a palette that slowly cycles.
    Bass lifts the brightness and every beat fires a white strobe that decays."""

    def init(self, ctx):
        n = ctx.n
        self.x = ctx.frames.signed_x
        self.y = ctx.frames.signed_y
        self.ang = ctx.frames.angle
        self.r2 = (2.0 * ctx.frames.radius).astype(np.float32)
        self._pal = None
        self._twist = None
        self.nx6 = np.empty(n, dtype=np.float32)    # twisted coordinates, pre-scaled
        self.ny8 = np.empty(n, dtype=np.float32)
        # One wrapped clock per wave term. A single clock scaled by 0.5 would
        # jump every time it wrapped.
        self.pa = 0.0
        self.pb = 0.0
        self.drift = 0.0
        self.env = 0.0          # strobe envelope
        self._a = np.empty(n, dtype=np.float32)
        self._b = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # Palette there-and-back, so the wrap point has no seam.
            self.lut = np.concatenate([lut, lut[::-1]])
            self._pal = p.palette
        twist = float(p.twist_amount)
        if twist != self._twist:
            # Only when the knob moves: rotate (x, y) by a per-LED twist angle.
            t = self.ang * twist + self.r2
            c, s = np.cos(t), np.sin(t)
            self.nx6[:] = 6.0 * (self.x * c - self.y * s)
            self.ny8[:] = 8.0 * (self.x * s + self.y * c)
            self._twist = twist

        flow = float(p.flow_speed)
        self.pa = (self.pa + dt * flow) % TAU
        self.pb = (self.pb + dt * flow * 0.5) % TAU
        self.drift = (self.drift + dt * float(p.palette_drift)) % 1.0
        decay = max(float(p.strobe_decay), 1e-3)
        self.env = max(self.env * float(np.exp(-dt / decay)), float(ctx.audio.beat))

        # a = sin(complexity * (sin(nx6 + pa) + cos(ny8 - pb)) + pa), in [-1, 1].
        a, b = self._a, self._b
        np.add(self.nx6, self.pa, out=a)
        np.sin(a, out=a)
        np.subtract(self.ny8, self.pb, out=b)
        np.cos(b, out=b)
        a += b
        a *= float(p.complexity)
        a += self.pa
        np.sin(a, out=a)

        # Pattern 0..1 spans the palette once; the drift slides that window
        # along the there-and-back table (mode="wrap" does the modulo).
        size = self.lut.shape[0]
        a *= 0.25 * size
        a += (0.25 + self.drift) * size
        np.copyto(self._idx, a, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")

        base = float(p.base_level)
        self.out *= base + (1.0 - base) * min(1.0, float(ctx.audio.low))
        flash = float(p.strobe_intensity) * self.env
        if flash > 0.004:
            self.out += flash
            clip01(self.out, out=self.out)
        return self.out
