"""Dashboard refresh presence stamp (Phase 1 MVP).

Authenticated model-serving /api GETs stamp ``_LAST_API_HIT``; the background
refresh loop consults ``_should_rebuild`` to rebuild every tick while a viewer
is active and back off to IDLE_REBUILD_SECONDS otherwise. Offline: the model
payload callables are stubbed so no build (and no network) ever runs.
"""
from __future__ import annotations

import importlib
import os
from http.client import HTTPConnection

import dashboard as dashboard_mod

from conftest import _serve, _stop


# ---------------------------------------------------------------------------
# _should_rebuild truth table (pure function, explicit windows).
# ---------------------------------------------------------------------------

def _should(now, last_hit, last_build):
    return dashboard_mod._should_rebuild(
        now, last_hit, last_build, active_window=90.0, idle_rebuild=300.0
    )


def test_active_viewer_rebuilds_every_tick():
    # Fresh hit 10s ago -> rebuild even though the last build was 1s ago.
    assert _should(1000.0, 990.0, 999.0) is True


def test_never_built_rebuilds_even_with_no_hits():
    assert _should(1000.0, 0.0, 0.0) is True


def test_idle_not_due_skips():
    # Hit aged out (400s ago), last build 100s ago -> skip.
    assert _should(1000.0, 600.0, 900.0) is False


def test_idle_due_rebuilds():
    # Hit aged out, last build 300s ago -> idle refresh due.
    assert _should(1000.0, 600.0, 700.0) is True


def test_no_hits_ever_recent_build_skips():
    # last_hit=0 (never) must not count as active; recent build -> skip.
    assert _should(1000.0, 0.0, 999.0) is False


def test_active_window_boundary_is_idle():
    # Exactly active_window seconds since the hit -> no longer active.
    assert _should(1000.0, 910.0, 999.0) is False


def test_default_windows_come_from_dashboard_module(monkeypatch):
    monkeypatch.setattr(dashboard_mod, "ACTIVE_WINDOW_SECONDS", 5.0)
    monkeypatch.setattr(dashboard_mod, "IDLE_REBUILD_SECONDS", 50.0)
    # Hit 4s ago: inside the patched 5s active window.
    assert dashboard_mod._should_rebuild(1000.0, 996.0, 999.0) is True
    # Hit 10s ago (idle), build 40s ago: under the patched 50s idle interval.
    assert dashboard_mod._should_rebuild(1000.0, 990.0, 960.0) is False
    # Same but build 60s ago: idle refresh due.
    assert dashboard_mod._should_rebuild(1000.0, 990.0, 940.0) is True


def test_window_constants_env_overridable():
    saved = {
        name: os.environ.get(name)
        for name in ("HERMX_DASHBOARD_ACTIVE_WINDOW_SECONDS",
                     "HERMX_DASHBOARD_IDLE_REBUILD_SECONDS")
    }
    os.environ["HERMX_DASHBOARD_ACTIVE_WINDOW_SECONDS"] = "45"
    os.environ["HERMX_DASHBOARD_IDLE_REBUILD_SECONDS"] = "600"
    try:
        importlib.reload(dashboard_mod)
        assert dashboard_mod.ACTIVE_WINDOW_SECONDS == 45.0
        assert dashboard_mod.IDLE_REBUILD_SECONDS == 600.0
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        importlib.reload(dashboard_mod)
    assert dashboard_mod.ACTIVE_WINDOW_SECONDS == float(
        saved["HERMX_DASHBOARD_ACTIVE_WINDOW_SECONDS"] or 90)
    assert dashboard_mod.IDLE_REBUILD_SECONDS == float(
        saved["HERMX_DASHBOARD_IDLE_REBUILD_SECONDS"] or 300)


# ---------------------------------------------------------------------------
# Presence stamp over the real Handler (loopback, offline payload stubs).
# ---------------------------------------------------------------------------

def _get(port, path, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def _arm_auth(monkeypatch):
    monkeypatch.setattr(dashboard_mod, "DASH_AUTH_ENABLED", True)
    monkeypatch.setattr(dashboard_mod, "DASH_AUTH_TOKEN", "dash-token")
    monkeypatch.setattr(dashboard_mod, "_LAST_API_HIT", 0.0)
    monkeypatch.setattr(dashboard_mod, "api_payload", lambda: {"ok": True})
    monkeypatch.setattr(dashboard_mod, "health_payload", lambda: {"ok": True})


def test_authed_api_get_stamps_last_api_hit(monkeypatch):
    _arm_auth(monkeypatch)
    server, thread = _serve(dashboard_mod.Handler)
    try:
        status, _ = _get(
            server.server_address[1], "/api",
            headers={"Authorization": "Bearer dash-token"},
        )
        assert status == 200
        assert dashboard_mod._LAST_API_HIT > 0.0
    finally:
        _stop(server, thread)


def test_unauthed_api_get_does_not_stamp(monkeypatch):
    _arm_auth(monkeypatch)
    server, thread = _serve(dashboard_mod.Handler)
    try:
        status, _ = _get(server.server_address[1], "/api")
        assert status == 401
        assert dashboard_mod._LAST_API_HIT == 0.0
    finally:
        _stop(server, thread)


def test_health_get_does_not_stamp(monkeypatch):
    _arm_auth(monkeypatch)
    server, thread = _serve(dashboard_mod.Handler)
    try:
        status, _ = _get(server.server_address[1], "/health")
        assert status == 200
        assert dashboard_mod._LAST_API_HIT == 0.0
    finally:
        _stop(server, thread)
