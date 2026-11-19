class PulseMono(Effect):
    """One colour, brightest at the hub and fading toward the tips. An audio
    band lifts it, a beat adds a soft swell, and when the room is quiet a slow
    breath rolls out from the hub. The safe-idle effect."""

    def init(self, ctx):
        self.r = ctx.frames.radius
        self.env = 0.0          # beat envelope: jumps on a kick, then decays
        self.breath_t = 0.0     # wrapped clock, one turn per breath
        self._a = np.empty(ctx.n, dtype=np.float32)
        self._b = np.empty(ctx.n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        self.env = max(self.env * float(np.exp(-dt / 0.6)), float(ctx.audio.beat))
        self.breath_t = (self.breath_t + dt / 8.0) % 1.0
        a, b = self._a, self._b
        # The breath leaves the hub as a slow wave: the tips trail the hub by a
        # quarter of a cycle.
        np.multiply(self.r, -0.25 * TAU, out=a)
        a += TAU * self.breath_t
        np.sin(a, out=a)
        a *= 0.5 * float(p.breath)
        a += 0.5 * float(p.breath)
        # The band drives brightness; the breath takes over when it falls quiet.
        np.maximum(a, float(p.band_depth) * float(ctx.audio.bands[p.audio_band]), out=a)
        a += float(p.beat_punch) * self.env
        np.minimum(a, 1.0, out=a)
        floor = float(p.floor)
        a *= 1.0 - floor
        a += floor
        np.multiply(self.r, -float(p.tip_fade), out=b)
        b += 1.0
        a *= b
        np.outer(a, hex_to_rgb(p.color), out=self.out)
        return self.out
