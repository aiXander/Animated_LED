"""Tests for the shared-password auth gate (Phase 8 prep).

Loads a config that has `auth.password` set and verifies:
  - bare requests are bounced (HTML 200 with login page, JSON 401)
  - the cookie path lets you back in
  - the query-string path sets the cookie and lets you in
  - websocket upgrades reject without a cookie and accept with one
  - dev config (no password) is unaffected
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ledctl.api.auth import COOKIE_NAME
from ledctl.api.server import create_app
from ledctl.config import load_config

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "config" / "config.dev.yaml"
PRESETS = ROOT / "config" / "presets"


def _make_authed_config(tmp_path: Path, password: str = "kaailed") -> Path:
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(DEV, cfg_path)
    data = yaml.safe_load(cfg_path.read_text())
    data["auth"] = {"password": password}
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return cfg_path


@pytest.fixture
def authed_client(tmp_path: Path) -> TestClient:
    cfg_path = _make_authed_config(tmp_path)
    cfg = load_config(cfg_path)
    app = create_app(cfg, presets_dir=PRESETS, config_path=cfg_path)
    with TestClient(app) as c:
        yield c


def test_html_request_without_cookie_returns_login_page(authed_client: TestClient) -> None:
    r = authed_client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert 'name="password"' in r.text


def test_json_request_without_cookie_returns_401(authed_client: TestClient) -> None:
    r = authed_client.get("/state", headers={"Accept": "application/json"})
    assert r.status_code == 401


def test_correct_cookie_unlocks_state(authed_client: TestClient) -> None:
    authed_client.cookies.set(COOKIE_NAME, "kaailed")
    r = authed_client.get("/state")
    assert r.status_code == 200
    assert "fps" in r.json()


def test_wrong_cookie_is_rejected(authed_client: TestClient) -> None:
    authed_client.cookies.set(COOKIE_NAME, "wrong")
    r = authed_client.get("/state")
    assert r.status_code == 401


def test_query_password_sets_cookie(authed_client: TestClient) -> None:
    r = authed_client.get("/state?password=kaailed")
    assert r.status_code == 200
    # Cookie persisted on the client for follow-ups.
    assert authed_client.cookies.get(COOKIE_NAME) == "kaailed"


def test_login_post_with_correct_password_redirects_and_sets_cookie(
    authed_client: TestClient,
) -> None:
    r = authed_client.post(
        "/login",
        content="password=kaailed",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # Set-Cookie header must carry the cookie.
    assert COOKIE_NAME in r.headers.get("set-cookie", "")


def test_login_post_with_wrong_password_returns_401(authed_client: TestClient) -> None:
    r = authed_client.post(
        "/login",
        content="password=nope",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Wrong password" in r.text


def test_healthz_is_always_public(authed_client: TestClient) -> None:
    r = authed_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_websocket_rejected_without_cookie(authed_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        authed_client.websocket_connect("/ws/frames"),
    ):
        pass
    # Custom 4401 close code from the auth gate.
    assert exc_info.value.code == 4401


def test_websocket_accepted_with_cookie(authed_client: TestClient) -> None:
    authed_client.cookies.set(COOKIE_NAME, "kaailed")
    with authed_client.websocket_connect("/ws/frames") as ws:
        # If we got here the upgrade succeeded.
        assert ws is not None


def test_dev_config_has_no_auth_gate() -> None:
    """No `auth.password` in the dev config — every endpoint is open."""
    cfg = load_config(DEV)
    app = create_app(cfg, presets_dir=PRESETS)
    with TestClient(app) as c:
        r = c.get("/state")
        assert r.status_code == 200
