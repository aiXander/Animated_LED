class MultiLayerOrganic(Effect):
    """Three interfering wave layers (a slow swell and cross-ripples, both a
    2-D field sampled along the legs, and rings from the hub) coloured through
    a palette. Bass lifts the brightness and every beat sends a soft ring out
    from the hub."""

    RING_SPEED = 0.3        # radius per second
    RING_WIDTH = 0.06
    RING_FADE = 1.5         # seconds for the ring to fade to about a third

    def init(self, ctx):
        n = ctx.n
        x, y = ctx.frames.signed_x, ctx.frames.signed_y
        self.d = ctx.frames.radius                  # 0 at the hub -> 1 at the tips
        # Spatial part of every wave term, precomputed once.
        self.x2 = (2.0 * x).astype(np.float32)
        self.y2 = (2.0 * y).astype(np.float32)
        self.p5 = (5.0 * (x + y)).astype(np.float32)
        self.m5 = (5.0 * (x - y)).astype(np.float32)
        self.d8 = (8.0 * self.d).astype(np.float32)
        self._pal = None
        self.slow = 0.0
        self.fast = 0.0         # only ever used at whole multiples, so it can wrap
        self.ring_pos = 2.0     # the beat ring; starts beyond the tips
        self.ring_amp = 0.0
        self._a = np.empty(n, dtype=np.float32)
        self._b = np.empty(n, dtype=np.float32)
        self._c = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)
        self._s3 = np.empty((n, 3), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            self.lut = named_palette(p.palette)
            # The ring glows in the palette's brightest colour.
            self.tint = self.lut[int(np.argmax(self.lut.sum(axis=1)))]
            self._pal = p.palette
        flow = float(p.flow_speed)
        self.slow = (self.slow + dt * flow * 0.2) % TAU
        self.fast = (self.fast + dt * flow * 1.5) % TAU

        a, b, c = self._a, self._b, self._c
        # Layer 1: slow swell.
        np.add(self.x2, self.slow, out=a)
        np.sin(a, out=a)
        np.subtract(self.y2, self.slow, out=b)
        np.cos(b, out=b)
        a *= b
        # Layer 2: fast cross-ripples.
        np.add(self.p5, self.fast, out=b)
        np.sin(b, out=b)
        np.subtract(self.m5, self.fast, out=c)
        np.sin(c, out=c)
        b *= c
        # Layer 3: rings travelling out from the hub.
        np.subtract(self.d8, 2.0 * self.fast, out=c)
        np.sin(c, out=c)
        b += c
        b *= 0.3 * float(p.detail)
        a *= 0.4
        a += b

        # Field (-1..1) -> palette index, stretched by `contrast` and clipped.
        a *= float(p.contrast) * 0.5 * (LUT_SIZE - 1)
        a += 0.5 * (LUT_SIZE - 1)
        np.clip(a, 0.0, LUT_SIZE - 1, out=a)
        np.copyto(self._idx, a, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="clip")
        self.out *= float(p.base_intensity) + float(p.bass_boost) * float(ctx.audio.low)

        # Beat ring: restarts at the hub on each kick, fades as it travels.
        beat = float(ctx.audio.beat)
        if beat > 0.0:
            self.ring_pos = 0.0
            self.ring_amp = beat
        self.ring_pos += dt * self.RING_SPEED
        self.ring_amp *= float(np.exp(-dt / self.RING_FADE))
        glow = self.ring_amp * float(p.beat_ripple) * min(1.0, self.ring_pos / 0.05)
        if glow > 0.004:
            np.subtract(self.d, self.ring_pos, out=c)
            gauss(c, self.RING_WIDTH, out=c)
            c *= glow
            np.multiply(c[:, None], self.tint, out=self._s3)
            self.out += self._s3
        return clip01(self.out, out=self.out)
