"""Phase 5 -- normalization & ledger cleanup.

Covers:
  - strategy_instrument no longer assumes okx for a venue-less top-level inst_id
    (all on-disk strategies are v2 with an explicit instrument block);
  - strategy lookup is keyed by the strategy_id FIELD, not the filename;
  - dashboard symbol<->inst_id mapping is venue-aware (driven by the strategy's own
    instrument block, not a hardcoded -USDT-SWAP transform);
  - the dead okx-* legacy execution-ledger mirror constants are gone and the dashboard
    reads the canonical executions ledger.
"""
from __future__ import annotations

import json

import dashboard as dash
import webhook_receiver as wr


# ---------------------------------------------------------------------------
# 5.1 -- no venue-less inst_id -> okx fallback.
# ---------------------------------------------------------------------------

def test_strategy_instrument_drops_venueless_inst_id_fallback():
    # A row with ONLY a top-level inst_id (no instrument block) must NOT silently assume
    # okx; it resolves to {} so the caller fails closed on an unresolved venue.
    assert wr.strategy_instrument({"inst_id": "BTC-USDT-SWAP"}) == {}
    assert wr.strategy_instrument({}) == {}


def test_strategy_instrument_resolves_explicit_venue_block():
    # A proper instrument block still resolves, venue-aware (kucoin spot, not okx).
    assert wr.strategy_instrument(
        {"instrument": {"exchange": "kucoin", "inst_id": "ETH-BTC", "type": "spot"}}
    ) == {"exchange": "kucoin", "inst_id": "ETH-BTC", "type": "spot"}


# ---------------------------------------------------------------------------
# 5.3 -- lookup keyed by strategy_id field, not filename.
# ---------------------------------------------------------------------------

def test_strategy_lookup_keyed_by_id_not_filename(wr, monkeypatch, tmp_path):
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "totally_different_filename.json").write_text(
        json.dumps({
            "strategy_id": "btcusdt_real_id",
            "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
            "submit_orders": False,
            "execution_mode": "demo",
            "timeframe": "2h",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(wr, "STRATEGIES_DIR", sdir)

    loaded = wr.load_strategy_files()
    assert "btcusdt_real_id" in loaded            # keyed by the FIELD
    assert "totally_different_filename" not in loaded  # NOT by the filename stem
    assert loaded["btcusdt_real_id"]["strategy_id"] == "btcusdt_real_id"


# ---------------------------------------------------------------------------
# 5.4 -- dashboard symbol<->inst_id mapping is venue-aware.
# ---------------------------------------------------------------------------

def test_dashboard_inst_id_is_venue_aware(monkeypatch):
    strat = {"asset": "ETHBTC", "instrument": {"exchange": "kucoin", "inst_id": "ETH-BTC", "type": "spot"}}
    monkeypatch.setattr(dash, "load_strategy_files", lambda: [strat])
    monkeypatch.setattr(dash, "trial_symbols", lambda config=None: ["ETHBTC"])

    # Resolves to the strategy's OWN venue id, not a fabricated -USDT-SWAP id.
    assert dash.strategy_inst_id({}, "ETHBTC") == "ETH-BTC"
    # Reverse mapping recovers the asset venue-neutrally.
    assert dash.symbol_from_inst_id({}, "ETH-BTC") == "ETHBTC"


def test_dashboard_unknown_symbol_no_hardcoded_okx_transform(monkeypatch):
    monkeypatch.setattr(dash, "load_strategy_files", lambda: [])
    monkeypatch.setattr(dash, "trial_symbols", lambda config=None: [])
    # No strategy/config mapping => empty, NOT a fabricated FOO-USDT-SWAP.
    assert dash.strategy_inst_id({}, "FOOUSDT") == ""


# ---------------------------------------------------------------------------
# 5.5 -- dead okx-* legacy ledger mirrors removed; dashboard reads canonical ledger.
# ---------------------------------------------------------------------------

def test_legacy_okx_ledger_constants_removed(wr):
    assert not hasattr(wr, "LEGACY_EXECUTION_LEDGER")
    assert not hasattr(wr, "LEGACY_EXECUTION_PLAN_LEDGER")


def test_dashboard_reads_canonical_executions_ledger(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    # Execution outcomes live in the unified pipeline ledger under stage="execution".
    # The service wraps the adapter's normalized_result() (executors/base.py) under
    # okx_execution.payload: fill_summary at the top of that envelope, and the venue
    # payload (executed_orders / symbol / target_direction) one level deeper under its
    # own ``payload`` key. The reader must extract from THIS shape, not the retired
    # OKX-native payload.plan / payload.okx_fill_summary shape.
    adapter_result = {
        "ok": True,
        "mode": "submit_enabled",
        "exchange": "okx_demo",
        "elapsed_ms": 812,
        "fill_summary": {
            "status": "submitted",
            "order_id": "ORD-1",
            "client_order_id": "mxc-btc-1",
            "avg_fill_price": 65010.0,
            "filled_size": 0.05,
            "position_after_order": {"side": "long", "contracts": 0.05},
        },
        "payload": {
            "executed_orders": [
                {
                    "action": "OPEN_LONG",
                    "submitted": True,
                    "status": "submitted",
                    "requested_amount": 0.05,
                    "order": {
                        "id": "ORD-1",
                        "clientOrderId": "mxc-btc-1",
                        "side": "buy",
                        "status": "closed",
                        "average": 65010.0,
                        "filled": 0.05,
                        "cost": 3250.5,
                        "fee": {"cost": 1.5, "currency": "USDT"},
                    },
                }
            ],
            "symbol": "BTC/USDT:USDT",
            "reference_price": 65000.0,
            "target_direction": "long",
        },
    }
    (logs / "pipeline.jsonl").write_text(
        json.dumps({
            "ts": "2026-06-28T00:00:00Z",
            "stage": "execution",
            "signal_id": None,
            "received_at": "2026-06-28T00:00:00Z",
            "okx_execution": {
                "mode": "submit_enabled",
                "ok": True,
                "elapsed_ms": 812,
                "payload": adapter_result,
            },
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "LOGS", logs)
    monkeypatch.setattr(dash, "okx_order_history_snapshot", lambda config: {"ok": False})

    recs = dash.okx_execution_records({})
    assert recs, "expected one execution record"
    rec = recs[0]
    assert rec["mode"] == "submit_enabled"
    assert rec["ok"] is True
    # Symbol / side now come from the CCXT venue payload (payload.payload), not payload.plan.
    assert rec["symbol"] == "BTC/USDT:USDT"
    assert rec["signal"] == "LONG"
    assert rec["okx_side"] == "buy"
    assert rec["okx_action"] == "OPEN_LONG"
    # Order id / fill data now come from the nested ccxt order dict + top-level fill_summary.
    assert rec["order_id"] == "ORD-1"
    assert rec["client_order_id"] == "mxc-btc-1"
    assert rec["okx_price"] == 65010.0
    assert rec["contracts"] == 0.05
    assert rec["notional"] == 3250.5
    assert rec["fee"] == 1.5
    assert rec["position_after"] == "LONG"


def test_dashboard_backfills_sparse_gate_blocked_row_from_journal(monkeypatch, tmp_path):
    """A gate-blocked (no-payload) execution row carries no CCXT payload, so symbol/side/
    notional are blank. When a cl_ord_id is present (the idempotency gate stamps one on the
    result), okx_execution_records backfills the missing fields from the order journal's
    submit-time intent via the fail-open latest_order_record enrichment path."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "pipeline.jsonl").write_text(
        json.dumps({
            "ts": "2026-06-28T01:00:00Z",
            "stage": "execution",
            "signal_id": None,
            "received_at": "2026-06-28T01:00:00Z",
            # Shape written by execution/service.py::_blocked for the idempotency gate:
            # no ``payload`` key at all, cl_ord_id stamped at the top of okx_execution.
            "okx_execution": {
                "ok": True,
                "mode": "not_submitted",
                "reason": "duplicate_cl_ord_id",
                "gate": "idempotency",
                "cl_ord_id": "mxc-eth-9",
                "existing_state": "submitted",
            },
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "LOGS", logs)
    monkeypatch.setattr(dash, "okx_order_history_snapshot", lambda config: {"ok": False})
    # Submit-time journal record for the blocked cl_ord_id (orders.journal shape).
    journal_rec = {
        "cl_ord_id": "mxc-eth-9",
        "state": "submitted",
        "intent": {
            "symbol": "ETH/USDT:USDT",
            "side": "long",
            "inst_id": "ETH-USDT-SWAP",
            "planned_notional_usd": 500.0,
        },
    }
    monkeypatch.setattr(dash, "latest_order_record",
                        lambda cl: journal_rec if cl == "mxc-eth-9" else None)

    recs = dash.okx_execution_records({})
    assert recs, "expected one execution record"
    rec = recs[0]
    assert rec["mode"] == "not_submitted"
    # These were blank on the raw sparse row; the journal enrichment backfilled them.
    assert rec["symbol"] == "ETH/USDT:USDT"
    assert rec["inst_id"] == "ETH-USDT-SWAP"
    assert rec["signal"] == "LONG"
    assert rec["cl_ord_id"] == "mxc-eth-9"
    assert rec["planned_notional"] == 500.0
    assert rec["order_status"] == "submitted"


def test_dashboard_sparse_row_without_cl_ord_id_left_blank(monkeypatch, tmp_path):
    """Gates that fire BEFORE the cl_ord_id is computed (arming, kill switch, symbol pause,
    ...) leave no cl_ord_id on the row, so there is nothing to look up. The enrichment must
    fail open: the row stays sparse, the journal is never consulted, no data is fabricated."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "pipeline.jsonl").write_text(
        json.dumps({
            "ts": "2026-06-28T02:00:00Z",
            "stage": "execution",
            "signal_id": None,
            "received_at": "2026-06-28T02:00:00Z",
            "okx_execution": {
                "ok": True,
                "mode": "not_submitted",
                "reason": "live_trading_disabled",
                "gate": "live_trading_kill_switch",
            },
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "LOGS", logs)
    monkeypatch.setattr(dash, "okx_order_history_snapshot", lambda config: {"ok": False})

    def _must_not_be_called(cl):  # pragma: no cover - asserted via raise
        raise AssertionError("journal lookup must not run without a cl_ord_id")

    monkeypatch.setattr(dash, "latest_order_record", _must_not_be_called)

    recs = dash.okx_execution_records({})
    assert recs and recs[0]["mode"] == "not_submitted"
    assert not recs[0].get("symbol")
    assert not recs[0].get("cl_ord_id")
