"""Observe-only OKX query interface tests (REFACTOR_PLAN.md:207, :209-212, :237).

Three layers, all WITHOUT network:

(a) The PURE normalizer (raw OKX v5 envelope -> venue-neutral shape) maps the
    success / partial / not-found / aged-out fixtures to the correct normalized
    shapes. This is the heart of acceptance :237.
(b) The CLI verb dispatch + argument parsing works without creds and without
    network, by monkeypatching ``require_env`` and ``OkxClient.get`` to return
    fixture envelopes. Read-only verbs never call POST and never arm submission.
(c) ``OkxDemoExecutor`` query methods (the venue-neutral contract) return
    normalized shapes given a stubbed subprocess, and force OKX_SUBMIT_ORDERS=false.

The fixtures are the hash-stamped corpus under tests/fixtures/okx_query/.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import okx_demo_executor as okx
from executors.okx_demo import OkxDemoExecutor

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "okx_query"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) PURE normalizer -- no client, no network.
# ---------------------------------------------------------------------------


def test_normalize_order_filled():
    out = okx.normalize_order(_load("order_filled.json"))
    assert out["exchange"] == "okx_demo"
    assert out["state"] == "filled"
    assert out["inst_id"] == "BTC-USDT-SWAP"
    assert out["ord_id"] == "688577747503788032"
    assert out["cl_ord_id"] == "mxc1a1b2c3d4e5f6a7b8c9"
    assert out["acc_fill_sz"] == 3.0
    assert isinstance(out["acc_fill_sz"], float)
    assert out["avg_px"] == 63000.5
    assert out["side"] == "buy"
    assert out["pos_side"] == "long"
    assert out["ord_type"] == "market"
    assert out["ts"] == "1719300001000"


def test_normalize_order_partially_filled():
    out = okx.normalize_order(_load("order_partially_filled.json"))
    assert out["state"] == "partially_filled"
    assert out["acc_fill_sz"] == 1.0
    # Partial: 0 < accFillSz < ordered size (3) -- the signal Task 4 maps to FILLED+partial.
    ordered = float(out["raw"]["sz"])
    assert 0.0 < out["acc_fill_sz"] < ordered
    assert out["avg_px"] == 3010.25


def test_normalize_order_canceled_zero_fill():
    out = okx.normalize_order(_load("order_canceled_zero_fill.json"))
    # canceled + accFillSz==0 -> REJECTED later (:211).
    assert out["state"] == "canceled"
    assert out["acc_fill_sz"] == 0.0
    assert out["avg_px"] is None  # empty avgPx normalizes to None, not 0.0


def test_normalize_order_not_found_is_not_an_exception():
    envelope = _load("order_not_found.json")
    out = okx.normalize_order(envelope)
    assert out["state"] == "not_found"  # -> REJECTED later (:212), never raises
    assert out["raw"] == envelope  # the raw error is preserved for reconciliation
    assert out["acc_fill_sz"] == 0.0
    assert out["ord_id"] is None


def test_normalize_orders_history_archive_aged_out():
    # An order that has aged out of the live set but is found, filled, in archive.
    rows = okx.normalize_orders(_load("orders_history_archive_aged.json"))
    assert len(rows) == 1
    aged = rows[0]
    assert aged["state"] == "filled"
    assert aged["acc_fill_sz"] == 2.0
    assert aged["avg_px"] == 61500.0
    assert aged["inst_id"] == "BTC-USDT-SWAP"


def test_normalize_orders_pending_live():
    rows = okx.normalize_orders(_load("orders_pending.json"))
    assert len(rows) == 1
    assert rows[0]["state"] == "live"
    assert rows[0]["acc_fill_sz"] == 0.0


def test_normalize_positions_open_is_signed():
    rows = okx.normalize_positions(_load("positions_open.json"))
    assert len(rows) == 1
    pos = rows[0]
    assert pos["exchange"] == "okx_demo"
    assert pos["inst_id"] == "BTC-USDT-SWAP"
    assert pos["pos_side"] == "long"
    assert pos["pos"] == 3.0  # long -> positive signed contracts
    assert pos["upl"] == 12.5


def test_normalize_positions_flat_is_empty():
    assert okx.normalize_positions(_load("positions_flat.json")) == []


def test_normalize_balance():
    rows = okx.normalize_balances(_load("balance.json"))
    assert len(rows) == 1
    bal = rows[0]
    assert bal["exchange"] == "okx_demo"
    assert bal["ccy"] == "USDT"
    assert bal["eq"] == 100012.5
    assert bal["avail"] == 99000.0


def test_normalize_balance_ccy_filter():
    assert okx.normalize_balances(_load("balance.json"), ccy="BTC") == []
    assert len(okx.normalize_balances(_load("balance.json"), ccy="USDT")) == 1


# ---------------------------------------------------------------------------
# (b) CLI verb dispatch -- no creds, no network. Monkeypatch require_env +
#     OkxClient.get to serve fixtures; assert read-only (POST forbidden).
# ---------------------------------------------------------------------------

# Map a request path substring -> fixture envelope.
_PATH_FIXTURES = [
    ("/api/v5/trade/order?", "order_filled.json"),
    ("/api/v5/trade/orders-pending", "orders_pending.json"),
    ("/api/v5/trade/orders-history-archive", "orders_history_archive_aged.json"),
    ("/api/v5/account/positions", "positions_open.json"),
    ("/api/v5/account/balance", "balance.json"),
]


@pytest.fixture
def offline_okx(monkeypatch):
    """OkxClient that needs no creds and serves fixtures over a fake GET; POST raises."""
    monkeypatch.setattr(okx, "require_env", lambda name: "test-" + name)

    def fake_get(self, path, *, private=True):
        for needle, fixture in _PATH_FIXTURES:
            if needle in path:
                return {"http_status": 200, "elapsed_ms": 1, "payload": _load(fixture)}
        raise AssertionError(f"unexpected GET path: {path}")

    def forbidden_post(self, path, body):  # read-only invariant guard
        raise AssertionError(f"read-only verb must never POST (path={path})")

    monkeypatch.setattr(okx.OkxClient, "get", fake_get, raising=True)
    monkeypatch.setattr(okx.OkxClient, "post", forbidden_post, raising=True)
    return okx


def _run_cli(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["okx_demo_executor.py", *argv])
    rc = okx.main()
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_cli_query_order_dispatch(offline_okx, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, ["query-order", "--inst-id", "BTC-USDT-SWAP", "--ord-id", "688577747503788032"])
    assert out["state"] == "filled"
    assert out["exchange"] == "okx_demo"
    assert out["acc_fill_sz"] == 3.0


def test_cli_query_order_requires_identifier(offline_okx, monkeypatch):
    monkeypatch.setattr("sys.argv", ["okx_demo_executor.py", "query-order", "--inst-id", "BTC-USDT-SWAP"])
    with pytest.raises(SystemExit):  # argparse parser.error -> SystemExit
        okx.main()


def test_cli_orders_pending_dispatch(offline_okx, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, ["orders-pending", "--inst-id", "XRP-USDT-SWAP"])
    assert out["exchange"] == "okx_demo"
    assert [o["state"] for o in out["orders"]] == ["live"]


def test_cli_orders_history_archive_dispatch(offline_okx, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, ["orders-history-archive", "--inst-id", "BTC-USDT-SWAP", "--limit", "10"])
    assert out["orders"][0]["state"] == "filled"
    assert out["orders"][0]["acc_fill_sz"] == 2.0


def test_cli_positions_dispatch(offline_okx, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, ["positions"])
    assert out["positions"][0]["pos"] == 3.0


def test_cli_balance_dispatch(offline_okx, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, ["balance", "--ccy", "USDT"])
    assert out["balances"][0]["ccy"] == "USDT"
    assert out["balances"][0]["eq"] == 100012.5


def test_cli_query_verbs_are_submission_independent(offline_okx, monkeypatch, capsys):
    """Read-only verbs never arm submission: SUBMIT_ORDERS is irrelevant and POST is forbidden.

    The offline_okx fixture makes OkxClient.post raise, so a clean rc==0 across
    all query verbs proves no submission path was taken regardless of env.
    """
    monkeypatch.setenv("OKX_SUBMIT_ORDERS", "true")  # even if armed, queries must not submit
    for argv in (
        ["query-order", "--inst-id", "BTC-USDT-SWAP", "--cl-ord-id", "mxc1a1b2c3d4e5f6a7b8c9"],
        ["orders-pending"],
        ["positions"],
        ["balance"],
    ):
        _run_cli(monkeypatch, capsys, argv)  # asserts rc==0; forbidden_post would have raised


# ---------------------------------------------------------------------------
# (c) OkxDemoExecutor venue-neutral query methods -- stubbed subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_subprocess(monkeypatch, repo_root):
    """Capture argv/env and return canned normalized JSON instead of running the CLI."""
    calls = {}

    def make(stdout_obj):
        def fake_run(argv, **kwargs):
            calls["argv"] = argv
            calls["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(stdout_obj), stderr="")

        monkeypatch.setattr("executors.okx_demo.subprocess.run", fake_run)
        return calls

    return make


def _executor(repo_root):
    # Root must point at the repo so the real src/okx_demo_executor.py exists()
    # (the subprocess itself is stubbed, so nothing actually runs).
    return OkxDemoExecutor({"execution": {"exchange": "okx_demo"}}, repo_root)


def test_executor_get_order_returns_normalized(stub_subprocess, repo_root):
    normalized = okx.normalize_order(_load("order_filled.json"))
    calls = stub_subprocess(normalized)
    ex = _executor(repo_root)
    out = ex.get_order("BTC-USDT-SWAP", ord_id="688577747503788032")
    assert out["state"] == "filled"
    assert out["exchange"] == "okx_demo"
    # Read-only invariant: the subprocess env must NEVER arm submission.
    assert calls["env"].get("OKX_SUBMIT_ORDERS") == "false"
    assert "query-order" in calls["argv"]
    assert "--inst-id" in calls["argv"]


def test_executor_get_positions_returns_list(stub_subprocess, repo_root):
    envelope = okx._query_list_envelope(okx.normalize_positions(_load("positions_open.json")), "positions")
    calls = stub_subprocess(envelope)
    ex = _executor(repo_root)
    out = ex.get_positions()
    assert isinstance(out, list)
    assert out[0]["pos"] == 3.0
    assert calls["env"].get("OKX_SUBMIT_ORDERS") == "false"


def test_executor_get_balance_returns_list(stub_subprocess, repo_root):
    envelope = okx._query_list_envelope(okx.normalize_balances(_load("balance.json")), "balances")
    stub_subprocess(envelope)
    ex = _executor(repo_root)
    out = ex.get_balance("USDT")
    assert out[0]["ccy"] == "USDT"


def test_executor_query_failure_degrades_safely(monkeypatch, repo_root):
    # Subprocess fails -> get_order returns a normalized error, never raises.
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr("executors.okx_demo.subprocess.run", fake_run)
    ex = _executor(repo_root)
    out = ex.get_order("BTC-USDT-SWAP", ord_id="x")
    assert out["state"] == "error"
    assert out["exchange"] == "okx_demo"


def test_base_executor_query_defaults_are_safe():
    # A venue with no query path must degrade, not crash (venue-neutral default).
    from executors.base import BaseExecutor

    class Bare(BaseExecutor):
        key = "bare"

        def execute(self, readiness):  # only abstract method
            return self.normalized_result(ok=True, mode="noop")

    ex = Bare({}, repo_root_placeholder())
    assert ex.get_order("X")["state"] == "not_implemented"
    assert ex.get_open_orders() == []
    assert ex.get_order_history_archive() == []
    assert ex.get_positions() == []
    assert ex.get_balance() == []


def repo_root_placeholder() -> Path:
    return Path(__file__).resolve().parents[1]
