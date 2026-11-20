class LegDirection(Effect):
    """Calibration aid: soft balls of light roll along every leg so the direction
    each strip runs is obvious. Each ball is a gradient: a flat core that eases
    to dark over `softness`. `leg_colors` tints each strip by its order in the
    config (leg_1 red, leg_2 green, leg_3 blue, leg_4 yellow) to tell them apart."""

    def init(self, ctx):
        n = ctx.n
        self.ci = ctx.frames.chain_index            # 0 at the hub end of each leg, 1 at its tip
        tints = np.array([hex_to_rgb(h) for h in
                          ("#ff3020", "#30ff40", "#3060ff", "#ffd020")], dtype=np.float32)
        self.leg_rgb = np.ones((n, 3), dtype=np.float32)
        for i, s in enumerate(ctx.strips):
            self.leg_rgb[s.pixel_offset:s.pixel_offset + s.pixel_count] = tints[i % 4]
        self.phase = 0.0
        self._d = np.empty(n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        self.phase = (self.phase + float(ctx.dt) / max(float(p.seconds), 0.5)) % 1.0
        if p.direction == "inward":
            pos = 1.0 - self.phase
        elif p.direction == "bounce":
            pos = 1.0 - abs(2.0 * self.phase - 1.0)
        else:
            pos = self.phase
        k = max(int(p.balls), 1)
        half = 0.5 * float(p.lit)
        soft = max(float(p.softness), 0.02)
        d = self._d
        # Distance from the nearest ball centre, in units of one ball's spacing.
        np.multiply(self.ci, k, out=d)
        d -= pos
        d += 0.5
        np.mod(d, 1.0, out=d)
        d -= 0.5
        np.abs(d, out=d)
        d /= half                                   # 0 at the centre, 1 at the ball's edge
        d -= 1.0 - soft
        d /= soft
        np.clip(d, 0.0, 1.0, out=d)                 # 0 in the core, ramps to 1 over the soft edge
        g = 0.5 + 0.5 * np.cos(PI * d)
        floor = float(p.floor)
        g = floor + (1.0 - floor) * g
        col = hex_to_rgb(p.color)
        if p.leg_colors:
            np.multiply(self.leg_rgb, g[:, None], out=self.out)
        else:
            np.multiply(g[:, None], col, out=self.out)
        return self.out
