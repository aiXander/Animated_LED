class FluidStrobeNebula(Effect):
    """Flowing, shifting nebula patterns that react to audio and strobe on the beat."""

    def init(self, ctx):
        # We need per-pixel offsets to create flow
        self.offsets = np.random.uniform(0, TAU, ctx.n).astype(np.float32)
        self.phase = 0.0

    def render(self, ctx):
        p = ctx.params
        dt = ctx.dt
        
        # Advance global phase for movement
        self.phase = (self.phase + dt * p.flow_speed) % TAU
        
        # Calculate spatial flow
        x = ctx.frames.signed_x  # [-1, 1]
        y = ctx.frames.signed_y  # [-1, 1]
        
        # Create shifting plasma-like waves
        wave = np.sin((x * 5) + self.phase) + np.cos((y * 10) + self.phase * 0.7)
        pattern = (np.sin(wave * 2.0 + self.phase) * 0.5 + 0.5)
        
        # Audio modulation
        bass = ctx.audio.low
        beat = ctx.audio.beat
        
        # Color Mapping
        h = (pattern * 0.3 + self.phase * 0.1 / TAU) % 1.0
        v = pattern * (0.3 + 0.7 * bass)
        
        # Apply result
        self.out[:] = hsv_to_rgb(h, 0.9, v)
        
        # Additive strobe
        if beat > 0:
            self.out += beat * 1.5
            
        return clip01(self.out)
