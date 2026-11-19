class PulseMono(Effect):
    """Solid colour that follows one audio band, with a beat punch on top and a
    slow breath when the room is quiet. The safe-idle effect."""

    def init(self, ctx):
        self.env = 0.0          # beat envelope: snaps up on a kick, then decays
        self.breath_t = 0.0

    def render(self, ctx):
        p = ctx.params
        dt = float(ctx.dt)
        self.env = max(self.env * float(np.exp(-dt / 0.25)), float(ctx.audio.beat))
        self.breath_t = (self.breath_t + dt * 0.1) % 1.0
        breath = float(p.breath) * (0.5 + 0.5 * float(np.sin(TAU * self.breath_t)))
        # The band drives brightness; the breath takes over when it falls quiet.
        drive = max(float(ctx.audio.bands[p.audio_band]), breath)
        drive = min(1.0, drive + float(p.beat_punch) * self.env)
        floor = float(p.floor)
        self.out[:] = hex_to_rgb(p.color) * (floor + (1.0 - floor) * drive)
        return self.out
