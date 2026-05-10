class PaletteWashWithKickSparkles(Effect):
    """Background: palette wash scrolling along u_loop. Foreground: bright
    sparkles deposited on each kick (audio.beat > 0), fading exponentially.
    Demonstrates the canonical 'X plus Y' multi-component pattern."""

    def init(self, ctx):
        self.u = ctx.frames.u_loop
        self.lut = named_palette("warm")
        self._wash_idx = np.empty(ctx.n, dtype=np.int32)
        self._wash_pos = np.empty(ctx.n, dtype=np.float32)
        # Sparkle state.
        self.sparkle_age = np.full(ctx.n, np.inf, dtype=np.float32)
        self.sparkle_rgb = np.zeros((ctx.n, 3), dtype=np.float32)
        self._fade = np.empty(ctx.n, dtype=np.float32)
        self._wash = np.empty((ctx.n, 3), dtype=np.float32)
        self._spark = np.empty((ctx.n, 3), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        if getattr(self, "_palette_name", None) != p.palette:
            self.lut = named_palette(p.palette)
            self._palette_name = p.palette

        # Background wash along u_loop.
        np.subtract(self.u, ctx.t * float(p.wash_speed), out=self._wash_pos)
        np.mod(self._wash_pos, 1.0, out=self._wash_pos)
        np.multiply(self._wash_pos, LUT_SIZE - 1, out=self._wash_pos)
        np.copyto(self._wash_idx, self._wash_pos.astype(np.int32))
        np.take(self.lut, self._wash_idx, axis=0, out=self._wash)

        # Sparkle decay.
        np.add(self.sparkle_age, dt, out=self.sparkle_age)
        np.divide(self.sparkle_age, max(float(p.sparkle_decay), 1e-3), out=self._fade)
        np.exp(np.negative(self._fade, out=self._fade), out=self._fade)
        np.multiply(self.sparkle_rgb, self._fade[:, None], out=self._spark)

        # Drop a fresh batch on each beat. Use a fixed-density sample so the
        # peak sparkle count is bounded.
        # ctx.audio.beat is a float in [0, 1] (0 most frames; on a kick it
        # equals master audio_reactivity clipped to 1). Use it as both the
        # rising-edge trigger AND as the deposit intensity multiplier.
        beat_amp = float(ctx.audio.beat)
        if beat_amp > 0.0:
            n = int(ctx.params.sparkle_count)
            if n > 0:
                idx = rng.integers(0, ctx.n, size=n)
                col = hex_to_rgb(p.sparkle_color) * beat_amp
                self.sparkle_age[idx] = 0.0
                self.sparkle_rgb[idx] = col
                # Re-emit the just-fresh sparkles into _spark too so they
                # appear THIS frame (otherwise there's a one-frame delay).
                self._spark[idx] = col

        # Compose: wash + sparkles (additive), then clip.
        np.add(self._wash, self._spark, out=self.out)
        np.clip(self.out, 0.0, 1.0, out=self.out)
        return self.out
