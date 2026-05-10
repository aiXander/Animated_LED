class TwinCometsWithSparkles(Effect):
    """Two comets sweep along the rig (top red, bottom blue). Each comet leaves
    fading sparkles behind it in its colour. Brightness pulsates with audio.low."""

    def init(self, ctx):
        self.x = ctx.frames.x
        self.top = ctx.frames.side_top.astype(bool)
        self.bot = ctx.frames.side_bottom.astype(bool)
        self.sparkle_age = np.full(ctx.n, np.inf, dtype=np.float32)
        self.sparkle_rgb = np.zeros((ctx.n, 3), dtype=np.float32)
        self.head_top = 0.0
        self.head_bot = 0.0
        self.last_top = -1
        self.last_bot = -1
        self._fade = np.empty(ctx.n, dtype=np.float32)
        self._d = np.empty(ctx.n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        rate = float(p.comet_rate)
        lead = float(p.lead_offset)

        self.head_top = (self.head_top + rate * dt) % 1.0
        self.head_bot = (self.head_top - lead) % 1.0

        amp = 0.6 + 0.4 * float(ctx.audio.bands[p.audio_band])

        # Sparkle decay (exp).
        np.add(self.sparkle_age, dt, out=self.sparkle_age)
        np.divide(self.sparkle_age, max(float(p.sparkle_decay), 1e-3), out=self._fade)
        np.exp(np.negative(self._fade, out=self._fade), out=self._fade)
        np.multiply(self.sparkle_rgb, self._fade[:, None], out=self.out)

        head_sigma = float(p.head_size)
        self._stamp(self.head_top, hex_to_rgb(p.leader_color), self.top, amp, head_sigma)
        self._stamp(self.head_bot, hex_to_rgb(p.follower_color), self.bot, amp, head_sigma)

        self._deposit(self.head_top, hex_to_rgb(p.leader_color), self.top, "top")
        self._deposit(self.head_bot, hex_to_rgb(p.follower_color), self.bot, "bot")

        return self.out

    def _stamp(self, head_x, color, mask, amp, sigma):
        np.subtract(self.x, head_x, out=self._d)
        np.abs(self._d, out=self._d)
        np.minimum(self._d, 1.0 - self._d, out=self._d)
        s2 = max(sigma * sigma, 1e-9)
        g = np.exp(-(self._d * self._d) * (0.5 / s2)) * amp
        self.out[mask] += g[mask, None] * color

    def _deposit(self, head_x, color, mask, side):
        idxs = np.where(mask)[0]
        if idxs.size == 0:
            return
        i = int(idxs[np.argmin(np.abs(self.x[idxs] - head_x))])
        prev = self.last_top if side == "top" else self.last_bot
        if i != prev:
            self.sparkle_age[i] = 0.0
            self.sparkle_rgb[i] = color
            if side == "top":
                self.last_top = i
            else:
                self.last_bot = i
