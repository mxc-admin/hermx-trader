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
    (logs / "pipeline.jsonl").write_text(
        json.dumps({
            "ts": "2026-06-28T00:00:00Z",
            "stage": "execution",
            "signal_id": None,
            "received_at": "2026-06-28T00:00:00Z",
            "okx_execution": {"mode": "submit_enabled", "ok": True, "payload": {}},
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "LOGS", logs)
    monkeypatch.setattr(dash, "okx_order_history_snapshot", lambda config: {"ok": False})

    recs = dash.okx_execution_records({})
    assert recs and recs[0]["mode"] == "submit_enabled"
