class PulseMono(Effect):
    """Solid colour fills the rig; brightness pulses on audio.low."""

    def init(self, ctx):
        self._scratch = np.empty(ctx.n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        col = hex_to_rgb(p.color)
        amp = float(p.floor) + (1.0 - float(p.floor)) * float(ctx.audio.low)
        # Fill self.out with col, scaled by amp. Avoid per-frame allocation.
        self.out[:] = col[None, :]
        self.out *= amp
        return self.out
