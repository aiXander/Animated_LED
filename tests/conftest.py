"""Suite-wide guard: tests never touch the operator's live audio setup.

`config/config.dev.yaml` binds UDP 9000 and spawns the audio server, and WZRD
on the same Mac needs that port. Every app a test builds therefore gets an
ephemeral OSC port and spawns nothing; the WZRD listener likewise gets an
ephemeral loopback port, so the suite runs beside a live ledctl.
"""

from __future__ import annotations

import pytest

from ledctl.audio import AudioBridge
from ledctl.wzrd import WzrdListener


@pytest.fixture(autouse=True)
def _isolated_audio_bridge(monkeypatch):
    monkeypatch.delenv("LEDCTL_AUDIO_SOURCE", raising=False)
    monkeypatch.delenv("LEDCTL_OSC_ALLOW_FROM", raising=False)
    real = AudioBridge.from_config.__func__

    def from_config(cls, cfg):
        return real(cls, cfg.model_copy(update={"autostart": False, "osc_listen_port": 0}))

    monkeypatch.setattr(AudioBridge, "from_config", classmethod(from_config))

    real_init = WzrdListener.__init__

    def init(self, host="127.0.0.1", port=0, **kw):
        real_init(self, host="127.0.0.1", port=0, **kw)

    monkeypatch.setattr(WzrdListener, "__init__", init)
