class AudioRadial(Effect):
    """Palette-mapped concentric rings expanding from the rig centre. Speed
    locked to ctx.t; brightness modulated by audio.bands[<param>]."""

    def init(self, ctx):
        self.r = ctx.frames.radius
        self.lut = named_palette("ocean")
        self._scratch = np.empty(ctx.n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        speed = float(p.ring_speed)
        floor = float(p.brightness_floor)
        # Pick palette per param. (Recompute LUT only on change to keep the
        # hot path allocation-free.)
        if getattr(self, "_palette_name", None) != p.palette:
            self.lut = named_palette(p.palette)
            self._palette_name = p.palette
        # ring-position = (radius - t * speed) wrapped to [0, 1)
        np.subtract(self.r, ctx.t * speed, out=self._scratch)
        np.mod(self._scratch, 1.0, out=self._scratch)
        idx = (self._scratch * (LUT_SIZE - 1)).astype(np.int32)
        np.take(self.lut, idx, axis=0, out=self.out)
        amp = floor + (1.0 - floor) * float(ctx.audio.bands[p.audio_band])
        self.out *= amp
        return self.out
