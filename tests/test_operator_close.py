"""P0 -- operator-instructed close (POST /api/close + execute_operator_close).

A close is a RISK-REDUCING flatten an operator triggers out-of-band. It routes
through the SAME controlled ExecutionService as a normal submit, but the readiness
``close_only`` flag bypasses exactly two gates -- the global HERMX_LIVE_TRADING kill
switch and the per-symbol pause -- because both exist to stop NEW risk and a close
only reduces it. Every other gate (idempotency, auth, watchdog) still applies.
(2-mode model: there is no submit_orders arming gate anymore -- demo and live both
submit; demo routes to the sandbox.)

These tests are fully offline: the executor is mocked via ``ExecutorFactory.create``
so the only way the submit call is reached is by passing the gate chain; no
exchange/network is touched. The ``wr`` fixture gives each test an isolated temp
order journal so submitting tests do not collide on the deterministic close id.
"""
from __future__ import annotations

import io
import json
from unittest import mock

from conftest import fake_executor as _fake_executor


def _strategy(*, submit_orders: bool = True, execution_mode: str = "live",
              sid: str = "btcusdt_duo_base_dev_2h") -> dict:
    return {
        "strategy_id": sid,
        "submit_orders": submit_orders,
        "execution_mode": execution_mode,
        "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
        "leverage": 2,
        "margin_mode": "isolated",
        "asset": "BTCUSDT",
    }


def _make_handler(wr, *, token=None, body=None, raw=None, content_length=None):
    """Construct a Handler without the socket-bound BaseHTTPRequestHandler.__init__,
    stub its request inputs, and capture _send(status, body) calls."""
    handler = wr.Handler.__new__(wr.Handler)
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-Dashboard-Token"] = token
    if raw is None:
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
    headers["Content-Length"] = str(len(raw) if content_length is None else content_length)
    handler.headers = headers
    handler.rfile = io.BytesIO(raw)
    captured: list[tuple[int, dict]] = []
    handler._send = lambda status, payload: captured.append((status, payload))
    return handler, captured


# ---------------------------------------------------------------------------
# 1. Kill-switch bypass -- the critical regression test.
# ---------------------------------------------------------------------------

def test_close_bypasses_kill_switch_when_live_trading_disabled(wr, monkeypatch):
    """A LIVE-mode close succeeds even with HERMX_LIVE_TRADING unset. Without the
    close_only bypass this exact record would be blocked ``live_trading_disabled``."""
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_operator_close("BTCUSDT", _strategy(execution_mode="live"),
                                        operator="telegram", reason="operator_instructed_close")

    create_mock.assert_called_once()
    fake.execute.assert_called_once()
    assert out["ok"] is True
    assert out["mode"] == "submit_enabled"
    # OKX-safe opaque id: opcls + truncated sha256 hex (alphanumeric, 32 chars).
    assert out["_cl_ord_id"].startswith("opcls")
    assert out["_cl_ord_id"].isalnum()
    assert len(out["_cl_ord_id"]) <= 32


# ---------------------------------------------------------------------------
# 2. submit_orders flag is ignored (2-mode model) -- a demo close still submits.
# ---------------------------------------------------------------------------

def test_close_submits_demo_ignoring_legacy_submit_orders(wr, monkeypatch):
    # The legacy per-strategy submit_orders flag is gone; both demo and live submit.
    # An operator close on a demo strategy routes to the sandbox regardless of the
    # (now-ignored) flag.
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake) as create_mock:
        out = wr.execute_operator_close("BTCUSDT", _strategy(submit_orders=False, execution_mode="demo"))

    create_mock.assert_called_once()
    fake.execute.assert_called_once()
    assert out["mode"] == "submit_enabled"


# ---------------------------------------------------------------------------
# 3. Auth -- X-Dashboard-Token must equal HERMX_SECRET (constant-time).
# ---------------------------------------------------------------------------

def test_close_rejects_missing_token(wr):
    handler, captured = _make_handler(wr, token=None, body={"symbol": "BTCUSDT", "strategy_id": "btcusdt_duo_base_dev_2h"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 401
    assert payload["ok"] is False


def test_close_rejects_wrong_token(wr):
    handler, captured = _make_handler(wr, token="not-the-secret", body={"symbol": "BTCUSDT", "strategy_id": "btcusdt_duo_base_dev_2h"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 401
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# 4. Validation -- symbol and strategy_id are required.
# ---------------------------------------------------------------------------

def test_close_requires_symbol(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"strategy_id": "btcusdt_duo_base_dev_2h"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 400
    assert payload["error"] == "missing_symbol"


def test_close_requires_strategy_id(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"symbol": "BTCUSDT"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 400
    assert payload["error"] == "missing_strategy_id"


# ---------------------------------------------------------------------------
# 5. Strategy not found -> 404.
# ---------------------------------------------------------------------------

def test_close_unknown_strategy_returns_404(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"symbol": "BTCUSDT", "strategy_id": "does_not_exist"})
    handler._handle_operator_close()
    status, payload = captured[-1]
    assert status == 404
    assert payload["error"] == "unknown_strategy_id"


# ---------------------------------------------------------------------------
# 6. Idempotency -- a repeat close (same deterministic cl_ord_id) is refused.
# ---------------------------------------------------------------------------

def test_close_is_idempotent_on_duplicate_cl_ord_id(wr, monkeypatch):
    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    strategy = _strategy(execution_mode="demo")  # demo: sandbox submit, no kill-switch concern

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        first = wr.execute_operator_close("BTCUSDT", strategy)
        second = wr.execute_operator_close("BTCUSDT", strategy)

    assert first["ok"] is True
    assert first["mode"] == "submit_enabled"
    assert second["mode"] == "not_submitted"
    assert second["reason"] == "duplicate_cl_ord_id"
    # Only the first close ever reached the executor.
    fake.execute.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Distinct same-day closes must NOT collide (cl_ord_id day-granularity bug).
# ---------------------------------------------------------------------------

def test_two_distinct_same_day_closes_both_submit(wr, monkeypatch):
    """Regression: a full-day close id (``operator_close_{sym}_{sid}_{YYYYMMDD}``)
    made a SECOND distinct close for the same symbol/strategy on the same UTC day
    collide on the order-journal dedupe key -- silently refused ``duplicate_cl_ord_id``
    so the flatten never reached the venue. With seconds granularity two closes a few
    seconds apart get distinct ids and BOTH reach the executor."""
    import datetime as _dt

    monkeypatch.delenv("HERMX_LIVE_TRADING", raising=False)
    strategy = _strategy(execution_mode="demo")

    times = [
        _dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 7, 4, 10, 0, 5, tzinfo=_dt.timezone.utc),  # same day, +5s
    ]

    class _ClockDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return times.pop(0)

    monkeypatch.setattr(wr, "datetime", _ClockDT)

    fake = _fake_executor()
    with mock.patch.object(wr.ExecutorFactory, "create", return_value=fake):
        first = wr.execute_operator_close("BTCUSDT", strategy)
        second = wr.execute_operator_close("BTCUSDT", strategy)

    assert first["mode"] == "submit_enabled"
    assert second["mode"] == "submit_enabled"
    assert first["_cl_ord_id"] != second["_cl_ord_id"]
    # Both distinct closes reached the venue -- neither was dropped as a duplicate.
    assert fake.execute.call_count == 2


# ---------------------------------------------------------------------------
# 8. OKX-safe cl_ord_id format (opcls + sha256 hex) preserving exact idempotency.
# ---------------------------------------------------------------------------

def _freeze_clock(wr, monkeypatch, *whens):
    """Monkeypatch wr.datetime so successive .now() calls return ``whens`` in order
    (repeating the last one)."""
    import datetime as _dt

    seq = list(whens)

    class _ClockDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(wr, "datetime", _ClockDT)


def test_cl_ord_id_is_okx_safe_alphanumeric_max_32(wr):
    """OKX clientOrderId spec: alphanumeric only, <=32 chars. The legacy readable id
    (62 chars, 9 underscores) violated it and was passed raw to the venue."""
    cl_ord_id = wr._operator_close_cl_ord_id("BTC_USDT", "btcusdt_duo_base_dev_2h")
    assert cl_ord_id.startswith("opcls")
    assert cl_ord_id.isalnum()
    assert len(cl_ord_id) <= 32


def test_cl_ord_id_same_second_same_inputs_identical(wr, monkeypatch):
    """Idempotency domain unchanged: same symbol+strategy within the same UTC second
    hashes to the IDENTICAL id, so a duplicate resubmit still trips the journal dedupe."""
    import datetime as _dt

    _freeze_clock(wr, monkeypatch, _dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=_dt.timezone.utc))
    first = wr._operator_close_cl_ord_id("BTCUSDT", "strat_a")
    second = wr._operator_close_cl_ord_id("BTCUSDT", "strat_a")
    assert first == second


def test_cl_ord_id_differs_across_symbol_strategy_and_second(wr, monkeypatch):
    """No false collisions: a different symbol OR strategy at the same second, or the
    same inputs one second later, each yield a distinct id."""
    import datetime as _dt

    t0 = _dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=_dt.timezone.utc)
    _freeze_clock(wr, monkeypatch, t0)
    base = wr._operator_close_cl_ord_id("BTCUSDT", "strat_a")
    other_symbol = wr._operator_close_cl_ord_id("ETHUSDT", "strat_a")
    other_strategy = wr._operator_close_cl_ord_id("BTCUSDT", "strat_b")
    _freeze_clock(wr, monkeypatch, t0 + _dt.timedelta(seconds=1))
    next_second = wr._operator_close_cl_ord_id("BTCUSDT", "strat_a")
    assert len({base, other_symbol, other_strategy, next_second}) == 4


def test_legacy_operator_close_ids_still_recognized_and_parsed(wr):
    """Historical ledger rows keep working: legacy ``operator_close_...`` ids (and the
    new ``opcls`` form) pass is_hermx_cl_ord_id, and the legacy parser still recovers
    the sid from old ids while returning None for the new opaque hash."""
    import pnl_ledger

    legacy = "operator_close_BTC_USDT_my_strat_v2_20260703"
    assert pnl_ledger.is_hermx_cl_ord_id(legacy) is True
    assert pnl_ledger._parse_operator_close_strategy_id(legacy, "BTC-USDT") == "my_strat_v2"

    new_id = wr._operator_close_cl_ord_id("BTCUSDT", "strat_a")
    assert pnl_ledger.is_hermx_cl_ord_id(new_id) is True
    assert pnl_ledger._parse_operator_close_strategy_id(new_id, "BTC-USDT") is None
