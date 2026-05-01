import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ledctl.api.server import create_app
from ledctl.config import load_config

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "config" / "config.dev.yaml"
PRESETS = ROOT / "config" / "presets"


@pytest.fixture
def client() -> TestClient:
    cfg = load_config(DEV)
    app = create_app(cfg, presets_dir=PRESETS)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def writable_client(tmp_path: Path) -> TestClient:
    """Client wired to a tmp-copied config file so PUT /config is safe to test."""
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(DEV, cfg_path)
    cfg = load_config(cfg_path)
    app = create_app(cfg, presets_dir=PRESETS, config_path=cfg_path)
    with TestClient(app) as c:
        c.app.state._cfg_path = cfg_path  # exposed for the test to read
        yield c


def test_state_reports_default_layer(client: TestClient):
    r = client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert body["target_fps"] == 60
    assert body["transport_mode"] == "simulator"
    assert body["blackout"] is False
    # Default boot stack is a single `scroll` layer with audio.rms bound to
    # brightness in [0.5, 1.0] — the new field+palette+bindings shape.
    assert [layer["effect"] for layer in body["layers"]] == ["scroll"]
    layer = body["layers"][0]
    assert layer["params"]["axis"] == "x"
    assert layer["params"]["speed"] == 0.15
    assert layer["params"]["cross_phase"] == [0.0, 0.075, 0.0]
    binding = layer["params"]["bindings"]["brightness"]
    assert binding["source"] == "audio.rms"
    assert binding["floor"] == 0.5
    assert binding["ceiling"] == 1.0


def test_effects_lists_field_generators(client: TestClient):
    r = client.get("/effects")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"scroll", "radial", "sparkle", "noise"}
    for name in body:
        assert "params_schema" in body[name]
        assert body[name]["params_schema"]["type"] == "object"


def test_post_effect_appends_layer(client: TestClient):
    r = client.post(
        "/effects/scroll",
        json={"params": {"palette": "fire", "speed": 0.5}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Default boot stack has 1 layer; ours is appended as the 2nd.
    assert body["layer_index"] == 1
    assert len(body["layers"]) == 2
    assert body["layers"][-1]["effect"] == "scroll"
    assert body["layers"][-1]["params"]["palette"]["name"] == "fire"


def test_post_unknown_effect_404(client: TestClient):
    r = client.post("/effects/nope", json={})
    assert r.status_code == 404


def test_post_effect_invalid_params_422(client: TestClient):
    r = client.post(
        "/effects/scroll",
        json={"params": {"palette": "not-a-real-palette"}},
    )
    assert r.status_code == 422


def test_post_effect_invalid_blend_422(client: TestClient):
    r = client.post("/effects/scroll", json={"blend": "subtract"})
    assert r.status_code == 422


def test_patch_layer_updates(client: TestClient):
    r = client.patch(
        "/layer/0",
        json={"params": {"speed": 2.5}, "opacity": 0.75},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["layers"][0]["params"]["speed"] == 2.5
    assert body["layers"][0]["opacity"] == 0.75


def test_patch_layer_out_of_range_404(client: TestClient):
    r = client.patch("/layer/9", json={"opacity": 0.5})
    assert r.status_code == 404


def test_delete_layer(client: TestClient):
    # Boot stack has 1 layer; add a sparkle, then delete the original scroll.
    client.post("/effects/sparkle", json={"params": {"palette": "white"}})
    r = client.delete("/layer/0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [layer["effect"] for layer in body["layers"]] == ["sparkle"]


def test_blackout_resume(client: TestClient):
    r = client.post("/blackout")
    assert r.status_code == 200
    assert r.json()["blackout"] is True
    assert client.get("/state").json()["blackout"] is True

    r = client.post("/resume")
    assert r.status_code == 200
    assert r.json()["blackout"] is False
    assert client.get("/state").json()["blackout"] is False


def test_presets_list_includes_seed_files(client: TestClient):
    r = client.get("/presets")
    assert r.status_code == 200
    names = set(r.json()["presets"])
    assert {"chill", "peak", "cooldown"}.issubset(names)


def test_apply_preset_replaces_stack(client: TestClient):
    r = client.post("/presets/peak", json={"crossfade_seconds": 0.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "peak"
    assert body["crossfade_seconds"] == 0.0
    # peak preset is a fire-palette scroll with a sparkle layer on top.
    assert [layer["effect"] for layer in body["layers"]] == ["scroll", "sparkle"]


def test_apply_unknown_preset_404(client: TestClient):
    r = client.post("/presets/does-not-exist", json={})
    assert r.status_code == 404


def test_topology_still_served(client: TestClient):
    r = client.get("/topology")
    assert r.status_code == 200
    body = r.json()
    assert body["pixel_count"] == 1800
    assert len(body["leds"]) == 1800
    assert len(body["strips"]) == 4


# ---- Phase 4: calibration ----


def test_calibration_solo_sets_state(client: TestClient):
    r = client.post("/calibration/solo", json={"indices": [42, 1799]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["calibration"]["mode"] == "solo"
    assert body["calibration"]["indices"] == [42, 1799]
    state = client.get("/state").json()
    assert state["calibration"]["indices"] == [42, 1799]


def test_calibration_solo_rejects_out_of_range(client: TestClient):
    r = client.post("/calibration/solo", json={"indices": [9999]})
    assert r.status_code == 422


def test_calibration_solo_rejects_empty(client: TestClient):
    r = client.post("/calibration/solo", json={"indices": []})
    # min_length=1 → pydantic 422
    assert r.status_code == 422


def test_calibration_walk_starts_and_stops(client: TestClient):
    r = client.post("/calibration/walk", json={"step": 50, "interval": 0.25})
    assert r.status_code == 200, r.text
    cal = r.json()["calibration"]
    assert cal["mode"] == "walk"
    assert cal["step"] == 50
    assert cal["interval"] == 0.25

    r = client.post("/calibration/stop")
    assert r.status_code == 200
    assert r.json()["calibration"] is None
    assert client.get("/state").json()["calibration"] is None


def test_calibration_walk_rejects_zero_step(client: TestClient):
    r = client.post("/calibration/walk", json={"step": 0, "interval": 1.0})
    assert r.status_code == 422


# ---- Phase 4: editor (config GET / PUT) ----


def test_get_config_returns_full_layout(client: TestClient):
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert "project" in body and "controllers" in body and "strips" in body
    assert len(body["strips"]) == 4
    assert body["strips"][0]["geometry"]["type"] == "line"


def test_get_editor_html(client: TestClient):
    r = client.get("/editor")
    assert r.status_code == 200
    assert "ledctl · layout editor" in r.text


def test_put_config_swaps_topology_and_writes_file(writable_client: TestClient):
    cfg_path: Path = writable_client.app.state._cfg_path
    base = writable_client.get("/config").json()
    # Move the bottom-right strip up to y = -0.5 (matches the rest).
    new_strips = [{**s} for s in base["strips"]]
    for s in new_strips:
        if s["id"] == "bottom_right":
            s["geometry"] = {
                "type": "line",
                "start": [0.0, -0.5, 0.0],
                "end": [15.0, -0.5, 0.0],
            }

    r = writable_client.put("/config", json={"strips": new_strips})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pixel_count"] == 1800
    assert any(
        s["id"] == "bottom_right" and s["start"][1] == -0.5 for s in body["strips"]
    )

    # Disk reflects the change.
    on_disk = yaml.safe_load(cfg_path.read_text())
    found = next(s for s in on_disk["strips"] if s["id"] == "bottom_right")
    assert found["geometry"]["start"][1] == -0.5

    # Backup of the previous version is alongside.
    assert cfg_path.with_suffix(cfg_path.suffix + ".bak").exists()

    # Topology endpoint reflects the new layout.
    topo = writable_client.get("/topology").json()
    br_strip = next(s for s in topo["strips"] if s["id"] == "bottom_right")
    assert br_strip["start"][1] == -0.5


def test_put_config_rejects_overlap(writable_client: TestClient):
    base = writable_client.get("/config").json()
    new_strips = [{**s} for s in base["strips"]]
    # Force two strips to overlap in pixel range.
    new_strips[1]["pixel_offset"] = 0
    r = writable_client.put("/config", json={"strips": new_strips})
    assert r.status_code == 422


def test_put_config_changing_pixel_count_resizes_engine(writable_client: TestClient):
    base = writable_client.get("/config").json()
    # Halve every strip — total pixels go 1800 → 900.
    new_strips = [{**s} for s in base["strips"]]
    half = []
    offset = 0
    for s in new_strips:
        s = {**s, "pixel_count": s["pixel_count"] // 2, "pixel_offset": offset}
        offset += s["pixel_count"]
        half.append(s)
    r = writable_client.put("/config", json={"strips": half})
    assert r.status_code == 200, r.text
    assert r.json()["pixel_count"] == 900

    state = writable_client.get("/state").json()
    # Default boot stack is preserved across the swap.
    assert [layer["effect"] for layer in state["layers"]] == ["scroll"]

    topo = writable_client.get("/topology").json()
    assert topo["pixel_count"] == 900
    assert len(topo["leds"]) == 900
