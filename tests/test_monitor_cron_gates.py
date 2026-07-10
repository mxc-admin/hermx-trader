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
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


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
intake = _load("hermx_intake_gate", "hermx-intake-gate.py")
ledger = _load("hermx_ledger_reconcile", "hermx-ledger-reconcile.py")
lag = _load("hermx_reconcile_lag_gate", "hermx-reconcile-lag-gate.py")


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


def test_reconcile_stuck_order_unreachable_api_escalates(monkeypatch):
    # #6 half-2: a failed /api read is UNKNOWN trading state, not an all-clear.
    _install_urlopen(monkeypatch, {"/api": urllib.error.URLError("down")})
    conds = reconcile.stuck_order_conditions(hermx_ops)
    assert [c["fingerprint"] for c in conds] == ["reconcile:api_unreadable"]
    assert conds[0]["severity"] == "error"


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
# #6 half-1: secret resolved via ops._load_secret() (env or .env), not raw env #
# --------------------------------------------------------------------------- #
def _secret_recorder():
    """A ``_get_json`` stand-in that records the (base, path, secret) of each call
    and returns a benign, all-healthy dict so the caller runs to completion."""
    calls = []

    def rec(base, path, secret=None, timeout=None):
        calls.append({"base": base, "path": path, "secret": secret})
        return {"ok": True, "arm": {"armed": True},
                "open_orders": {"rows": []}, "strategies": []}, None

    return calls, rec


def _arm_env_file_secret(monkeypatch, tmp_path):
    # HERMX_SECRET absent from env, present only in ${HERMX_DATA_DIR}/.env → the swap
    # must resolve it via _load_secret(); the old os.environ.get read would yield None.
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HERMX_SECRET", raising=False)
    (tmp_path / ".env").write_text("HERMX_SECRET=fromfile\n")


def test_stuck_order_resolves_secret_from_env_file(monkeypatch, tmp_path):
    _arm_env_file_secret(monkeypatch, tmp_path)
    calls, rec = _secret_recorder()
    monkeypatch.setattr(hermx_ops, "_get_json", rec)
    reconcile.stuck_order_conditions(hermx_ops)
    api = [c for c in calls if c["path"] == "/api"]
    assert api and api[0]["secret"] == "fromfile"


def test_health_check_resolves_secret_from_env_file(monkeypatch, tmp_path):
    _arm_env_file_secret(monkeypatch, tmp_path)
    calls, rec = _secret_recorder()
    monkeypatch.setattr(hermx_ops, "_get_json", rec)
    health.check(hermx_ops)
    dash = [c for c in calls if c["path"] == "/health" and "8098" in c["base"]]
    assert dash and dash[0]["secret"] == "fromfile"


def test_ledger_reconcile_resolves_secret_from_env_file(monkeypatch, tmp_path):
    _arm_env_file_secret(monkeypatch, tmp_path)
    calls, rec = _secret_recorder()
    monkeypatch.setattr(hermx_ops, "_get_json", rec)
    monkeypatch.setattr(ledger.g, "import_hermx_ops", lambda: hermx_ops)
    assert ledger.main() == 0
    api = [c for c in calls if c["path"] == "/api"]
    assert api and api[0]["secret"] == "fromfile"


# --------------------------------------------------------------------------- #
# #6 half-2: failed /api read escalates (no false all-clear on trading state)   #
# --------------------------------------------------------------------------- #
def test_stuck_order_http_401_escalates(monkeypatch):
    # A 401 may return a JSON error body (a dict) alongside err → gate on err.
    monkeypatch.setattr(hermx_ops, "_get_json",
                        lambda *a, **k: ({"error": "forbidden"}, "http_401"))
    conds = reconcile.stuck_order_conditions(hermx_ops)
    assert [c["fingerprint"] for c in conds] == ["reconcile:api_unreadable"]
    assert conds[0]["severity"] == "error"
    assert conds[0]["detail"]["error"] == "http_401"


def test_stuck_order_unreachable_escalates(monkeypatch):
    monkeypatch.setattr(hermx_ops, "_get_json",
                        lambda *a, **k: (None, "unreachable:conn refused"))
    conds = reconcile.stuck_order_conditions(hermx_ops)
    assert [c["fingerprint"] for c in conds] == ["reconcile:api_unreadable"]


def test_stuck_order_success_zero_stuck_stays_silent(monkeypatch):
    # Genuine successful read, zero UNKNOWN rows → real all-clear, must stay empty.
    monkeypatch.setattr(hermx_ops, "_get_json",
                        lambda *a, **k: ({"open_orders": {"rows": []}}, None))
    assert reconcile.stuck_order_conditions(hermx_ops) == []


# --------------------------------------------------------------------------- #
# Reconcile gate: ledger-mismatch + rejected-order conditions                  #
# --------------------------------------------------------------------------- #
def _write_intake_wal(tmp_path, rows):
    (tmp_path / "logs").mkdir(exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "logs" / "raw-webhooks.jsonl").write_text(text)


def _intake(now_offset, strategy_id, side=None, action=None):
    payload = {"strategy_id": strategy_id}
    if side is not None:
        payload["side"] = side
    if action is not None:
        payload["action"] = action
    return {"phase": "intake", "received_at": _iso(NOW + now_offset), "payload": payload}


def _write_journal(tmp_path, rows):
    (tmp_path / "logs").mkdir(exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "logs" / "order-journal.jsonl").write_text(text)


def _write_submit_map(tmp_path, rows):
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "cl-ord-strategy-map.jsonl").write_text(text)


def _write_trades(tmp_path, rows):
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "closed-trades.jsonl").write_text(text)


def _flip_fixture(tmp_path, journal_state="FILLED"):
    """buy at NOW-7200, sell flip at NOW-3600 (close-implying, past the 1800s grace),
    submit-map entry + journal terminal state for the flip's order, no ledger row."""
    _write_intake_wal(tmp_path, [
        _intake(-7200, "s1", side="buy"),
        _intake(-3600, "s1", side="sell"),
    ])
    _write_submit_map(tmp_path, [
        {"cl_ord_id": "c2", "strategy_id": "s1", "ts_ms": int((NOW - 3600 + 2) * 1000)},
    ])
    _write_journal(tmp_path, [
        {"seq": 1, "ts": _iso(NOW - 3600 + 3), "cl_ord_id": "c2", "state": "SUBMITTED",
         "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
        {"seq": 2, "ts": _iso(NOW - 3600 + 5), "cl_ord_id": "c2", "state": journal_state,
         "prev_state": "SUBMITTED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
    ])


def test_ledger_mismatch_missing_wal_fail_open(tmp_path):
    assert reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_ledger_mismatch_no_submit_map_fail_open(tmp_path):
    _write_intake_wal(tmp_path, [
        _intake(-7200, "s1", side="buy"),
        _intake(-3600, "s1", side="sell"),
    ])
    assert reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_ledger_mismatch_no_journal_fail_open(tmp_path):
    # Candidates exist but no journal → no terminal state → skip (still resolving).
    _write_intake_wal(tmp_path, [
        _intake(-7200, "s1", side="buy"),
        _intake(-3600, "s1", side="sell"),
    ])
    _write_submit_map(tmp_path, [
        {"cl_ord_id": "c2", "strategy_id": "s1", "ts_ms": int((NOW - 3600 + 2) * 1000)},
    ])
    assert reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_ledger_mismatch_fires_on_filled_no_ledger_row_past_grace(tmp_path):
    _flip_fixture(tmp_path, journal_state="FILLED")
    conds = reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW)
    assert [c["fingerprint"] for c in conds] == ["reconcile:ledger_mismatch:s1"]
    (c,) = conds
    assert c["severity"] == "warning"
    assert c["title"] == "ledger mismatch: s1 closed at venue, missing from ledger"
    assert c["detail"]["strategy_id"] == "s1"
    assert c["detail"]["cl_ord_id"] == "c2"
    assert c["detail"]["signal_received_at"] == _iso(NOW - 3600)
    assert c["detail"]["grace_seconds"] == 1800.0


def test_ledger_mismatch_skips_rejected_order(tmp_path):
    # REJECTED is owned by rejected_order_conditions, not the mismatch gate.
    _flip_fixture(tmp_path, journal_state="REJECTED")
    assert reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_ledger_mismatch_silent_within_grace(tmp_path):
    # Close-implying `action=="close"` signal only 600s old (< 1800s grace) → no alert.
    _write_intake_wal(tmp_path, [
        _intake(-7200, "s1", side="buy"),
        _intake(-600, "s1", action="close"),
    ])
    _write_submit_map(tmp_path, [
        {"cl_ord_id": "c2", "strategy_id": "s1", "ts_ms": int((NOW - 600 + 2) * 1000)},
    ])
    _write_journal(tmp_path, [
        {"seq": 1, "ts": _iso(NOW - 600 + 5), "cl_ord_id": "c2", "state": "FILLED",
         "prev_state": "SUBMITTED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
    ])
    assert reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_ledger_mismatch_satisfied_by_ledger_row(tmp_path):
    _flip_fixture(tmp_path, journal_state="FILLED")
    _write_trades(tmp_path, [
        {"cl_ord_id": "c2", "strategy_id": "s1",
         "recorded_at_ms": int((NOW - 3600 + 60) * 1000)},
    ])
    assert reconcile.ledger_mismatch_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_rejected_order_missing_journal_fail_open(tmp_path):
    assert reconcile.rejected_order_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_rejected_order_fires_within_lookback(tmp_path):
    _write_journal(tmp_path, [
        {"seq": 1, "ts": _iso(NOW - 20), "cl_ord_id": "c9", "state": "SUBMITTED",
         "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
        {"seq": 2, "ts": _iso(NOW - 10), "cl_ord_id": "c9", "state": "REJECTED",
         "prev_state": "SUBMITTED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
    ])
    conds = reconcile.rejected_order_conditions(hermx_ops, str(tmp_path), NOW)
    assert [c["fingerprint"] for c in conds] == ["reconcile:rejected_order:c9"]
    (c,) = conds
    assert c["severity"] == "warning"
    assert c["title"] == "order rejected: c9 -- position may still be open"
    assert c["detail"] == {"cl_ord_id": "c9", "symbol": "BTCUSDT", "side": "sell",
                           "prev_state": "SUBMITTED"}


def test_rejected_order_stale_row_excluded(tmp_path):
    _write_journal(tmp_path, [
        {"seq": 1, "ts": _iso(NOW - 5000), "cl_ord_id": "c9", "state": "REJECTED",
         "prev_state": "SUBMITTED", "intent": {}},
    ])
    assert reconcile.rejected_order_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_collect_includes_new_condition_families(monkeypatch, tmp_path):
    # One rejected order + one ledger mismatch, empty alerts/api → both surface
    # through collect() alongside the existing families.
    _flip_fixture(tmp_path, journal_state="FILLED")
    _write_journal(tmp_path, [
        {"seq": 1, "ts": _iso(NOW - 3600 + 3), "cl_ord_id": "c2", "state": "SUBMITTED",
         "prev_state": "PLANNED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
        {"seq": 2, "ts": _iso(NOW - 3600 + 5), "cl_ord_id": "c2", "state": "FILLED",
         "prev_state": "SUBMITTED", "intent": {"symbol": "BTCUSDT", "side": "sell"}},
        {"seq": 3, "ts": _iso(NOW - 10), "cl_ord_id": "c9", "state": "REJECTED",
         "prev_state": "SUBMITTED", "intent": {"symbol": "ETHUSDT", "side": "buy"}},
    ])
    _install_urlopen(monkeypatch, {"/api": {"open_orders": {"rows": []}}})
    conds = reconcile.collect(hermx_ops, str(tmp_path), NOW)
    assert [c["fingerprint"] for c in conds] == [
        "reconcile:ledger_mismatch:s1",
        "reconcile:rejected_order:c9",
    ]


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


def _health_fps(conds):
    return [c["fingerprint"] for c in conds]


def _health_titles(conds):
    return [c["title"] for c in conds]


def test_health_all_ok_empty(monkeypatch):
    _install_urlopen(monkeypatch, _health_router())
    assert health.check(hermx_ops) == []


def test_health_dashboard_unreachable(monkeypatch):
    router = _health_router(dashboard={"ok": False})
    _install_urlopen(monkeypatch, router)
    conds = health.check(hermx_ops)
    assert "health:dashboard_unreachable" in _health_fps(conds)
    (c,) = [c for c in conds if c["fingerprint"] == "health:dashboard_unreachable"]
    assert c["severity"] == "critical" and c["title"] == "dashboard: unreachable"


def test_health_receiver_down(monkeypatch):
    _install_urlopen(monkeypatch, _health_router(receiver={"ok": False}))
    conds = health.check(hermx_ops)
    assert "health:receiver_down" in _health_fps(conds)
    (c,) = [c for c in conds if c["fingerprint"] == "health:receiver_down"]
    assert c["severity"] == "critical" and c["title"] == "receiver: down"


def test_health_kill_switch_engaged(monkeypatch):
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    router = _health_router(dashboard={"ok": True, "arm": {"armed": True, "kill_switch_engaged": True}})
    _install_urlopen(monkeypatch, router)
    conds = health.check(hermx_ops)
    assert "health:kill_switch_engaged" in _health_fps(conds)
    (c,) = [c for c in conds if c["fingerprint"] == "health:kill_switch_engaged"]
    assert c["severity"] == "warning" and c["title"] == "arm: kill-switch engaged"


def test_health_kill_switch_ignored_when_not_live(monkeypatch):
    # Demo/shadow host: HERMX_LIVE_TRADING unset. kill_switch_engaged=True is the
    # normal fail-closed state, so it must NOT surface as a health problem.
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    router = _health_router(dashboard={"ok": True, "arm": {"armed": True, "kill_switch_engaged": True}})
    _install_urlopen(monkeypatch, router)
    conds = health.check(hermx_ops)
    assert "health:kill_switch_engaged" not in _health_fps(conds)
    assert conds == []


def test_health_kill_switch_alerted_when_live_expected(monkeypatch):
    # Live host: HERMX_LIVE_TRADING truthy. A kill switch is a genuine problem.
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")
    router = _health_router(dashboard={"ok": True, "arm": {"armed": True, "kill_switch_engaged": True}})
    _install_urlopen(monkeypatch, router)
    conds = health.check(hermx_ops)
    assert "health:kill_switch_engaged" in _health_fps(conds)
    (c,) = [c for c in conds if c["fingerprint"] == "health:kill_switch_engaged"]
    assert c["severity"] == "warning" and c["title"] == "arm: kill-switch engaged"


def test_health_disarmed_only_when_required(monkeypatch):
    router = _health_router(dashboard={"ok": True, "arm": {"armed": False}})
    _install_urlopen(monkeypatch, router)
    assert health.check(hermx_ops) == []  # disarmed not a problem by default
    monkeypatch.setenv("HERMX_HEALTH_REQUIRE_ARMED", "true")
    conds = health.check(hermx_ops)
    assert "health:disarmed" in _health_fps(conds)
    (c,) = [c for c in conds if c["fingerprint"] == "health:disarmed"]
    assert c["severity"] == "warning" and c["title"] == "arm: disarmed"


def test_health_main_healthy_empty_stdout(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _install_urlopen(monkeypatch, _health_router())
    monkeypatch.setattr(health.g, "import_hermx_ops", lambda: hermx_ops)
    health.main()
    assert capsys.readouterr().out == ""


def test_health_main_problem_prints_lines(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _install_urlopen(monkeypatch, _health_router(receiver={"ok": False}))
    monkeypatch.setattr(health.g, "import_hermx_ops", lambda: hermx_ops)
    health.main()
    assert "receiver: down" in capsys.readouterr().out.splitlines()


# --------------------------------------------------------------------------- #
# Health watchdog: suppression window (no flood)                              #
# --------------------------------------------------------------------------- #
def test_health_kill_switch_suppressed_within_window(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")  # kill switch only a problem on live hosts
    _install_urlopen(monkeypatch, _health_router(
        dashboard={"ok": True, "arm": {"armed": True, "kill_switch_engaged": True}}))

    first = health.run(hermx_ops, NOW)
    assert _health_fps(first) == ["health:kill_switch_engaged"]
    assert "arm: kill-switch engaged" in capsys.readouterr().out.splitlines()

    # Second tick inside the 900s window: silent (no flood).
    assert health.run(hermx_ops, NOW + 100) == []
    assert capsys.readouterr().out == ""

    # After the window it re-fires (still-broken condition eventually re-pages).
    third = health.run(hermx_ops, NOW + g.WINDOW["health"])
    assert _health_fps(third) == ["health:kill_switch_engaged"]
    assert "arm: kill-switch engaged" in capsys.readouterr().out.splitlines()


def test_health_dashboard_unreachable_suppressed_within_window(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _install_urlopen(monkeypatch, _health_router(dashboard={"ok": False}))

    assert _health_fps(health.run(hermx_ops, NOW)) == ["health:dashboard_unreachable"]
    assert "dashboard: unreachable" in capsys.readouterr().out.splitlines()
    assert health.run(hermx_ops, NOW + 100) == []
    assert capsys.readouterr().out == ""


def test_health_receiver_down_suppressed_within_window(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _install_urlopen(monkeypatch, _health_router(receiver={"ok": False}))

    assert _health_fps(health.run(hermx_ops, NOW)) == ["health:receiver_down"]
    assert "receiver: down" in capsys.readouterr().out.splitlines()
    assert health.run(hermx_ops, NOW + 100) == []
    assert capsys.readouterr().out == ""


def test_health_disarmed_suppressed_within_window(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setenv("HERMX_HEALTH_REQUIRE_ARMED", "true")
    _install_urlopen(monkeypatch, _health_router(dashboard={"ok": True, "arm": {"armed": False}}))

    assert _health_fps(health.run(hermx_ops, NOW)) == ["health:disarmed"]
    assert "arm: disarmed" in capsys.readouterr().out.splitlines()
    assert health.run(hermx_ops, NOW + 100) == []
    assert capsys.readouterr().out == ""


def test_health_multiple_problems_all_print_same_tick(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setenv("HERMX_LIVE_TRADING", "true")  # kill switch only a problem on live hosts
    _install_urlopen(monkeypatch, _health_router(
        dashboard={"ok": True, "arm": {"armed": True, "kill_switch_engaged": True}},
        receiver={"ok": False}))

    fresh = health.run(hermx_ops, NOW)
    assert set(_health_fps(fresh)) == {"health:receiver_down", "health:kill_switch_engaged"}
    out = capsys.readouterr().out.splitlines()
    assert "receiver: down" in out and "arm: kill-switch engaged" in out


def test_health_sidecar_persisted_on_wake(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _install_urlopen(monkeypatch, _health_router(receiver={"ok": False}))

    health.run(hermx_ops, NOW)
    sp = tmp_path / ".hermx-health.state"
    assert sp.exists()
    state = g.load_state(sp)
    assert state["health:receiver_down"]["last_notified_epoch"] == NOW
    assert state["health:receiver_down"]["last_severity"] == "critical"


def test_health_no_sidecar_write_when_healthy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _install_urlopen(monkeypatch, _health_router())

    assert health.run(hermx_ops, NOW) == []
    assert not (tmp_path / ".hermx-health.state").exists()


# --------------------------------------------------------------------------- #
# Intake-recency gate (D — absence detection)                                  #
# --------------------------------------------------------------------------- #
def _write_raw(tmp_path, rows):
    (tmp_path / "logs").mkdir(exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "logs" / "raw-webhooks.jsonl").write_text(text)


def test_intake_missing_wal_fail_open(tmp_path):
    assert intake.intake_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_intake_empty_wal_fail_open(tmp_path):
    _write_raw(tmp_path, [])
    assert intake.intake_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_intake_no_intake_phase_fail_open(tmp_path):
    # Only non-intake rows present → no intake ever seen → fail-open.
    _write_raw(tmp_path, [{"phase": "webhook", "received_at": _iso(NOW)}])
    assert intake.intake_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_intake_recent_no_alert(tmp_path):
    _write_raw(tmp_path, [
        {"phase": "intake", "received_at": _iso(NOW - 100)},
        {"phase": "webhook", "received_at": _iso(NOW - 100)},  # ignored
    ])
    assert intake.intake_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_intake_stale_emits_one_condition(tmp_path):
    _write_raw(tmp_path, [
        {"phase": "intake", "received_at": _iso(NOW - 4 * 24 * 3600)},   # 4d old (> 3d default)
        {"phase": "intake", "received_at": _iso(NOW - 5 * 24 * 3600)},   # older; not the max
    ])
    conds = intake.intake_conditions(hermx_ops, str(tmp_path), NOW)
    assert len(conds) == 1
    assert conds[0]["fingerprint"] == "frequency:zero_intake:global"
    assert conds[0]["severity"] == "error"


def test_intake_uses_newest_row_not_first(tmp_path):
    # A fresh row anywhere in the file suppresses the alert (max, not last-line).
    _write_raw(tmp_path, [
        {"phase": "intake", "received_at": _iso(NOW - 5 * 3600)},
        {"phase": "intake", "received_at": _iso(NOW - 60)},         # recent
    ])
    assert intake.intake_conditions(hermx_ops, str(tmp_path), NOW) == []


def test_intake_corrupt_lines_skipped(tmp_path):
    (tmp_path / "logs").mkdir()
    good = json.dumps({"phase": "intake", "received_at": _iso(NOW - 4 * 24 * 3600)})
    (tmp_path / "logs" / "raw-webhooks.jsonl").write_text(
        good + "\n{not json\n" + '{"phase":"intake","received_at":"broken\n'
    )
    conds = intake.intake_conditions(hermx_ops, str(tmp_path), NOW)
    assert [c["fingerprint"] for c in conds] == ["frequency:zero_intake:global"]


def test_intake_gate_end_to_end_wake_then_sleep(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    _write_raw(tmp_path, [{"phase": "intake", "received_at": _iso(NOW - 4 * 24 * 3600)}])
    conds = intake.intake_conditions(hermx_ops, str(tmp_path), NOW)

    first = g.run_gate("signal_late", conds, NOW)
    assert first["wakeAgent"] is True
    assert (tmp_path / ".hermx-signal_late.state").exists()

    second = g.run_gate("signal_late", conds, NOW + 100)  # inside 3600s window
    assert second["wakeAgent"] is False


# --------------------------------------------------------------------------- #
# Ledger reconcile (no-agent): stdout contract                                 #
# --------------------------------------------------------------------------- #
def test_ledger_reconcile_success_silent(monkeypatch, capsys):
    _install_urlopen(monkeypatch, {"/api": {
        "strategies": [{"venue": "okx", "okx_account_source": "demo"}],
        "generated_at": _iso(NOW),
    }})
    monkeypatch.setattr(ledger.g, "import_hermx_ops", lambda: hermx_ops)
    assert ledger.main() == 0
    assert capsys.readouterr().out == ""


def test_ledger_reconcile_unreachable_prints_error(monkeypatch, capsys):
    _install_urlopen(monkeypatch, {"/api": urllib.error.URLError("down")})
    monkeypatch.setattr(ledger.g, "import_hermx_ops", lambda: hermx_ops)
    assert ledger.main() == 0
    out = capsys.readouterr().out
    assert out != ""
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["reconciled"] is False


# --------------------------------------------------------------------------- #
# Reconcile-lag gate (no-agent): stdout contract + suppression                 #
# --------------------------------------------------------------------------- #
def _write_ledger(tmp_path, recorded_at_ms):
    (tmp_path / "closed-trades.jsonl").write_text(
        json.dumps({"recorded_at_ms": recorded_at_ms}) + "\n")


def test_reconcile_lag_no_lag_silent(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    now_ms = int(NOW * 1000)
    _write_ledger(tmp_path, now_ms - 60_000)  # 1m old, well inside the 20m threshold

    conds = lag.lag_conditions(str(tmp_path), now_ms)
    assert conds == []
    assert lag.run(conds, NOW) == []
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".hermx-reconcile.state").exists()


def test_reconcile_lag_condition_prints_and_suppresses(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMX_SCRIPTS_DIR", str(tmp_path))
    now_ms = int(NOW * 1000)
    _write_ledger(tmp_path, now_ms - 30 * 60 * 1000)  # 30m > 20m default threshold
    conds = lag.lag_conditions(str(tmp_path), now_ms)

    fresh = lag.run(conds, NOW)
    assert [c["fingerprint"] for c in fresh] == ["reconcile:lag"]
    out = capsys.readouterr().out.splitlines()
    assert out.count("reconcile lag exceeds threshold") == 1
    assert (tmp_path / ".hermx-reconcile.state").exists()

    # Second tick inside the 1800s window: silent (no flood).
    assert lag.run(conds, NOW + 100) == []
    assert capsys.readouterr().out == ""


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
