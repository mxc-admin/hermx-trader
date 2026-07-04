"""Failing-first guards for the strategy-attribution regression (C1) plus the
H1/H2/H3 hardening gaps in the P&L reconcile path.

These tests are written to FAIL against the current code, proving each bug, and to
turn green once the corresponding fix lands. They intentionally exercise the real
``pnl_ledger`` / ``dashboard`` code paths — never a re-implemented copy — following
the fixture conventions in ``tests/test_pnl_ledger.py`` (``ledger_dir`` isolates the
ledger to a tmp dir via ``HERMX_DATA_DIR``; the path resolves at call time so no
module reload is needed).

Root cause of the C1 regression: ``pnl_ledger._build_entry`` sets
``strategy_id = row.get("strategy_id")``. Exchange order-history rows do NOT carry a
``strategy_id``, so every reconciled close lands with ``strategy_id=None`` and
``aggregate_strategy_pnl(<sid>)`` sums zero rows forever — the user-visible "realized
P&L = 0" symptom. The fix threads a submit-time ``cl_ord_id -> strategy_id`` map
through reconcile. That writer does not exist yet; tests that require it call the
expected ``pnl_ledger.record_submit_strategy`` interface and are marked
``xfail(strict=True)`` because they raise ``AttributeError`` today (per test-writing
rule 3). Removing the marker is part of landing the fix.
"""
from __future__ import annotations

import json
import logging

import pytest

import pnl_cloid_map
import pnl_ledger


# ---------------------------------------------------------------------------
# Submit-time strategy-map writer (C1 infrastructure the fix must provide).
#
# The fix will expose a submit-time ``cl_ord_id -> strategy_id`` recorder on
# ``pnl_ledger`` (mirroring ``pnl_cloid_map.record_cloid_mapping``). It does not
# exist today, so calling it raises AttributeError -> the tests that depend on it
# xfail(strict=True). We deliberately do NOT import a ``pnl_strategy_map`` module
# (it doesn't exist yet) — attribution is verified through the ``pnl_ledger``
# interface that will change.
# ---------------------------------------------------------------------------

def _record_submit_strategy(cl_ord_id: str, strategy_id: str) -> None:
    pnl_ledger.record_submit_strategy(cl_ord_id, strategy_id)  # type: ignore[attr-defined]


def _close_row(cl_ord_id, *, inst_id="BTC-USDT-SWAP", ord_id="close1",
               pnl="100.0", side="sell", uTime=200):
    """A single reduceOnly close row in exchange-order-history shape."""
    return {
        "instId": inst_id,
        "ordId": ord_id,
        "clOrdId": cl_ord_id,
        "side": side,
        "accFillSz": 1.0,
        "reduceOnly": True,
        "avgPx": "51000",
        "pnl": pnl,
        "fee": "-0.5",
        "feeCcy": "USDT",
        "uTime": uTime,
    }


# ---------------------------------------------------------------------------
# C1 — attribution round-trip
# ---------------------------------------------------------------------------

def test_reconcile_attributes_strategy_via_submit_map(ledger_dir):
    """Proves: a close whose cl_ord_id was mapped at submit time is attributed to
    its strategy, so ``aggregate_strategy_pnl`` returns nonzero closed P&L.

    Fails today: the submit-map writer doesn't exist (AttributeError), and even if
    it did ``_build_entry`` reads ``row.get("strategy_id")`` (always None on real
    exchange rows) — attribution never happens. The fix records the submit-time
    ``cl_ord_id -> strategy_id`` map and has reconcile resolve it, making
    ``closed_net_pnl_usd`` nonzero for the mapped strategy.
    """
    _record_submit_strategy("mxcAlphaClose", "alpha")
    pnl_ledger.reconcile_from_order_history([_close_row("mxcAlphaClose")], "okx", "demo")

    agg = pnl_ledger.aggregate_strategy_pnl("alpha", mode="demo")
    assert agg["closed_order_count"] == 1
    assert agg["closed_net_pnl_usd"] != 0


def test_reconcile_unmapped_close_persisted_with_strategy_none(ledger_dir):
    """Proves: a HermX close with NO submit-time mapping is still written to the
    ledger (portfolio total is never lost) — just tagged ``strategy_id=None``.

    Why this matters: it documents that "attribution required to persist" is the
    WRONG fix. The row must always land; only its per-strategy attribution is
    best-effort. This should pass today AND after the fix (behavior is preserved).
    """
    pnl_ledger.reconcile_from_order_history([_close_row("mxcOrphan", ord_id="orphan")], "okx", "demo")

    # No strategy filter -> the row is present (portfolio total intact).
    all_rows = pnl_ledger.read_closed_trades(strategy_id=None)
    assert [r["ord_id"] for r in all_rows] == ["orphan"]
    assert all_rows[0]["strategy_id"] is None
    # Filtering by any concrete strategy finds nothing (it is unattributed).
    assert pnl_ledger.read_closed_trades(strategy_id="any") == []


def test_operator_close_cl_ord_id_attributes_without_map(ledger_dir):
    """Proves: an ``operator_close_{symbol}_{sid}_{utcday}`` cl_ord_id recovers the
    strategy id via a delimiter-safe parser even when BOTH symbol and sid contain
    underscores.

    Ambiguous case documented explicitly: ``operator_close_BTC_USDT_my_strat_v2_20260703``
    -> rightmost segment ``20260703`` is the UTC day, the segment before it
    (``v2``)... is NOT the whole sid. A naive ``rsplit("_")`` would take ``v2`` as
    the sid. The parser must treat everything between the ``operator_close_`` prefix
    and the trailing date as ``{symbol}_{sid}`` and split symbol/sid on the venue's
    known symbol shape (``BTC_USDT``), yielding sid ``my_strat_v2``.

    Fails today: ``_build_entry`` never parses the cl_ord_id, so ``strategy_id`` is
    None and ``aggregate_strategy_pnl("my_strat_v2")`` counts zero rows. The fix adds
    the delimiter-safe parser.
    """
    cl_ord_id = "operator_close_BTC_USDT_my_strat_v2_20260703"
    pnl_ledger.reconcile_from_order_history(
        [_close_row(cl_ord_id, inst_id="BTC-USDT", ord_id="opClose")], "okx", "demo"
    )

    agg = pnl_ledger.aggregate_strategy_pnl("my_strat_v2")
    assert agg["closed_order_count"] == 1


def test_hyperliquid_numeric_cloid_attributes_via_two_map_hops(ledger_dir):
    """Proves: a Hyperliquid numeric cloid attributes through TWO map hops —
    ``numeric_cloid -> mxc_id`` (pnl_cloid_map) then ``mxc_id -> strategy_id``
    (submit map).

    Hyperliquid echoes back a numeric cloid, not the submitted ``mxc`` id. Reconcile
    already resolves ``numeric_cloid -> mxc_id`` via ``pnl_cloid_map`` and stores the
    original mxc id as ``cl_ord_id``. Attribution then needs ``mxc_id -> strategy_id``.

    Fails today: the second hop's writer (submit map) doesn't exist (AttributeError);
    ``strategy_id`` stays None. The fix chains both maps so the mapped strategy sees
    the close.
    """
    pnl_cloid_map.record_cloid_mapping("mxc-hl", "555000555", "hyperliquid")
    _record_submit_strategy("mxc-hl", "hl_strat")

    row = _close_row("555000555", inst_id="BTC", ord_id="hlClose")
    row["pnl"] = None
    row["realized_pnl"] = 42.0
    pnl_ledger.reconcile_from_order_history([row], "hyperliquid", "live")

    agg = pnl_ledger.aggregate_strategy_pnl("hl_strat", mode="live")
    assert agg["closed_order_count"] == 1


def test_strategy_pnl_nonzero_after_reconcile_roundtrip(ledger_dir):
    """Proves (the headline symptom): after a real reconcile of a HermX close, the
    strategy shows NONZERO realized P&L — not $0 forever.

    This is the end-to-end reproduction of the user-visible bug: realized P&L stays
    at zero because reconciled rows are never attributed. Rows are produced by the
    real ``reconcile_from_order_history`` path (not hand-injected ledger rows).

    Fails today: submit map absent (AttributeError) + strategy_id=None. The fix makes
    the mapped strategy's ``closed_net_pnl_usd`` reflect the reconciled close.
    """
    _record_submit_strategy("mxcE2eClose", "e2e")
    pnl_ledger.reconcile_from_order_history(
        [_close_row("mxcE2eClose", ord_id="e2eClose", pnl="250.0")], "okx", "demo"
    )

    agg = pnl_ledger.aggregate_strategy_pnl("e2e", mode="demo")
    assert agg["closed_net_pnl_usd"] != 0


# ---------------------------------------------------------------------------
# H3 — TOCTOU duplicate write
# ---------------------------------------------------------------------------

def test_append_cross_process_toctou_no_duplicate(ledger_dir, monkeypatch):
    """Proves: a stale key-load (as a concurrent peer would produce between
    key-read and lock) must NOT let the same composite key land twice.

    We simulate the race with a monkeypatch rather than real processes:
    ``_load_existing_keys`` always returns an empty set (a maximally stale read), so
    the write-side dedupe is blind to the already-persisted row. Two ``append_closed_trades``
    calls for the same key then both write.

    Fails today: ``read_closed_trades`` has NO read-side dedupe, so the ledger reports
    two rows for one key. The robust fix (read-side dedupe by composite key, which
    also fixes ``test_read_closed_trades_deduplicates_duplicate_rows_on_disk``) makes
    the reader collapse the duplicate back to one.
    """
    monkeypatch.setattr(pnl_ledger, "_load_existing_keys", lambda path: set())
    row = {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "race1",
           "mode": "demo", "strategy_id": "alpha", "net_realized_pnl": 10.0,
           "closed_at_ms": 100}

    pnl_ledger.append_closed_trades([dict(row)])
    pnl_ledger.append_closed_trades([dict(row)])  # stale read -> writes a duplicate

    rows = pnl_ledger.read_closed_trades()
    assert len(rows) == 1


def test_read_closed_trades_deduplicates_duplicate_rows_on_disk(ledger_dir):
    """Proves: a duplicate that somehow reached disk (legacy data or an H3 race) is
    NOT double-counted on read.

    Two byte-identical rows (same composite key) are written straight to the ledger
    file. ``aggregate_strategy_pnl`` must count the trade once and must not double its
    P&L.

    Fails today: no read-side dedupe -> ``closed_order_count == 2`` and P&L doubled to
    20.0. The fix dedupes by composite key on read.
    """
    dup = {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "ord_id": "dup1",
           "mode": "demo", "strategy_id": "dup", "pnl_gross": 10.0, "fee_cost": 0.0,
           "net_realized_pnl": 10.0, "closed_at_ms": 100}
    ledger_dir.write_text(
        json.dumps(dup) + "\n" + json.dumps(dup) + "\n", encoding="utf-8"
    )

    agg = pnl_ledger.aggregate_strategy_pnl("dup")
    assert agg["closed_order_count"] == 1
    assert agg["closed_net_pnl_usd"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# H1 — legacy hardcoded ("okx","demo") reconcile literal
# ---------------------------------------------------------------------------

def test_okx_execution_records_does_not_reconcile_to_okx_demo_literal(tmp_path, monkeypatch):
    """Proves: the order-history snapshot must NOT reconcile with hardcoded
    ("okx","demo") literals — that mislabels every non-OKX / live close.

    We record every ``reconcile_from_order_history`` call made during
    ``okx_order_history_snapshot(config)`` and assert none used the literal
    ("okx","demo"). Reconcile is imported inside the function (``from pnl_ledger
    import ...``), so patching the attribute on ``pnl_ledger`` intercepts it.

    Fails today: ``dashboard.py:1105`` calls ``reconcile_from_order_history(rows,
    "okx", "demo")`` unconditionally. The fix threads the real (venue, mode) (as
    ``strategy_order_history_snapshot`` already does) or drops the legacy path.
    """
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    import dashboard

    calls: list[tuple] = []

    def _spy_reconcile(rows, exchange, mode):
        calls.append((exchange, mode))
        return 0

    monkeypatch.setattr(pnl_ledger, "reconcile_from_order_history", _spy_reconcile)

    class _FakeExec:
        def get_order_history_raw(self, inst_ids, limit=100):
            return [_close_row("mxcLegacy", ord_id="legacy")]

    monkeypatch.setattr(dashboard, "_dashboard_executor",
                        lambda config, **kw: (_FakeExec(), None))
    dashboard._OKX_ORDER_HISTORY_CACHE.clear()

    dashboard.okx_order_history_snapshot({"instrument": {"exchange": "okx"}})

    assert calls, "expected okx_order_history_snapshot to invoke reconcile"
    assert not any(exchange == "okx" and mode == "demo" for exchange, mode in calls), (
        f"reconcile was called with hardcoded ('okx','demo') literals: {calls}"
    )


# ---------------------------------------------------------------------------
# H2 — silent $0 for an unknown venue's realized P&L
# ---------------------------------------------------------------------------

def test_normalized_realized_pnl_none_triggers_log_not_silent_zero(ledger_dir, caplog):
    """Proves: when realized P&L is unknown for a venue (e.g. bybit exposes none in
    order history), reconcile LOGS the gap before writing — it must not silently
    persist an ambiguous row with no trace.

    The written row's ``pnl_gross`` stays ``None`` (distinct from a real 0.0), which is
    already correct today; the missing behavior is the log.

    Fails today: no log is emitted for the None-P&L close. The fix warns (on
    ``pnl_ledger``) that realized P&L was unavailable for the venue.
    """
    row = _close_row("mxcBybit", inst_id="BTCUSDT", ord_id="bybitClose")
    row["pnl"] = None  # bybit exposes no realized pnl in order history
    row.pop("fee", None)

    with caplog.at_level(logging.DEBUG, logger="pnl_ledger"):
        written = pnl_ledger.reconcile_from_order_history([row], "bybit", "live")

    assert written == 1
    ledger_row = pnl_ledger.read_closed_trades()[0]
    # None, not 0.0 -> the "unknown" state is preserved, not masked as a real zero.
    assert ledger_row["pnl_gross"] is None

    pnl_logs = [r for r in caplog.records if r.name == "pnl_ledger"]
    assert any(
        "pnl" in r.getMessage().lower()
        and any(k in r.getMessage().lower()
                for k in ("none", "unknown", "missing", "unsupported", "unavailable", "gross"))
        for r in pnl_logs
    ), f"expected a log when realized pnl is None for an unsupported venue; got {[r.getMessage() for r in pnl_logs]}"
