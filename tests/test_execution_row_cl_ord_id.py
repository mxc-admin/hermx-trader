"""Execution-row canonical cl_ord_id (dashboard Bug 3).

Position episodes hold submitted ``mxc`` ids (the ledger resolves Hyperliquid's
echoed ``0x`` cloid back through the submit-time cloid map), but execution rows
stamped the raw ccxt ``clientOrderId`` — for Hyperliquid a ``0x`` hex cloid — so
the position click-filter's EXACT cl_ord_id match found zero rows and the alert
join (which chains through the same hop) went empty. ``exchange_execution_records``
now stamps a canonical ``cl_ord_id`` resolved via ``pnl_cloid_map`` (falling back
to the fill-summary submitted id, then the raw echoed id), keeping the raw
``client_order_id`` untouched.
"""
from __future__ import annotations

import json

import dashboard as dash
import pnl_cloid_map

HL_CLOID = "0x" + "ab" * 16


def _adapter_result(client_order_id, fill_client_id, exchange="ccxt"):
    return {
        "ok": True,
        "mode": "submit_enabled",
        "exchange": exchange,
        "elapsed_ms": 812,
        "fill_summary": {
            "status": "submitted",
            "order_id": "ORD-1",
            "client_order_id": fill_client_id,
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
                        "clientOrderId": client_order_id,
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


def _write_pipeline(logs, adapter_result):
    (logs / "pipeline.jsonl").write_text(
        json.dumps({
            "ts": "2026-07-20T00:00:00Z",
            "stage": "execution",
            "signal_id": None,
            "received_at": "2026-07-20T00:00:00Z",
            "exec_result": {
                "mode": "submit_enabled",
                "ok": True,
                "elapsed_ms": 812,
                "payload": adapter_result,
            },
        }) + "\n",
        encoding="utf-8",
    )


def _exec_records(monkeypatch, tmp_path, adapter_result):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    _write_pipeline(logs, adapter_result)
    monkeypatch.setattr(dash, "LOGS", logs)
    monkeypatch.setattr(dash, "okx_order_history_snapshot", lambda config: {"ok": False})
    return dash.exchange_execution_records({})


def test_hl_row_with_mapped_cloid_resolves_to_mxc_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    pnl_cloid_map.record_cloid_mapping("mxc-hl-open-leg", HL_CLOID, "hyperliquid")

    recs = _exec_records(monkeypatch, tmp_path, _adapter_result(HL_CLOID, "mxc-hl-1"))
    assert recs, "expected one execution record"
    # Canonical id is the submit-time mxc id the position episodes hold...
    assert recs[0]["cl_ord_id"] == "mxc-hl-open-leg"
    # ...while the raw venue-echoed clientOrderId stays untouched.
    assert recs[0]["client_order_id"] == HL_CLOID


def test_hl_row_with_unmapped_cloid_falls_back_to_fill_summary_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))

    recs = _exec_records(monkeypatch, tmp_path, _adapter_result(HL_CLOID, "mxc-hl-1"))
    assert recs[0]["cl_ord_id"] == "mxc-hl-1"
    assert recs[0]["client_order_id"] == HL_CLOID


def test_okx_row_mxc_id_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))

    recs = _exec_records(monkeypatch, tmp_path, _adapter_result("mxc-btc-1", "mxc-btc-1"))
    assert recs[0]["cl_ord_id"] == "mxc-btc-1"
    assert recs[0]["client_order_id"] == "mxc-btc-1"


def test_cross_venue_ambiguous_cloid_does_not_resolve(monkeypatch, tmp_path):
    # The adapter envelope's "exchange" is the ccxt BACKEND name (not a venue), so a
    # cloid mapped to different mxc ids on two venues is ambiguous — fall back to the
    # fill-summary id rather than guess (zero events beats wrong events).
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    pnl_cloid_map.record_cloid_mapping("mxc-hl-leg", HL_CLOID, "hyperliquid")
    pnl_cloid_map.record_cloid_mapping("mxc-other-leg", HL_CLOID, "bitfinex")

    recs = _exec_records(monkeypatch, tmp_path, _adapter_result(HL_CLOID, "mxc-hl-1"))
    assert recs[0]["cl_ord_id"] == "mxc-hl-1"


def test_row_with_real_venue_exchange_uses_exact_venue_hit(monkeypatch, tmp_path):
    # When the row's envelope does carry a real venue, the (venue, cloid) hit wins
    # even if another venue mapped the same cloid.
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    pnl_cloid_map.record_cloid_mapping("mxc-hl-leg", HL_CLOID, "hyperliquid")
    pnl_cloid_map.record_cloid_mapping("mxc-other-leg", HL_CLOID, "bitfinex")

    recs = _exec_records(
        monkeypatch, tmp_path, _adapter_result(HL_CLOID, "mxc-hl-1", exchange="hyperliquid")
    )
    assert recs[0]["cl_ord_id"] == "mxc-hl-leg"
