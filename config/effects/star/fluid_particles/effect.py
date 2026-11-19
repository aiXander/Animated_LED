class FluidParticles(Effect):
    """Soft colour blobs drifting in and out along the legs. A blob that
    reaches the hub flows through it onto another leg; at a tip it turns back.
    Bass inflates the blobs and every beat gives each one a shove that then
    settles back into its drift."""

    MAX = 24

    def init(self, ctx):
        m = self.MAX
        self.k = ctx.rig.n_legs
        self.r = ctx.frames.radius[None, :]                # (1, N)
        self.leg = ctx.frames.leg[None, :]                 # (1, N)
        self.p_leg = (np.arange(m) % self.k).reshape(m, 1)          # the leg each blob is on
        self.p_pos = rng.uniform(0.05, 0.95, (m, 1)).astype(np.float32)  # its distance out
        # Each blob's own cruising velocity (radius/s), alternating direction.
        drift = rng.uniform(0.03, 0.10, (m, 1)) * np.where(np.arange(m) % 2 == 0, 1.0, -1.0)[:, None]
        self.drift = drift.astype(np.float32)
        self.v = self.drift.copy()
        self._pal = None
        self.cols = np.zeros((m, 3), dtype=np.float32)
        self._kick = np.empty((m, 1), dtype=np.float32)
        self._f = np.empty((m, ctx.n), dtype=np.float32)   # (P, N) falloff
        self._g = np.empty((m, ctx.n), dtype=np.float32)
        self._off = np.empty((m, ctx.n), dtype=bool)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        m = int(p.count)
        if p.palette != self._pal:
            # Colours spread over the palette, each lifted to full brightness so
            # no blob is invisible on a dark palette stop. Shuffled so
            # neighbouring blobs differ.
            t = (0.08 + 0.92 * ((np.arange(self.MAX) * 7) % self.MAX) / (self.MAX - 1))
            c = palette_lerp(named_palette(p.palette), t.astype(np.float32))
            self.cols[:] = c / np.maximum(c.max(axis=1, keepdims=True), 1e-3)
            self._pal = p.palette

        # Velocity relaxes toward the blob's own drift, so a kick dies away but
        # the flow never stalls.
        v, pos, leg, drift = self.v[:m], self.p_pos[:m], self.p_leg[:m], self.drift[:m]
        v += (drift - v) * min(1.0, float(p.damping) * dt)
        beat = float(ctx.audio.beat)
        if beat > 0.0:
            rng.random(out=self._kick, dtype=np.float32)
            kick = self._kick[:m]
            kick -= 0.5
            kick *= 0.3 * float(p.kick_impulse) * beat
            v += kick
        pos += v * (dt * float(p.speed))
        # Through the hub: onto another leg, now heading outward.
        hub = pos[:, 0] < 0.0
        if hub.any():
            hop = rng.integers(1, max(self.k, 2), size=(int(hub.sum()), 1))
            leg[hub] = (leg[hub] + hop) % self.k
            pos[hub] *= -1.0
            v[hub] = np.abs(v[hub])
            drift[hub] = np.abs(drift[hub])
        # At a tip: turn back.
        tip = pos[:, 0] > 1.0
        if tip.any():
            pos[tip] = 2.0 - pos[tip]
            v[tip] = -np.abs(v[tip])
            drift[tip] = -np.abs(drift[tip])

        # (P, N) distance from each blob to each LED: along the blob's own leg,
        # or through the hub to an LED on another leg, so a blob near the hub
        # spills onto every leg.
        radius = float(p.particle_size) * (1.0 + float(ctx.audio.low) * float(p.audio_size_boost))
        f, g, off = self._f[:m], self._g[:m], self._off[:m]
        np.subtract(self.r, pos, out=f)
        np.abs(f, out=f)
        np.add(self.r, pos, out=g)
        np.not_equal(self.leg, leg, out=off)
        np.copyto(f, g, where=off)
        f *= -1.0 / max(radius, 1e-4)
        f += 1.0
        np.maximum(f, 0.0, out=f)
        f *= f
        # Sum every blob's colour into the LEDs in one matrix product.
        np.matmul(f.T, self.cols[:m], out=self.out)
        self.out *= float(p.intensity)
        return clip01(self.out, out=self.out)
