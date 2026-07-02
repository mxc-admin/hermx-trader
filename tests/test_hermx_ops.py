"""Unit tests for the skills' shared helper module (``hermx_ops``).

Covers the money-safety invariants that the slash-command skills depend on:
  * UNKNOWN-never-flat on a degraded/unreadable executor read
  * close is reduce-only — the wire body NEVER carries a size
  * resume maps to the wire verb ``clear``
  * a transport 5xx yields an UNKNOWN outcome (order may or may not have gone out)
  * the safe control-state updater refuses a corrupt file and writes atomically
  * strategy resolution precedence (exact id wins; ambiguous symbol → candidates)

Stdlib-only, matching the module under test. HTTP is faked by monkeypatching
``urllib.request.urlopen``; files use pytest's ``tmp_path``.
"""

import io
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h  # noqa: E402


# --------------------------------------------------------------------------- #
# HTTP fakes                                                                   #
# --------------------------------------------------------------------------- #
class _FakeResp:
    """Minimal context-managed stand-in for an http.client response."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_urlopen(monkeypatch, payload=None, http_error=None, captured=None):
    """Patch ``urllib.request.urlopen``.

    ``payload`` may be a dict or a callable(req)->dict. ``http_error`` (if given)
    is raised instead. ``captured`` (a list) receives each request for assertions.
    """

    def fake(req, timeout=None):
        if captured is not None:
            captured.append(req)
        if http_error is not None:
            raise http_error
        body = payload(req) if callable(payload) else payload
        return _FakeResp(json.dumps({} if body is None else body).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError(
        "http://127.0.0.1/x", code, "err", {}, io.BytesIO(body)
    )


# --------------------------------------------------------------------------- #
# read_state / positions                                                      #
# --------------------------------------------------------------------------- #
def test_read_state_degraded_returns_unknown_positions(monkeypatch):
    def payload(req):
        url = req.full_url
        if url.endswith("/api"):
            return {
                "okx_live": {"ok": False, "positions": {"BTCUSDT": {"side": "long"}}},
                "executor": {"ok": True, "degraded": True},
            }
        # both /health endpoints
        return {"ok": True, "arm": {"armed": False}, "mode": "demo"}

    _install_urlopen(monkeypatch, payload=payload)
    st = h.read_state()
    assert st["positions"] == h.UNKNOWN
    assert st["positions_status"] == h.UNKNOWN


def test_read_position_for_symbol_unknown_when_state_unreadable():
    res = h.read_position_for_symbol({"positions": h.UNKNOWN}, "BTCUSDT")
    assert res["status"] == h.UNKNOWN


def test_read_position_for_symbol_flat_when_empty():
    res = h.read_position_for_symbol({"positions": {}}, "BTCUSDT")
    assert res["status"] == "FLAT"


# --------------------------------------------------------------------------- #
# post_close (reduce-only; UNKNOWN on 5xx; never sends a size)                 #
# --------------------------------------------------------------------------- #
def test_post_close_submitted_on_200_ok(monkeypatch):
    _install_urlopen(
        monkeypatch,
        payload={"ok": True, "mode": "submitted", "cl_ord_id": "test-123"},
    )
    r = h.post_close(h.RECEIVER_BASE, "secret", "BTCUSDT", "s1", "op", "reason")
    assert r["outcome"] == "submitted"
    assert r["cl_ord_id"] == "test-123"


def test_post_close_not_submitted_on_200_not_submitted(monkeypatch):
    _install_urlopen(
        monkeypatch,
        payload={"ok": True, "mode": "not_submitted", "reason": "symbol_paused"},
    )
    r = h.post_close(h.RECEIVER_BASE, "secret", "BTCUSDT", "s1", "op", "reason")
    assert r["outcome"] == "not_submitted"


def test_post_close_unknown_on_5xx(monkeypatch):
    _install_urlopen(monkeypatch, http_error=_http_error(500))
    r = h.post_close(h.RECEIVER_BASE, "secret", "BTCUSDT", "s1", "op", "reason")
    assert r["outcome"] == h.UNKNOWN


def test_post_close_body_has_no_size(monkeypatch):
    captured = []
    _install_urlopen(monkeypatch, payload={"ok": True, "mode": "submitted"},
                     captured=captured)
    h.post_close(h.RECEIVER_BASE, "secret", "BTCUSDT", "s1", "op", "reason")
    assert captured, "urlopen was not called"
    body = json.loads(captured[-1].data.decode("utf-8"))
    assert "size" not in body


# --------------------------------------------------------------------------- #
# post_strategy_mode (resume → clear)                                         #
# --------------------------------------------------------------------------- #
def test_post_strategy_mode_resume_maps_to_clear(monkeypatch):
    captured = []
    _install_urlopen(monkeypatch, payload={"ok": True}, captured=captured)
    r = h.post_strategy_mode(h.DASHBOARD_BASE, "secret", "s1", "resume")
    assert captured, "urlopen was not called"
    body = json.loads(captured[-1].data.decode("utf-8"))
    assert body["mode"] == "clear"
    assert r["wire_mode"] == "clear"


# --------------------------------------------------------------------------- #
# safe_update_control_state (refuse corrupt; atomic write)                     #
# --------------------------------------------------------------------------- #
def test_safe_update_control_state_refuses_corrupt_file(tmp_path):
    p = tmp_path / "control-state.json"
    original = b"{ this is not json ::::"
    p.write_bytes(original)

    def mut(state):
        state["symbol_pauses"] = {"BTCUSDT": {"paused": True}}
        return state

    res = h.safe_update_control_state(str(p), mut)
    assert res["ok"] is False
    assert p.read_bytes() == original  # never overwritten


def test_safe_update_control_state_atomic_write(tmp_path):
    p = tmp_path / "control-state.json"  # missing → fresh create

    def mut(state):
        state.setdefault("symbol_pauses", {})
        return state

    res = h.safe_update_control_state(str(p), mut)
    assert res["ok"] is True
    assert p.exists()
    written = json.loads(p.read_text())
    assert "updated_at" in written
    assert "symbol_pauses" in written


# --------------------------------------------------------------------------- #
# resolve_strategy (precedence)                                               #
# --------------------------------------------------------------------------- #
def _write_strategy(dir_path, filename, strategy_id, inst_id):
    (dir_path / filename).write_text(json.dumps({
        "strategy_id": strategy_id,
        "instrument": {"inst_id": inst_id},
        "execution_mode": "demo",
    }))


def test_resolve_strategy_exact_id_wins(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    _write_strategy(sdir, "a.json", "alpha-1", "BTC-USDT-SWAP")
    _write_strategy(sdir, "b.json", "beta-1", "ETH-USDT-SWAP")

    res = h.resolve_strategy("alpha-1", str(sdir))
    assert res["resolved"] == "alpha-1"
    assert res["match"] == "id"
    assert res["ambiguous"] is False


def test_resolve_strategy_ambiguous_symbol_returns_candidates(tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    _write_strategy(sdir, "a.json", "alpha-1", "BTC-USDT-SWAP")
    _write_strategy(sdir, "b.json", "beta-1", "BTC-USDT-SWAP")

    res = h.resolve_strategy("BTCUSDT", str(sdir))
    assert res["resolved"] is None
    assert res["ambiguous"] is True
    assert set(res["candidates"]) == {"alpha-1", "beta-1"}
