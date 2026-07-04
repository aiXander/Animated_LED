class BeatBreath(Effect):
    """The whole rig inhales on every beat and exhales exponentially — the
    canonical beat-envelope pattern (scalar state + dt-based release). A slow
    autonomous breath keeps it alive in silence. The global-envelope archetype:
    no particles, no position tricks, just light with dynamics."""

    def init(self, ctx):
        self.env = 0.0        # beat envelope: jumps on onset, decays toward 0
        self.breath_t = 0.0
        # Soft vignette: brightest at the centre column, dimmer at the corners.
        self.vig = (1.0 - 0.35 * ctx.frames.axial_dist).astype(np.float32)[:, None]

    def render(self, ctx):
        p = ctx.params
        # Exponential release between onsets; a fresh beat snaps it back up.
        decay = np.exp(-float(ctx.dt) / max(float(p.release), 1e-3))
        self.env = max(self.env * decay, float(ctx.audio.beat))

        # Autonomous fallback breath so the rig never goes dead in silence.
        self.breath_t += float(ctx.dt) * float(p.breath_rate)
        idle = 0.5 + 0.5 * np.sin(TAU * self.breath_t)
        drive = max(self.env, float(p.idle_breath) * idle)

        level = float(p.floor) + (1.0 - float(p.floor)) * drive
        np.multiply(self.vig, hex_to_rgb(p.color) * level, out=self.out)
        return self.out
