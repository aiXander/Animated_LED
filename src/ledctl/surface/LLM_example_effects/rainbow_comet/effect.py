class RainbowComet(Effect):
    def init(self, ctx):
        self.out = np.zeros((ctx.n, 3), dtype=np.float32)
        self.phase = 0.0

    def render(self, ctx):
        p = ctx.params
        
        # Advance comet phase
        direction_sign = 1.0 if p.direction == "Clockwise" else -1.0
        self.phase = (self.phase + ctx.dt * p.speed * direction_sign) % 1.0
        
        # Audio reactivity
        amp = p.base_brightness + (1.0 - p.base_brightness) * ctx.audio.low
        
        # Calculate trailing distance (behind the head)
        # Assuming CCW is negative speed, the tail is "behind" in that direction
        dist_from_head = (self.phase - ctx.frames.u_loop) * direction_sign
        dist_from_head %= 1.0
        
        tail_mask = dist_from_head < p.tail_length
        
        # Calculate fade: 1.0 at head, 0.0 at tail end
        fade = 1.0 - (dist_from_head / p.tail_length)
        fade = np.clip(fade, 0, 1)
        
        # Calculate hue based on head position and local position
        hue = (self.phase - (dist_from_head * direction_sign) * p.rainbow_stretch) % 1.0
        
        # Compute final RGB
        rgb = hsv_to_rgb(hue, 1.0, 1.0)
        self.out.fill(0)
        self.out[tail_mask] = rgb[tail_mask] * fade[tail_mask, None] * amp
        
        return self.out