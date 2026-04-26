from pathlib import Path

import pytest

from ledctl.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "config" / "config.dev.yaml"
PI = ROOT / "config" / "config.pi.yaml"


def test_dev_config_loads():
    cfg = load_config(DEV)
    assert cfg.project.name == "festival_scaffold_dev"
    assert cfg.project.target_fps == 60
    assert cfg.transport.mode == "simulator"
    assert len(cfg.strips) == 4
    assert cfg.controllers["gledopto_main"].pixel_count == 1800


def test_pi_config_loads():
    cfg = load_config(PI)
    assert cfg.transport.mode == "ddp"
    assert cfg.controllers["gledopto_main"].host == "10.0.0.2"


def test_overlapping_strips_rejected():
    bad = {
        "project": {"name": "x"},
        "controllers": {"c": {"type": "wled-ddp", "host": "127.0.0.1", "pixel_count": 100}},
        "strips": [
            {
                "id": "a", "controller": "c", "pixel_offset": 0, "pixel_count": 60,
                "geometry": {"type": "line", "start": [0, 0, 0], "end": [1, 0, 0]},
            },
            {
                "id": "b", "controller": "c", "pixel_offset": 50, "pixel_count": 30,
                "geometry": {"type": "line", "start": [0, 0, 0], "end": [1, 0, 0]},
            },
        ],
    }
    with pytest.raises(ValueError, match="overlap"):
        AppConfig.model_validate(bad)


def test_strip_exceeds_controller_pixel_count():
    bad = {
        "project": {"name": "x"},
        "controllers": {"c": {"type": "wled-ddp", "host": "127.0.0.1", "pixel_count": 50}},
        "strips": [
            {
                "id": "a", "controller": "c", "pixel_offset": 0, "pixel_count": 100,
                "geometry": {"type": "line", "start": [0, 0, 0], "end": [1, 0, 0]},
            },
        ],
    }
    with pytest.raises(ValueError, match="pixel_count"):
        AppConfig.model_validate(bad)
