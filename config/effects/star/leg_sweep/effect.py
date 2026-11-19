class LegSweep(Effect):
    """A slow beam turning round the hub like a lighthouse. A leg glows as the
    beam passes its direction and dims behind it; `twist` bends the beam into a
    spiral, so the light also runs out along each leg. A beat nudges it on."""

    def init(self, ctx):
        n = ctx.n
        self.ang = ctx.frames.angle         # each LED's leg direction, a turn in [0, 1)
        self.r = ctx.frames.radius
        # Colour position: every leg its own stretch of the palette.
        self.c0 = (0.5 * self.ang + 0.15 * self.r).astype(np.float32)
        self.phase = 0.0                    # where the beam points, a turn in [0, 1)
        self.drift = 0.0                    # colour clock
        self.env = 0.0                      # beat envelope
        self._pal = None
        self._d = np.empty(n, dtype=np.float32)
        self._g = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if p.palette != self._pal:
            lut = named_palette(p.palette)
            # The beam sets the brightness, so lift the palette's dark tones.
            lut = lut / np.maximum(lut.max(axis=1, keepdims=True), 0.3)
            self.lut = np.concatenate([lut, lut[::-1]])     # there-and-back: no seam
            self._pal = p.palette
        self.env = max(self.env * float(np.exp(-dt / 0.5)), float(ctx.audio.beat))
        turn = dt * float(p.speed) / 60.0
        self.phase = (self.phase + turn * (1.0 + float(p.beat_push) * self.env)) % 1.0
        self.drift = (self.drift + 0.3 * turn) % 1.0

        # d = angular distance from the beam to each LED's leg, wrapped to +-0.5
        # turn. `twist` delays the beam with radius: it reaches the tips later.
        d, g = self._d, self._g
        np.multiply(self.r, float(p.twist), out=d)
        d += self.ang
        d -= self.phase
        np.rint(d, out=g)
        d -= g
        # Brightness: a cosine bump `width` turns to each side, over a low floor.
        d *= 1.0 / float(p.width)
        np.clip(d, -1.0, 1.0, out=d)
        d *= PI
        np.cos(d, out=g)
        floor = float(p.floor)
        g *= 0.5 * max(0.0, float(p.peak) - floor)
        g += 0.5 * (float(p.peak) + floor)

        size = self.lut.shape[0]
        np.add(self.c0, self.drift, out=d)
        d *= size
        np.copyto(self._idx, d, casting="unsafe")
        np.take(self.lut, self._idx, axis=0, out=self.out, mode="wrap")
        self.out *= g[:, None]
        return self.out
