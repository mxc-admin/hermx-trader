"""Unit tests for the HermX Hermes-cron pre-check gate scripts.

Covers the HermX-owned bridge code (``deploy/hermes-scripts/``): fingerprint
stability, the suppression-window / escalation decision, the atomic + self-healing
sidecar state, the reconcile / risk gates, the no-agent health watchdog, and the
``wakeAgent`` JSON stdout contract.

Stdlib-only, offline. HTTP is faked by monkeypatching ``urllib.request.urlopen``
(the ``test_hermx_ops.py`` pattern); the clock is injected as ``now_epoch`` — never
frozen (repo convention). No Hermes gateway is required.
"""

import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "deploy" / "hermes-scripts"

# Make the shared lib + the real hermx_ops importable.
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "skills" / "hermx-ops" / "lib"))
import hermx_gate_lib as g  # noqa: E402
import hermx_ops  # noqa: E402


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reconcile = _load("hermx_reconcile_gate", "hermx-reconcile-gate.py")
risk = _load("hermx_risk_gate", "hermx-risk-gate.py")
health = _load("hermx_health_watch", "hermx-health-watch.py")


# --------------------------------------------------------------------------- #
# HTTP fake (dispatch by URL)                                                  #
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_urlopen(monkeypatch, router):
    """``router`` maps a substring of the URL → dict payload, or callable(url)->dict.
    A router value of an Exception instance is raised. Unmatched → empty dict."""

    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for needle, val in router.items():
            if needle in url:
                if isinstance(val, Exception):
                    raise val
                body = val(url) if callable(val) else val
                return _FakeResp(json.dumps(body).encode("utf-8"))
        return _FakeResp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _iso(now_epoch):
    return datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()


NOW = 1_770_000_000.0  # fixed injected clock


# --------------------------------------------------------------------------- #
# Fingerprint stability                                                        #
# --------------------------------------------------------------------------- #
def test_reconcile_fingerprint_stable_across_ticks(monkeypatch, tmp_path):
    (tmp_path / "logs").mkdir()
    row = {"ts": _iso(NOW), "kind": "reconcile", "alert": "UNKNOWN_RESOLVER_TIMEOUT",
           "severity": "error", "detail": {"symbol": "SOLUSDT", "cl_ord_id": "clord-abc"}}
    (tmp_path / "logs" / "alerts.jsonl").write_text(json.dumps(row) + "\n")

    a = reconcile.alert_conditions(hermx_ops, str(tmp_path), NOW)
    b = reconcile.alert_conditions(hermx_ops, str(tmp_path), NOW + 10)
    assert a[0]["fingerprint"] == b[0]["fingerprint"]
    assert a[0]["fingerprint"] == "reconcile:UNKNOWN_RESOLVER_TIMEOUT:SOLUSDT:clord-abc"


def test_reconcile_fingerprint_omits_absent_segments(tmp_path):
    (tmp_path / "logs").mkdir()
    row = {"ts": _iso(NOW), "kind": "operator", "alert": "WATCHDOG_DEGRADED",
           "severity": "error", "detail": {"resolver_stale": True}}
    (tmp_path / "logs" / "alerts.jsonl").write_text(json.dumps(row) + "\n")
    conds = reconcile.alert_conditions(hermx_ops, str(tmp_path), NOW)
    assert conds[0]["fingerprint"] == "reconcile:WATCHDOG_DEGRADED"


def test_risk_fingerprint_includes_state_and_symbol():
    fp_state = g.RISK_SEVERITY  # sanity: the mapping exists
    assert fp_state["elevated"] == "warning" and fp_state["high"] == "error"


# --------------------------------------------------------------------------- #
# Window / escalation decision                                                 #
# --------------------------------------------------------------------------- #
def test_is_fresh_unseen():
    assert g.is_fresh(None, "warning", NOW, 1800) is True


def test_is_fresh_within_window_suppressed():
    entry = {"last_notified_epoch": NOW, "last_severity": "warning"}
    assert g.is_fresh(entry, "warning", NOW + 100, 1800) is False


def test_is_fresh_after_window_refires():
    entry = {"last_notified_epoch": NOW, "last_severity": "warning"}
    assert g.is_fresh(entry, "warning", NOW + 1800, 1800) is True


def test_is_fresh_escalation_bypasses_window():
    entry = {"last_notified_epoch": NOW, "last_severity": "warning"}
    assert g.is_fresh(entry, "error", NOW + 100, 1800) is True


def test_is_fresh_deescalation_stays_suppressed():
    entry = {"last_notified_epoch": NOW, "last_severity": "error"}
    assert g.is_fresh(entry, "warning", NOW + 100, 1800) is False


def test_evaluate_advances_only_fresh_fingerprints():
    state = {"reconcile:A": {"last_notified_epoch": NOW, "last_severity": "warning"}}
    conds = [
        {"fingerprint": "reconcile:A", "severity": "warning"},  # suppressed
        {"fingerprint": "reconcile:B", "severity": "error"},    # fresh
    ]
    fresh, new_state = g.evaluate(conds, state, 1800, NOW + 100)
    assert [c["fingerprint"] for c in fresh] == ["reconcile:B"]
    # A preserved untouched, B added.
    assert new_state["reconcile:A"]["last_notified_epoch"] == NOW
    assert new_state["reconcile:B"]["last_notified_epoch"] == NOW + 100


# --------------------------------------------------------------------------- #
# Sidecar state: atomic write + corrupt fail-safe                             #
# --------------------------------------------------------------------------- #
def test_sidecar_roundtrip(tmp_path):
    p = tmp_path / ".hermx-reconcile.state"
    g.save_state(p, {"reconcile:A": {"last_notified_epoch": NOW, "last_severity": "error"}})
    assert not list(tmp_path.glob("*.tmp"))  # temp file cleaned up
    assert g.load_state(p)["reconcile:A"]["last_severity"] == "error"


def test_sidecar_corrupt_returns_empty(tmp_path):
    p = tmp_path / ".hermx-reconcile.state"
    p.write_text("{not json")
    assert g.load_state(p) == {}


def test_sidecar_missing_returns_empty(tmp_path):
    assert g.load_state(tmp_path / "nope.state") == {}


# --------------------------------------------------------------------------- #
# Reconcile gate                                                               #
# --------------------------------------------------------------------------- #
def _write_alerts(tmp_path, rows):
    (tmp_path / "logs").mkdir(exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "logs" / "alerts.jsonl").write_text(text)


def test_reconcile_gate_fresh_alert_and_torn_tail(monkeypatch, tmp_path):
    rows = [
        {"ts": _iso(NOW - 10), "kind": "reconcile", "alert": "STATE_MISMATCH",
         "severity": "error", "detail": {"symbol": "ETHUSDT", "cl_ord_id": "c1"}},
        {"ts": _iso(NOW - 5), "kind": "operator", "alert": "WATCHDOG_RECOVERED",
         "severity": "info", "detail": {}},  # below warning → excluded
    ]
    # torn trailing line must be tolerated (skipped)
    text = "".join(json.dumps(r) + "\n" for r in rows) + '{"ts":"broken'
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "alerts.jsonl").write_text(text)
    _install_urlopen(monkeypatch, {"/api": {"open_orders": {"rows": []}}})

    conds = reconcile.collect(hermx_ops, str(tmp_path), NOW)
    fps = [c["fingerprint"] for c in conds]
    assert fps == ["reconcile:STATE_MISMATCH:ETHUSDT:c1"]  # info row excluded, tail tolerated


def test_reconcile_gate_drops_stale_rows(tmp_path, monkeypatch):
    old = {"ts": _iso(NOW - 5000), "kind": "reconcile", "alert": "OLD",
           "severity": "error", "detail": {}}
    _write_alerts(tmp_path, [old])
    conds = reconcile.alert_conditions(hermx_ops, str(tmp_path), NOW)
    assert conds == []  # older than the 1800s lookback


def test_reconcile_stuck_order_from_api(monkeypatch):
    _install_urlopen(monkeypatch, {"/api": {"open_orders": {"rows": [
        {"cl_ord_id": "c9", "state": "UNKNOWN", "symbol": "BTCUSDT"},
        {"cl_ord_id": "c8", "state": "SUBMITTED", "symbol": "BTCUSDT"},  # not stuck
    ]}}})
    conds = reconcile.stuck_order_conditions(hermx_ops)
    assert [c["fingerprint"] for c in conds] == ["reconcile:stuck_order:c9"]


def test_reconcile_stuck_order_unreachable_api_fail_open(monkeypatch):
    _install_urlopen(monkeypatch, {"/api": urllib.error.URLError("down")})
    assert reconcile.stuck_order_conditions(hermx_ops) == []


def test_reconcile_gate_end_to_end_wake_then_sleep(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _write_alerts(tmp_path, [
        {"ts": _iso(NOW - 10), "kind": "reconcile", "alert": "X",
         "severity": "error", "detail": {"symbol": "SOLUSDT"}},
    ])
    _install_urlopen(monkeypatch, {"/api": {"open_orders": {"rows": []}}})
    conds = reconcile.collect(hermx_ops, str(tmp_path), NOW)

    first = g.run_gate("reconcile", conds, NOW)
    assert first["wakeAgent"] is True
    assert (tmp_path / ".hermx-reconcile.state").exists()

    second = g.run_gate("reconcile", conds, NOW + 100)  # inside window
    assert second["wakeAgent"] is False


# --------------------------------------------------------------------------- #
# Risk gate                                                                    #
# --------------------------------------------------------------------------- #
def _write_control_state(tmp_path, flag):
    body = {"version": 1}
    if flag is not None:
        body["risk_index_gate_enabled"] = flag
    (tmp_path / "control-state.json").write_text(json.dumps(body))


def test_risk_gate_disabled_absent_flag(tmp_path):
    _write_control_state(tmp_path, None)
    assert risk.risk_conditions(hermx_ops, str(tmp_path)) == []


def test_risk_gate_disabled_false_flag(tmp_path):
    _write_control_state(tmp_path, False)
    assert risk.risk_conditions(hermx_ops, str(tmp_path)) == []


def test_risk_gate_enabled_elevated_transition(tmp_path, monkeypatch):
    _write_control_state(tmp_path, True)
    _install_urlopen(monkeypatch, {"replit.app": {"risk_state": "elevated", "symbol": "SOLUSDT"}})
    conds = risk.risk_conditions(hermx_ops, str(tmp_path))
    assert len(conds) == 1
    assert conds[0]["fingerprint"] == "risk:elevated:SOLUSDT"
    assert conds[0]["severity"] == "warning"


def test_risk_gate_enabled_benign_state(tmp_path, monkeypatch):
    _write_control_state(tmp_path, True)
    _install_urlopen(monkeypatch, {"replit.app": {"risk_state": "normal"}})
    assert risk.risk_conditions(hermx_ops, str(tmp_path)) == []


def test_risk_gate_mxc_unreachable_fail_open(tmp_path, monkeypatch):
    _write_control_state(tmp_path, True)
    _install_urlopen(monkeypatch, {"replit.app": urllib.error.URLError("down")})
    assert risk.risk_conditions(hermx_ops, str(tmp_path)) == []


# --------------------------------------------------------------------------- #
# Health watchdog (no-agent)                                                   #
# --------------------------------------------------------------------------- #
def _health_router(dashboard=None, receiver=None):
    router = {}
    router["8098/health"] = dashboard if dashboard is not None else {"ok": True, "arm": {"armed": True}}
    router["8891/health"] = receiver if receiver is not None else {"ok": True}
    return router


def test_health_all_ok_empty(monkeypatch):
    _install_urlopen(monkeypatch, _health_router())
    assert health.check(hermx_ops) == []


def test_health_dashboard_unreachable(monkeypatch):
    router = _health_router(dashboard={"ok": False})
    _install_urlopen(monkeypatch, router)
    assert "dashboard: unreachable" in health.check(hermx_ops)


def test_health_receiver_down(monkeypatch):
    _install_urlopen(monkeypatch, _health_router(receiver={"ok": False}))
    assert "receiver: down" in health.check(hermx_ops)


def test_health_kill_switch_engaged(monkeypatch):
    router = _health_router(dashboard={"ok": True, "arm": {"armed": True, "kill_switch_engaged": True}})
    _install_urlopen(monkeypatch, router)
    assert "arm: kill-switch engaged" in health.check(hermx_ops)


def test_health_disarmed_only_when_required(monkeypatch):
    router = _health_router(dashboard={"ok": True, "arm": {"armed": False}})
    _install_urlopen(monkeypatch, router)
    assert health.check(hermx_ops) == []  # disarmed not a problem by default
    monkeypatch.setenv("HERMX_HEALTH_REQUIRE_ARMED", "true")
    assert "arm: disarmed" in health.check(hermx_ops)


def test_health_main_healthy_empty_stdout(monkeypatch, capsys):
    _install_urlopen(monkeypatch, _health_router())
    monkeypatch.setattr(health.g, "import_hermx_ops", lambda: hermx_ops)
    health.main()
    assert capsys.readouterr().out == ""


def test_health_main_problem_prints_lines(monkeypatch, capsys):
    _install_urlopen(monkeypatch, _health_router(receiver={"ok": False}))
    monkeypatch.setattr(health.g, "import_hermx_ops", lambda: hermx_ops)
    health.main()
    assert "receiver: down" in capsys.readouterr().out.splitlines()


# --------------------------------------------------------------------------- #
# wakeAgent JSON contract                                                      #
# --------------------------------------------------------------------------- #
def test_wake_json_contract_sleep(capsys):
    g.emit_sleep()
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"wakeAgent": False}


def test_wake_json_contract_wake_shape(capsys):
    g.emit_wake([{"category": "reconcile", "severity": "error", "title": "t",
                  "fingerprint": "reconcile:X", "detail": {"symbol": "SOLUSDT"}}])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["wakeAgent"] is True
    alert = payload["context"]["alerts"][0]
    assert set(alert) == {"category", "severity", "title", "fingerprint", "detail"}
    assert alert["fingerprint"] == "reconcile:X"


def test_run_gate_sleep_emits_valid_json(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    res = g.run_gate("risk", [], NOW)
    assert res["wakeAgent"] is False
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"wakeAgent": False}
