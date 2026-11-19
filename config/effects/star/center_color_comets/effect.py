class CenterComets(Effect):
    """Comets leave the hub one leg after another and run out to the tips (or
    back in), trailing palette colour. Bass pulses the brightness and every
    beat pushes the comets on."""

    EDGE = 0.008        # soft leading edge of a head (fraction of the radius)

    def init(self, ctx):
        n = ctx.n
        r = ctx.frames.radius
        # Travel coordinate: distance out, shifted per leg so the legs fire in turn.
        self.d = (r + ctx.frames.leg_phase).astype(np.float32)
        # Colour position: every leg its own stretch of the palette.
        self.c0 = (ctx.frames.leg_phase + 0.5 * r).astype(np.float32)
        self._pal = None
        self.phase = 0.0        # comet travel
        self.drift = 0.0        # slow colour drift, tied to the travel
        self.env = 0.0          # beat envelope
        self._r = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # The comets set the brightness, so lift the palette's dark tones
            # to full colour (a black entry stays black).
            lut = lut / np.maximum(lut.max(axis=1, keepdims=True), 0.3)
            # Palette there-and-back, so the wrap point has no seam.
            self.lut = np.concatenate([lut, lut[::-1]])
            self._pal = p.palette
        self.env = max(self.env * float(np.exp(-dt / 0.5)), float(ctx.audio.beat))
        adv = dt * float(p.speed) * (1.0 + float(p.beat_surge) * self.env)
        self.phase = (self.phase + 0.5 * adv) % 1.0
        self.drift = (self.drift + 0.1 * adv) % 1.0

        # r = distance behind the nearest head, in units of the comet spacing.
        count = int(p.comets)
        sgn = 1.0 if p.direction == "Outward" else -1.0
        r, g = self._r, self._g
        np.multiply(self.d, -sgn * count, out=r)
        r += self.phase * count
        np.floor(r, out=g)
        r -= g

        # Trail: exponential, rescaled so it reaches exactly 0 where the next
        # comet's head begins (no step at the wrap).
        k = 5.0 / max(0.1, float(p.trail_length))
        tail_end = float(np.exp(-k))
        np.multiply(r, -k, out=g)
        np.exp(g, out=g)
        g -= tail_end
        g *= 1.0 / (1.0 - tail_end)
        # Soft leading edge just ahead of the head (r near 1).
        r -= 1.0
        r *= 1.0 / (self.EDGE * count)
        r += 1.0
        np.maximum(g, r, out=g)
        g *= 1.0 - float(p.bass_pulse) * (1.0 - min(1.0, float(ctx.audio.low)))

        # Colour: the palette spread over the legs, drifting as the comets travel.
        size = self.lut.shape[0]
        np.multiply(self.c0, float(p.color_spread) * size, out=r)
        r += self.drift * size
        np.copyto(self._idx, r, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")
        self.out *= g[:, None]
        return self.out
