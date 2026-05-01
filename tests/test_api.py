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
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(DEV, cfg_path)
    cfg = load_config(cfg_path)
    app = create_app(cfg, presets_dir=PRESETS, config_path=cfg_path)
    with TestClient(app) as c:
        c.app.state._cfg_path = cfg_path
        yield c


def test_state_reports_default_layer(client: TestClient):
    r = client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert body["target_fps"] > 0
    assert body["transport_mode"] == "simulator"
    assert body["blackout"] is False
    # Default boot stack is a single palette_lookup(wave + palette_stops).
    assert len(body["layers"]) == 1
    layer = body["layers"][0]
    assert layer["node"]["kind"] == "palette_lookup"
    scalar = layer["node"]["params"]["scalar"]
    assert scalar["kind"] == "wave"
    assert scalar["params"]["axis"] == "x"
    assert scalar["params"]["speed"] == 0.15
    assert scalar["params"]["cross_phase"] == [0.0, 0.075, 0.0]
    brightness = layer["node"]["params"]["brightness"]
    assert brightness["kind"] == "envelope"
    assert brightness["params"]["floor"] == 0.65
    assert brightness["params"]["ceiling"] == 1.0


def test_surface_primitives_lists_catalogue(client: TestClient):
    r = client.get("/surface/primitives")
    assert r.status_code == 200
    body = r.json()
    # Every registered primitive shows up
    expected_subset = {
        "wave", "radial", "noise", "sparkles", "lfo", "audio_band",
        "envelope", "constant", "palette_named", "palette_stops",
        "palette_lookup", "solid", "mix", "mul", "add",
    }
    assert expected_subset.issubset(set(body))
    for entry in body.values():
        assert "params_schema" in entry
        assert "output_kind" in entry
        assert "summary" in entry


def test_post_layer_appends(client: TestClient):
    r = client.post(
        "/layers",
        json={
            "node": {
                "kind": "palette_lookup",
                "params": {
                    "scalar": {"kind": "wave", "params": {"speed": 0.5}},
                    "palette": "fire",
                },
            }
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["layer_index"] == 1
    assert len(body["layers"]) == 2
    assert body["layers"][-1]["node"]["kind"] == "palette_lookup"


def test_post_layer_invalid_node_422(client: TestClient):
    r = client.post(
        "/layers",
        json={"node": {"kind": "wave", "params": {}}},  # leaf must be rgb_field
    )
    assert r.status_code == 422


def test_post_layer_unknown_kind_422(client: TestClient):
    r = client.post("/layers", json={"node": {"kind": "nope", "params": {}}})
    assert r.status_code == 422


def test_post_layer_invalid_blend_422(client: TestClient):
    r = client.post(
        "/layers",
        json={
            "node": {
                "kind": "palette_lookup",
                "params": {
                    "scalar": {"kind": "constant", "params": {"value": 0.0}},
                    "palette": "white",
                },
            },
            "blend": "subtract",
        },
    )
    assert r.status_code == 422


def test_patch_layer_updates(client: TestClient):
    r = client.patch(
        "/layers/0",
        json={
            "node": {
                "kind": "palette_lookup",
                "params": {
                    "scalar": {"kind": "wave", "params": {"speed": 2.5}},
                    "palette": "fire",
                },
            },
            "opacity": 0.75,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["layers"][0]["opacity"] == 0.75
    scalar = body["layers"][0]["node"]["params"]["scalar"]
    assert scalar["params"]["speed"] == 2.5


def test_patch_layer_out_of_range_404(client: TestClient):
    r = client.patch("/layers/9", json={"opacity": 0.5})
    assert r.status_code == 404


def test_delete_layer(client: TestClient):
    client.post(
        "/layers",
        json={
            "node": {
                "kind": "palette_lookup",
                "params": {
                    "scalar": {"kind": "noise", "params": {"speed": 0.2}},
                    "palette": "white",
                },
            }
        },
    )
    r = client.delete("/layers/0")
    assert r.status_code == 200, r.text
    body = r.json()
    # Default scroll layer at index 0 dropped, sparkle remaining.
    assert len(body["layers"]) == 1


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
    # peak preset is a fire-palette wave with a sparkles layer on top.
    assert len(body["layers"]) == 2
    kinds = [layer["node"]["kind"] for layer in body["layers"]]
    assert kinds == ["palette_lookup", "sparkles"]


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


# ---- masters ----


def test_get_masters_returns_defaults(client: TestClient):
    r = client.get("/masters")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "brightness": 1.0,
        "speed": 1.0,
        "audio_reactivity": 1.0,
        "saturation": 1.0,
        "freeze": False,
    }


def test_patch_masters_merges_clamped(client: TestClient):
    r = client.patch("/masters", json={"brightness": 0.4, "speed": 2.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brightness"] == 0.4
    assert body["speed"] == 2.0
    # /state reflects the patch
    state = client.get("/state").json()
    assert state["masters"]["brightness"] == 0.4
    assert state["masters"]["speed"] == 2.0


def test_patch_masters_rejects_out_of_range(client: TestClient):
    r = client.patch("/masters", json={"brightness": 5.0})
    assert r.status_code == 422
    r = client.patch("/masters", json={"speed": -0.5})
    assert r.status_code == 422


def test_patch_masters_freeze_is_a_bool(client: TestClient):
    r = client.patch("/masters", json={"freeze": True})
    assert r.status_code == 200
    assert r.json()["freeze"] is True


def test_patch_masters_persist_writes_yaml(writable_client: TestClient):
    cfg_path: Path = writable_client.app.state._cfg_path
    r = writable_client.patch(
        "/masters", json={"brightness": 0.6, "saturation": 0.8, "persist": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["saved_to"] == str(cfg_path)
    on_disk = yaml.safe_load(cfg_path.read_text())
    assert on_disk["masters"]["brightness"] == 0.6
    assert on_disk["masters"]["saturation"] == 0.8


# ---- Phase 4: calibration ----


def test_calibration_solo_sets_state(client: TestClient):
    r = client.post("/calibration/solo", json={"indices": [42, 1799]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["calibration"]["mode"] == "solo"
    assert body["calibration"]["indices"] == [42, 1799]


def test_calibration_solo_rejects_out_of_range(client: TestClient):
    r = client.post("/calibration/solo", json={"indices": [9999]})
    assert r.status_code == 422


def test_calibration_solo_rejects_empty(client: TestClient):
    r = client.post("/calibration/solo", json={"indices": []})
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


def test_calibration_walk_rejects_zero_step(client: TestClient):
    r = client.post("/calibration/walk", json={"step": 0, "interval": 1.0})
    assert r.status_code == 422


# ---- Phase 4: editor (config GET / PUT) ----


def test_get_config_returns_full_layout(client: TestClient):
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert "project" in body and "controllers" in body and "strips" in body
    assert "masters" in body
    assert len(body["strips"]) == 4
    assert body["strips"][0]["geometry"]["type"] == "line"


def test_get_editor_html(client: TestClient):
    r = client.get("/editor")
    assert r.status_code == 200
    assert "ledctl · layout editor" in r.text


def test_put_config_swaps_topology_and_writes_file(writable_client: TestClient):
    cfg_path: Path = writable_client.app.state._cfg_path
    base = writable_client.get("/config").json()
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

    on_disk = yaml.safe_load(cfg_path.read_text())
    found = next(s for s in on_disk["strips"] if s["id"] == "bottom_right")
    assert found["geometry"]["start"][1] == -0.5

    assert cfg_path.with_suffix(cfg_path.suffix + ".bak").exists()

    topo = writable_client.get("/topology").json()
    br_strip = next(s for s in topo["strips"] if s["id"] == "bottom_right")
    assert br_strip["start"][1] == -0.5


def test_put_config_rejects_overlap(writable_client: TestClient):
    base = writable_client.get("/config").json()
    new_strips = [{**s} for s in base["strips"]]
    new_strips[1]["pixel_offset"] = 0
    r = writable_client.put("/config", json={"strips": new_strips})
    assert r.status_code == 422


def test_put_config_changing_pixel_count_resizes_engine(writable_client: TestClient):
    base = writable_client.get("/config").json()
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
    assert len(state["layers"]) == 1
    assert state["layers"][0]["node"]["kind"] == "palette_lookup"

    topo = writable_client.get("/topology").json()
    assert topo["pixel_count"] == 900
    assert len(topo["leds"]) == 900
