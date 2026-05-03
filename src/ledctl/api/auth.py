"""Shared-password gate for the HTTP/WS surface.

Activated by setting `auth.password` in the active config. When set:
  - All HTTP requests must carry a matching `ledctl_auth` cookie (or be
    redirected through `/login?password=…` / `POST /login`).
  - All WebSocket upgrades must arrive with the same cookie.
  - `/login` and `/healthz` are always public.
  - Static asset routes are gated too — the operator UI is the *only* thing
    served from this Pi, so a logged-out user sees the login page first.

Auth is intentionally minimalist: a shared share-code that bar staff can
type once on their phone. Behind Tailscale (recommended) this is plenty;
on the venue WiFi it keeps drunks-with-laptops off the panel. Not designed
to defeat a determined attacker.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp
from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

COOKIE_NAME = "ledctl_auth"

# Always-public paths. `/login` is the obvious one; `/healthz` is so a future
# uptime probe (Phase 9) doesn't trip the gate.
_PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/healthz"})


def _password_matches(supplied: str | None, expected: str) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def is_request_authenticated(request: Request, expected: str) -> bool:
    return _password_matches(request.cookies.get(COOKIE_NAME), expected)


def is_websocket_authenticated(ws: WebSocket, expected: str) -> bool:
    # Starlette parses cookies on the WebSocket scope the same way it does
    # on Request — same dict-like accessor, so re-use it.
    return _password_matches(ws.cookies.get(COOKIE_NAME), expected)


class PasswordAuthMiddleware(BaseHTTPMiddleware):
    """Gates every HTTP request behind the shared password.

    Strategy:
      - Request has a valid cookie → pass through.
      - Request has `?password=…` matching → set cookie, pass through.
      - Browser HTML navigation (Accept: text/html) → 200 + login page.
      - Anything else (XHR / curl / fetch) → 401 JSON.

    Putting the login page on a 200 (not 302 to /login) keeps deep-linked
    URLs typeable from the phone: paste `…/?password=kaailed` once and the
    cookie sticks.
    """

    def __init__(self, app: ASGIApp, password: str, cookie_max_age_days: int = 30) -> None:
        super().__init__(app)
        self._password = password
        self._cookie_max_age = int(cookie_max_age_days) * 24 * 60 * 60

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        if is_request_authenticated(request, self._password):
            return await call_next(request)

        # Login-via-query: lets you bookmark `…/?password=kaailed`.
        query_pw = request.query_params.get("password")
        if query_pw is not None and _password_matches(query_pw, self._password):
            response = await call_next(request)
            _set_auth_cookie(response, self._password, self._cookie_max_age)
            return response

        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            return HTMLResponse(_login_page_html(), status_code=200)
        return JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Cookie"},
        )


def _set_auth_cookie(response: Response, password: str, max_age: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=password,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        # Don't force `secure=True` — the Pi is reached over HTTP on the venue
        # LAN. Tailscale users can layer HTTPS on top later if they want.
        secure=False,
        path="/",
    )


def attach_password_auth(app: Any, password: str, cookie_max_age_days: int = 30) -> None:
    """Wire the middleware + login routes onto an existing FastAPI app.

    Idempotent on the routes (re-mounting in tests is fine), but must be
    called exactly once per app so the middleware stack stays clean.
    """
    from urllib.parse import parse_qs

    log.info("auth: shared-password gate enabled (cookie: %s)", COOKIE_NAME)

    app.add_middleware(
        PasswordAuthMiddleware,
        password=password,
        cookie_max_age_days=cookie_max_age_days,
    )
    cookie_max_age = int(cookie_max_age_days) * 24 * 60 * 60

    @app.get("/login", include_in_schema=False)
    async def _login_get(password: str | None = None) -> Response:
        if password is not None and _password_matches(password, app.state.auth_password):
            response = RedirectResponse("/", status_code=303)
            _set_auth_cookie(response, app.state.auth_password, cookie_max_age)
            return response
        return HTMLResponse(_login_page_html())

    @app.post("/login", include_in_schema=False)
    async def _login_post(request: Request) -> Response:
        # Parse `application/x-www-form-urlencoded` by hand. Avoids depending
        # on `python-multipart` for one trivial field — `Form(...)` would pull
        # it in just to read a single password.
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        supplied = (parsed.get("password") or [""])[0]
        if _password_matches(supplied, app.state.auth_password):
            response = RedirectResponse("/", status_code=303)
            _set_auth_cookie(response, app.state.auth_password, cookie_max_age)
            return response
        return HTMLResponse(_login_page_html(error=True), status_code=401)

    @app.post("/logout", include_in_schema=False)
    async def _logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    app.state.auth_password = password


def _login_page_html(error: bool = False) -> str:
    err_html = (
        '<p class="err">Wrong password.</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ledctl — sign in</title>
  <style>
    :root {{ color-scheme: dark; }}
    html, body {{ margin: 0; height: 100%; background: #050505; color: #ddd;
                font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    main {{ min-height: 100%; display: grid; place-items: center; padding: 1.5rem; }}
    form {{ width: min(22rem, 100%); display: grid; gap: 0.75rem;
            padding: 1.5rem; border: 1px solid #1a1a1a; border-radius: 0.75rem;
            background: #0a0a0a; }}
    h1 {{ font-size: 1rem; margin: 0 0 0.25rem 0; color: #93c5fd; }}
    .hint {{ font-size: 0.8rem; color: #888; margin: 0; }}
    label {{ font-size: 0.85rem; color: #aaa; }}
    input[type=password] {{
      width: 100%; padding: 0.75rem 0.875rem; font: inherit;
      background: #050505; color: #fff; border: 1px solid #2a2a2a;
      border-radius: 0.5rem; min-height: 2.75rem;
    }}
    button {{
      padding: 0.75rem 0.875rem; font: inherit;
      background: #1f2937; color: #f9fafb; border: 1px solid #2a2a2a;
      border-radius: 0.5rem; cursor: pointer; min-height: 2.75rem;
    }}
    button:hover {{ background: #273549; }}
    .err {{ color: #f87171; margin: 0; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <main>
    <form method="post" action="/login">
      <h1>ledctl</h1>
      <p class="hint">Enter the share code to control the lights.</p>
      {err_html}
      <label for="pw">Password</label>
      <input id="pw" name="password" type="password" autofocus
             autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>
"""
