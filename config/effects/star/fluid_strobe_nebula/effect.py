class FluidStrobeNebula(Effect):
    """A liquid plasma field that the legs cut through: each leg shows the slice
    of the field lying under it, so the colours flow differently along every
    leg. Bass swells the glow and a beat adds a soft white swell that fades."""

    def init(self, ctx):
        n = ctx.n
        # A 2-D field is only SAMPLED along the legs, through signed_x / signed_y.
        self.x = ctx.frames.signed_x
        self.y = ctx.frames.signed_y
        # HSV wheel at 0.9 saturation as a table; cyclic, so mode="wrap" is seamless.
        self.wheel = hsv_to_rgb(np.arange(1024, dtype=np.float32) / 1024.0, 0.9, 1.0)
        self.pa = self.pb = 0.0     # one wrapped clock per wave term
        self.hue = 0.0
        self.env = 0.0              # beat envelope
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
        self.env = max(self.env * float(np.exp(-dt / float(p.strobe_decay))),
                       float(ctx.audio.beat))

        # Plasma: a = sin(2 * (sin(4x + pa) + cos(5y + pb)) + pa), in [-1, 1].
        a, b = self._a, self._b
        scale = float(p.scale)
        np.multiply(self.x, 4.0 * scale, out=a)
        a += self.pa
        np.sin(a, out=a)
        np.multiply(self.y, 5.0 * scale, out=b)
        b += self.pb
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
        self.out += float(p.strobe) * self.env      # white, decaying
        return clip01(self.out, out=self.out)
