class FireWash(Effect):
    """Full-rig fire: per-LED flicker from two detuned random-phase sine layers
    (precomputed in init — no per-pixel loops, no per-frame allocation), coloured
    through a palette LUT. Bass fans the flames CONTINUOUSLY — this is an
    amplitude-following wash, so no beat logic. The noise-field archetype."""

    def init(self, ctx):
        n = ctx.n
        # Random per-LED phases/rates precomputed ONCE — rng + loops are fine here.
        self.ph1 = rng.uniform(0.0, TAU, n).astype(np.float32)
        self.ph2 = rng.uniform(0.0, TAU, n).astype(np.float32)
        self.rate1 = rng.uniform(1.5, 3.0, n).astype(np.float32)
        self.rate2 = rng.uniform(5.0, 9.0, n).astype(np.float32)
        # Bottom row burns hotter; the top row is cooler, like rising embers.
        self.row_bias = (0.75 + 0.25 * ctx.frames.side_bottom).astype(np.float32)
        self.lut = named_palette("fire")
        self._palette_name = "fire"
        self._heat = np.empty(n, dtype=np.float32)
        self._tmp = np.empty(n, dtype=np.float32)
        self._idx = np.empty(n, dtype=np.int32)
        self.t_acc = 0.0

    def render(self, ctx):
        p = ctx.params
        if self._palette_name != p.palette:
            self.lut = named_palette(p.palette)
            self._palette_name = p.palette
        self.t_acc += float(ctx.dt) * float(p.flicker_speed)

        # Two detuned sine layers per LED → organic flicker, fully vectorised.
        np.multiply(self.rate1, self.t_acc, out=self._heat)
        self._heat += self.ph1
        np.sin(self._heat, out=self._heat)
        np.multiply(self.rate2, self.t_acc, out=self._tmp)
        self._tmp += self.ph2
        np.sin(self._tmp, out=self._tmp)
        self._heat *= 0.6
        self._tmp *= 0.4
        self._heat += self._tmp
        self._heat *= 0.5
        self._heat += 0.5                      # → [0, 1]
        self._heat *= self.row_bias

        # Continuous bass drive; still glows when the music stops.
        amp = float(p.base_glow) + (1.0 - float(p.base_glow)) * float(ctx.audio.low)
        self._heat *= amp

        clip01(self._heat, out=self._heat)     # bands can overshoot 1.0 on transients
        self._heat *= LUT_SIZE - 1
        np.copyto(self._idx, self._heat.astype(np.int32))
        np.take(self.lut, self._idx, axis=0, out=self.out)
        return self.out
