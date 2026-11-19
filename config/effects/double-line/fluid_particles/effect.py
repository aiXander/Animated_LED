class FluidParticles(Effect):
    """Soft colour blobs drifting around the rig in both directions, passing
    through each other. Bass inflates them and every beat kicks each one with a
    random shove that then settles back into its drift."""

    MAX = 24

    def init(self, ctx):
        k = self.MAX
        self.u = ctx.frames.u_loop[None, :]            # (1, N) position around the rig
        self.pu = rng.uniform(0.0, 1.0, k).astype(np.float32)
        # Each particle's own cruising velocity (loops/s), alternating direction.
        drift = rng.uniform(0.02, 0.07, k) * np.where(np.arange(k) % 2 == 0, 1.0, -1.0)
        self.drift = drift.astype(np.float32)
        self.v = self.drift.copy()
        self._pal = None
        self.cols = np.zeros((k, 3), dtype=np.float32)
        self._kick = np.empty(k, dtype=np.float32)
        self._f = np.empty((k, ctx.n), dtype=np.float32)   # (P, N) falloff
        self._w = np.empty((k, ctx.n), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        k = int(p.count)
        if p.palette != self._pal:
            # Colours spread over the palette, each lifted to full brightness so
            # no particle is invisible on a dark palette stop. Shuffled so
            # neighbouring particles differ.
            t = (0.08 + 0.92 * ((np.arange(self.MAX) * 7) % self.MAX) / (self.MAX - 1))
            c = palette_lerp(named_palette(p.palette), t.astype(np.float32))
            self.cols[:] = c / np.maximum(c.max(axis=1, keepdims=True), 1e-3)
            self._pal = p.palette

        # Velocity relaxes toward the particle's own drift, so a kick dies away
        # but the flow never stalls.
        v, pu = self.v[:k], self.pu[:k]
        v += (self.drift[:k] - v) * min(1.0, float(p.damping) * dt)
        beat = float(ctx.audio.beat)
        if beat > 0.0:
            kick = self._kick[:k]
            rng.random(out=self._kick, dtype=np.float32)
            kick -= 0.5
            kick *= 0.5 * float(p.kick_impulse) * beat
            v += kick
        pu += v * (dt * float(p.speed))
        pu -= np.floor(pu)

        # (P, N) falloff: shortest distance around the loop -> soft blob.
        radius = 0.25 * float(p.particle_size) * (
            1.0 + float(ctx.audio.low) * float(p.audio_size_boost))
        f, w = self._f[:k], self._w[:k]
        np.subtract(self.u, pu[:, None], out=f)
        np.rint(f, out=w)
        f -= w
        np.abs(f, out=f)
        f *= -1.0 / max(radius, 1e-4)
        f += 1.0
        np.maximum(f, 0.0, out=f)
        f *= f
        # Sum every particle's colour into the LEDs in one matrix product.
        np.matmul(f.T, self.cols[:k], out=self.out)
        self.out *= float(p.intensity)
        return clip01(self.out, out=self.out)
