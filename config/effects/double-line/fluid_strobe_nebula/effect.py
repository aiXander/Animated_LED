class FluidStrobeNebula(Effect):
    """Liquid plasma flowing along the rig, the two rows sliding against each
    other. Bass swells the glow and every beat fires a white strobe that decays."""

    def init(self, ctx):
        n = ctx.n
        self.x = ctx.frames.signed_x
        self.y10 = (10.0 * ctx.frames.signed_y).astype(np.float32)
        # HSV wheel at 0.9 saturation as a table; cyclic, so mode="wrap" is seamless.
        self.wheel = hsv_to_rgb(np.arange(1024, dtype=np.float32) / 1024.0, 0.9, 1.0)
        # One wrapped clock per wave term. A single clock scaled by 0.7 would
        # jump every time it wrapped.
        self.pa = 0.0
        self.pb = 0.0
        self.hue = 0.0
        self.env = 0.0          # strobe envelope
        self._a = np.empty(n, dtype=np.float32)
        self._b = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        flow = float(p.flow_speed)
        self.pa = (self.pa + dt * flow) % TAU
        self.pb = (self.pb + dt * flow * 0.7) % TAU
        self.hue = (self.hue + dt * float(p.hue_drift)) % 1.0
        decay = max(float(p.strobe_decay), 1e-3)
        self.env = max(self.env * float(np.exp(-dt / decay)), float(ctx.audio.beat))

        # Plasma: a = sin(2 * (sin(5x + pa) + cos(10y + pb)) + pa), in [-1, 1].
        a, b = self._a, self._b
        np.multiply(self.x, 5.0 * float(p.scale), out=a)
        a += self.pa
        np.sin(a, out=a)
        np.add(self.y10, self.pb, out=b)
        np.cos(b, out=b)
        a += b
        a *= 2.0
        a += self.pa
        np.sin(a, out=a)

        # Hue = base + range * pattern + drift, through the wheel table.
        span = 0.5 * float(p.hue_range)
        np.multiply(a, span * 1024.0, out=b)
        b += (float(p.hue) + span + self.hue + 2.0) * 1024.0
        np.copyto(self._idx, b, casting="unsafe")
        np.take(self.wheel, self._idx, axis=0, out=self.out, mode="wrap")

        # Brightness follows the pattern; bass fills in `bass_surge` of it.
        surge = float(p.bass_surge)
        a += 1.0
        a *= 0.5 * (1.0 - surge + surge * min(1.0, float(ctx.audio.low)))
        self.out *= a[:, None]

        flash = float(p.strobe) * self.env
        if flash > 0.004:
            self.out += flash
            clip01(self.out, out=self.out)
        return self.out
