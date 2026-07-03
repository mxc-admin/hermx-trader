"""Static dashboard paths are auth-challenged, not just the exact route set (H1).

do_GET used an exact-path allowlist to decide whether to challenge for auth. But
_serve_static maps directory requests to index.html, and index.html has the auth token
(DASH_AUTH_TOKEN == HERMX_SECRET) injected into a <meta> tag. So /dashboard/index.html
was NOT in the allowlist yet still served the token to any unauthenticated caller. The
prefix check challenges every path under /dashboard (and /shadow/dashboard). These tests
build a minimal static export and assert the index is behind auth.
"""
from __future__ import annotations

import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import dashboard as dash


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _get(port, path, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def _static_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "index.html").write_text("<html><head></head><body>hi</body></html>", encoding="utf-8")
    return out


def test_dashboard_index_requires_auth_no_token_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dash, "DASH_AUTH_TOKEN", "dash-token")
    monkeypatch.setattr(dash, "STATIC_DIR", _static_dir(tmp_path))

    server, thread = _serve(dash.Handler)
    try:
        status, body = _get(server.server_address[1], "/dashboard/index.html")
        assert status == 401
        # The token must never appear in an unauthenticated response body.
        assert b"dash-token" not in body
    finally:
        _stop(server, thread)


def test_dashboard_index_served_with_token_when_authed(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dash, "DASH_AUTH_TOKEN", "dash-token")
    monkeypatch.setattr(dash, "STATIC_DIR", _static_dir(tmp_path))

    server, thread = _serve(dash.Handler)
    try:
        status, body = _get(
            server.server_address[1],
            "/dashboard/index.html",
            headers={"Authorization": "Bearer dash-token"},
        )
        assert status == 200
        assert b'<meta name="hermx-token" content="dash-token">' in body
    finally:
        _stop(server, thread)
