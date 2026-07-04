"""Durable closed-trade ledger for P&L accounting.

Append-only WAL stored at ``HERMX_DATA_DIR / "closed-trades.jsonl"``. Deduped by
the composite key ``(exchange, inst_id, ord_id, mode)``. Never pruned — it is the
lifetime realized-P&L record (P&L Master Plan Principle 10: the ledger must not
inherit the raw-webhook WAL's size-rotation).

Paths resolve at call time from the environment (HERMX_DATA_DIR -> HERMX_ROOT ->
repo root), mirroring dashboard.py, so tests that rebind the root via env need no
module reload.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

# Repo root == parent of this file's directory (src/). Mirrors dashboard.REPO_ROOT.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Cross-thread guard around the append critical section. flock() adds the
# cross-process guard so concurrent receiver/dashboard writers can't interleave.
_LOCK = threading.Lock()

# Ledger row schema version. v1 rows have no ``net_realized_pnl``/``schema_version``;
# ``read_closed_trades`` back-fills net for them on read (Phase 2). Bump on any
# breaking row-shape change so consumers can branch on it.
SCHEMA_VERSION = 2

# Per-venue knowledge: does the exchange's 'pnl' / 'realizedPnl' field already
# include trading fees (net), or is it gross P&L before fees?
# Default is False (gross) for safety — we add fees ourselves.
# When set to True, pnl_gross is already net and fee_cost is additive overhead.
# OKX ships raw ``info.pnl`` (gross); ``order['fee'].cost`` is signed negative for
# paid fees (okx.py:2405), so ``net = gross + fee`` is the right sign convention.
# Each venue's value is pending empirical verification (Phase 2 Open Decision).
ORDER_PNL_IS_NET = {
    "okx": False,      # OKX info.pnl is gross; fees are separate
    "hyperliquid": False,
    "binance": False,
    "bybit": False,
    # Add others as validated empirically
}


def _compute_net_realized(
    pnl_gross: float | None, fee_cost: float | None, exchange_id: str
) -> float | None:
    """Compute net realized P&L from gross and fee.

    * ``pnl_gross`` is None -> return None (net is unknown).
    * ``fee_cost`` is None -> return ``pnl_gross`` (missing fee treated as zero,
      not as "unknown net"; best available figure).
    * ``ORDER_PNL_IS_NET[exchange_id]`` True -> the exchange already deducted fees
      from ``pnl_gross``, so net == ``pnl_gross`` (fee is extra overhead already
      accounted for).
    * otherwise (default) -> ``pnl_gross + fee_cost`` (fee_cost signed, negative
      when paid).
    """
    if pnl_gross is None:
        return None
    if fee_cost is None:
        return pnl_gross
    if ORDER_PNL_IS_NET.get(exchange_id, False):
        # Exchange already netted fees into the P&L figure.
        return pnl_gross
    # Default: pnl_gross is before fees; add signed fee to get net.
    return pnl_gross + fee_cost


def _data_dir() -> Path:
    """Resolve the writable data dir the ledger lives under (call-time env read)."""
    return Path(
        os.environ.get("HERMX_DATA_DIR")
        or os.environ.get("HERMX_ROOT")
        or _REPO_ROOT
    )


def _ledger_path() -> Path:
    return _data_dir() / "closed-trades.jsonl"


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _composite_key(row: dict) -> tuple:
    return (row.get("exchange"), row.get("inst_id"), row.get("ord_id"), row.get("mode"))


def is_hermx_cl_ord_id(cl_ord_id: str | None) -> bool:
    """Return True if the client order id was issued by HermX (attribution gate)."""
    if not cl_ord_id:
        return False
    text = str(cl_ord_id)
    # OKX/Binance/Bybit: raw ``mxc`` prefix minted by the executor.
    if text.startswith("mxc"):
        return True
    # Operator-initiated close.
    if text.startswith("operator_close_"):
        return True
    # TODO(Phase 3, Decision 4): Hyperliquid uses numeric cloids — resolve via the
    # submit-time cloid->clOrdId map instead of a prefix check.
    return False


def read_closed_trades(
    since_ms: int | None = None, strategy_id: str | None = None
) -> list[dict]:
    """Read ledger entries, oldest-first, tolerant of corrupt/partial lines.

    Args:
        since_ms: when set, drop rows whose ``closed_at_ms`` is strictly older.
        strategy_id: when set, keep only rows with a matching ``strategy_id``.
    """
    path = _ledger_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # corrupt-tolerant: skip garbage, never raise
            if not isinstance(row, dict):
                continue
            if since_ms is not None:
                row_ts = row.get("closed_at_ms")
                if row_ts is not None and row_ts < since_ms:
                    continue
            if strategy_id is not None and row.get("strategy_id") != strategy_id:
                continue
            # Back-fill net for v1 rows (written before Phase 2 had the field).
            # Derived on read from stored gross+fee; never persisted here so the
            # ledger stays append-only and the computation stays rollback-safe.
            if "net_realized_pnl" not in row:
                row["net_realized_pnl"] = _compute_net_realized(
                    _as_float(row.get("pnl_gross")),
                    _as_float(row.get("fee_cost")),
                    row.get("exchange"),
                )
            rows.append(row)
    return rows


def net_realized_for_strategy(strategy_id: str, mode: str | None = None) -> float:
    """Sum net realized P&L for a strategy (optionally scoped to a mode).

    None nets count as zero so a partially-unknown history still sums cleanly.
    """
    rows = read_closed_trades(strategy_id=strategy_id)
    if mode is not None:
        rows = [r for r in rows if r.get("mode") == mode]
    return sum((r.get("net_realized_pnl") or 0.0) for r in rows)


def _load_existing_keys(path: Path) -> set:
    """Load the composite keys already persisted, for dedupe on append."""
    if not path.exists():
        return set()
    keys: set = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                keys.add(_composite_key(row))
    return keys


def append_closed_trades(entries: list[dict]) -> int:
    """Atomically append deduped entries to the ledger. Returns count written."""
    if not entries:
        return 0
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        # Re-read existing keys inside the lock so a concurrent writer's rows are
        # visible (append-only file; last reader before we write wins on dedupe).
        existing_keys = _load_existing_keys(path)
        new_lines: list[str] = []
        for entry in entries:
            key = _composite_key(entry)
            if key in existing_keys:
                continue
            new_lines.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            existing_keys.add(key)
        if not new_lines:
            return 0
        with open(path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                for line in new_lines:
                    handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return len(new_lines)


def _row_ts(row: dict) -> int:
    """Best-effort ms timestamp for chronological ordering (uTime then cTime)."""
    for field in ("uTime", "cTime", "closed_at_ms"):
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _build_entry(row: dict, exchange_id: str, mode: str, side: str, filled: float) -> dict:
    """Project an exchange order-history row onto the durable ledger schema."""
    # Prefer the adapter-normalized realized P&L; fall back to the OKX-native pnl.
    pnl_gross = _as_float(row.get("realized_pnl"))
    if pnl_gross is None:
        pnl_gross = _as_float(row.get("pnl"))
    fee_cost = _as_float(row.get("fee"))
    cl_ord_id = row.get("clOrdId") or row.get("clientOrderId")
    return {
        "schema_version": SCHEMA_VERSION,
        "exchange": exchange_id,
        "inst_id": row.get("instId") or row.get("inst_id"),
        "ord_id": row.get("ordId") or row.get("id"),
        "mode": mode,
        # Strategy attribution is best-effort in Phase 1 (hardened in Phase 3 via
        # the submit-time cl_ord_id map). None when not derivable from the row.
        "strategy_id": row.get("strategy_id"),
        "side": side,
        "filled_qty": filled,
        "avg_px": _as_float(row.get("avgPx")),
        "pnl_gross": pnl_gross,
        "fee_cost": fee_cost,
        "fee_currency": row.get("feeCcy"),
        # Phase 2: fee-correct net, computed from gross+fee per venue semantics.
        # Gross stays the displayed value for now (Decision ②: verify net later).
        "net_realized_pnl": _compute_net_realized(pnl_gross, fee_cost, exchange_id),
        "closed_at_ms": _row_ts(row),
        "cl_ord_id": cl_ord_id,
    }


def reconcile_from_order_history(
    history_rows: list[dict], exchange_id: str, mode: str
) -> int:
    """Extract HermX close rows from exchange order history and append to the ledger.

    Close detection uses **position-delta**, not just ``reduceOnly`` (Decision 3),
    so it works for spot venues that never set reduceOnly:

    * ``reduceOnly`` truthy -> close.
    * the running net position for the instrument was non-zero and this fill is on
      the opposite side (i.e. it reduces the position) -> close.

    The running position is tracked across *all* rows (HermX and external) so the
    delta is accurate, but only HermX-attributed closes are written to the ledger.

    Args:
        history_rows: raw order history (from ``get_order_history_raw``).
        exchange_id: venue id, e.g. "okx", "binance", "bybit".
        mode: "demo" or "live".

    Returns:
        Number of new ledger entries written.
    """
    entries: list[dict] = []
    positions: dict = {}  # inst_id -> signed running qty
    # Chronological order so position-delta detection sees opens before closes.
    for row in sorted(history_rows or [], key=_row_ts):
        inst_id = row.get("instId") or row.get("inst_id")
        side = str(row.get("side") or "").lower()
        filled = _as_float(row.get("accFillSz")) or _as_float(row.get("fillSz")) \
            or _as_float(row.get("filled")) or 0.0
        prev = positions.get(inst_id, 0.0)
        signed = filled if side == "buy" else -filled
        positions[inst_id] = prev + signed

        reduce_only = row.get("reduceOnly")
        if isinstance(reduce_only, str):
            reduce_only = reduce_only.strip().lower() == "true"
        opposite_side = (prev > 0 and side == "sell") or (prev < 0 and side == "buy")
        is_close = bool(reduce_only) or (prev != 0.0 and opposite_side)
        if not is_close:
            continue

        cl_ord_id = row.get("clOrdId") or row.get("clientOrderId")
        if not is_hermx_cl_ord_id(cl_ord_id):
            continue  # attribution: skip external / venue-native closes

        entries.append(_build_entry(row, exchange_id, mode, side, filled))

    return append_closed_trades(entries)
