"""The render loop against a browser that has stopped reading its preview."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from ledctl.config import load_config
from ledctl.engine import Engine
from ledctl.masters import MasterControls
from ledctl.surface import EffectStore, Runtime
from ledctl.topology import Topology
from ledctl.transports.ddp import DDPTransport
from ledctl.transports.simulator import SimulatorTransport
from ledctl.transports.split import SplitTransport

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


class _StuckBrowser:
    """A /ws/frames client on a congested link: a send never completes."""

    async def send_bytes(self, data: bytes) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


async def test_a_stalled_browser_does_not_hold_up_led_frames(tmp_path: Path):
    cfg = load_config(DEV)
    topology = Topology.from_config(cfg)
    wled = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wled.bind(("127.0.0.1", 0))
    sim = SimulatorTransport(target_fps=60)
    led = DDPTransport("127.0.0.1", wled.getsockname()[1])
    transport = SplitTransport(sim, led)
    engine = Engine(
        cfg, topology, transport, Runtime(topology, MasterControls()), EffectStore(tmp_path),
    )
    await sim.add_client(_StuckBrowser())
    try:
        await engine.start()
        await asyncio.sleep(0.5)
    finally:
        await engine.stop()
        await transport.close()
        wled.close()
    # 0.5 s at the configured frame rate, with slack for a loaded CI machine.
    assert led.frames_sent >= cfg.project.target_fps // 4


_CLOCK_PROBE = """\
class ClockProbe(Effect):
    def init(self, ctx):
        self.dts = []
        self.ts = []
    def render(self, ctx):
        self.dts.append(ctx.dt)
        self.ts.append(ctx.t)
        return self.out
"""


async def test_effect_clock_starts_at_zero_and_never_runs_backwards(tmp_path: Path):
    """The first tick once handed effects dt = -(host uptime) and started
    ctx.t there too: envelopes of the form exp(-dt / tau) overflowed to NaN,
    and float32 maths on ctx.t lost its precision for the whole run."""
    cfg = load_config(DEV)
    topology = Topology.from_config(cfg)
    runtime = Runtime(topology, MasterControls())
    runtime.install_layer("live", name="clock_probe", summary="", source=_CLOCK_PROBE,
                          param_schema=[])
    probe = runtime.live.layers[0].instance
    fenced = len(probe.dts)              # frames the install-time fence test rendered
    transport = SplitTransport(SimulatorTransport(target_fps=60), None)
    engine = Engine(cfg, topology, transport, runtime, EffectStore(tmp_path))
    try:
        await engine.start()
        await asyncio.sleep(0.2)
    finally:
        await engine.stop()
        await transport.close()
    dts, ts = probe.dts[fenced:], probe.ts[fenced:]
    assert dts, "the engine never rendered the live layer"
    assert min(dts) >= 0.0
    assert 0.0 <= ts[0] < 1.0
