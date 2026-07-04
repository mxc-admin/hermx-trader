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
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo root == parent of this file's directory (src/). Mirrors dashboard.REPO_ROOT.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Cross-thread guard around the append critical section. flock() adds the
# cross-process guard so concurrent receiver/dashboard writers can't interleave.
_LOCK = threading.Lock()

# Ledger row schema version. v1 rows have no ``net_realized_pnl``/``schema_version``;
# ``read_closed_trades`` back-fills net for them on read (Phase 2). Bump on any
# breaking row-shape change so consumers can branch on it.
SCHEMA_VERSION = 3

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

# Snap-to-zero tolerance for the per-poll signed-qty accumulator: successive
# float add/subtract can leave e.g. 1e-13 residue instead of exactly 0.0, which
# would spuriously satisfy ``prev != 0.0`` and mis-detect the next fill as a close.
QTY_EPS = 1e-9


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


def is_hermx_cl_ord_id(cl_ord_id: str | None, exchange_id: str | None = None) -> bool:
    """Return True if the client order id was issued by HermX (attribution gate).

    Args:
        cl_ord_id: the client order id from the exchange order history.
        exchange_id: venue identifier (e.g. "okx", "hyperliquid"); used to enable
            numeric/hex-cloid resolution on Hyperliquid.
    """
    if not cl_ord_id:
        return False
    text = str(cl_ord_id)
    # OKX/Binance/Bybit: raw ``mxc`` prefix minted by the executor.
    if text.startswith("mxc"):
        return True
    # Operator-initiated close.
    if text.startswith("operator_close_"):
        return True
    # Hyperliquid rewrites the submitted clientOrderId into a numeric/hex cloid, so
    # the raw ``mxc`` prefix is gone on read-back (Phase 7b). Resolve the cloid
    # against the submit-time map instead of a prefix check.
    if str(exchange_id or "").lower() == "hyperliquid" and (text.startswith("0x") or text.isdigit()):
        from pnl_cloid_map import resolve_cloid
        return resolve_cloid(text, "hyperliquid") is not None
    return False


def read_closed_trades(
    since_ms: int | None = None,
    strategy_id: str | None = None,
    accounting_start_at: int | None = None,
) -> list[dict]:
    """Read ledger entries, oldest-first, tolerant of corrupt/partial lines.

    Args:
        since_ms: when set, drop rows whose ``closed_at_ms`` is strictly older.
        strategy_id: when set, keep only rows with a matching ``strategy_id``.
        accounting_start_at: Phase-3 accounting window (ms). When set, P&L before
            this instant is "locked" — rows whose ``closed_at_ms`` predates it are
            dropped so a strategy's clean-window total ignores pre-reset history
            WITHOUT deleting it from the append-only ledger. Combined with
            ``since_ms`` by taking the later (stricter) of the two floors.
    """
    # One lower bound on closed_at_ms: the later of the freshness floor and the
    # accounting-window floor (both optional). max() of whichever are present.
    _floors = [v for v in (since_ms, accounting_start_at) if v is not None]
    effective_since = max(_floors) if _floors else None
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
            if effective_since is not None:
                row_ts = row.get("closed_at_ms")
                if row_ts is not None and row_ts < effective_since:
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
            # Back-fill recorded_at_ms (local ts_init) for v1/v2 rows as None on read
            # -- local observation time can't be recovered from stored rows. Same
            # read-only, never-persisted pattern as the net back-fill above (v3).
            if "recorded_at_ms" not in row:
                row["recorded_at_ms"] = None
            rows.append(row)
    return rows


def max_recorded_closed_at(exchange: str, mode: str) -> int | None:
    """Return the maximum closed_at_ms already ledgered for (exchange, mode), or None."""
    rows = read_closed_trades()
    best = None
    for row in rows:
        if row.get("exchange") != exchange or row.get("mode") != mode:
            continue
        ts = row.get("closed_at_ms")
        if ts is not None:
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                continue
            if best is None or ts > best:
                best = ts
    return best


def net_realized_for_strategy(
    strategy_id: str,
    mode: str | None = None,
    accounting_start_at: int | None = None,
) -> float:
    """Sum net realized P&L for a strategy (optionally scoped to a mode).

    None nets count as zero so a partially-unknown history still sums cleanly.
    ``accounting_start_at`` (ms) scopes the sum to the clean accounting window
    (Phase 3): closes before it are excluded but not deleted.
    """
    rows = read_closed_trades(
        strategy_id=strategy_id, accounting_start_at=accounting_start_at
    )
    if mode is not None:
        rows = [r for r in rows if r.get("mode") == mode]
    return sum((r.get("net_realized_pnl") or 0.0) for r in rows)


def aggregate_strategy_pnl(
    strategy_id: str,
    *,
    budget_usd: float = 0.0,
    mode: str | None = None,
    accounting_start_at: int | None = None,
    open_upl_usd: float = 0.0,
) -> dict:
    """The per-strategy P&L contract the API exposes (Phase 3).

    Sums the durable ledger's closed trades — scoped to the strategy, optionally a
    ``mode`` (demo|live), and the ``accounting_start_at`` clean window — and combines
    them with the live open UPnL to produce equity. Closed figures survive FLAT and
    the 100-row exchange history bound (they come from the ledger, not a live read).

    Additive/read-only: never mutates the ledger. Missing ledger -> all-zero, never an
    error (``test_strategy_pnl_absent_ledger``).
    """
    rows = read_closed_trades(
        strategy_id=strategy_id, accounting_start_at=accounting_start_at
    )
    if mode is not None:
        rows = [r for r in rows if r.get("mode") == mode]
    closed_net = sum((r.get("net_realized_pnl") or 0.0) for r in rows)
    closed_realized = sum((_as_float(r.get("pnl_gross")) or 0.0) for r in rows)
    closed_fees = sum((_as_float(r.get("fee_cost")) or 0.0) for r in rows)
    # Latest close instant within the window (ms epoch), or None when no rows. The
    # Phase-4 API contract surfaces it as ``last_close_at_ms`` so the UI can show
    # "as of" freshness without re-reading the ledger.
    last_close = max((_row_ts(r) for r in rows), default=0) or None
    budget = float(budget_usd or 0.0)
    upl = float(open_upl_usd or 0.0)
    return {
        "budget_usd": budget,
        "closed_realized_pnl_usd": closed_realized,
        "closed_fees_usd": closed_fees,
        "closed_net_pnl_usd": closed_net,
        "open_upl_usd": upl,
        "equity_now_usd": budget + closed_net + upl,
        "closed_order_count": len(rows),
        "last_close_at_ms": last_close,
        "accounting_start_at": accounting_start_at,
    }


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
    # Hyperliquid returns a numeric/hex cloid in place of the submitted mxc id; if
    # we recorded the mapping at submit time, store the original mxc id (Phase 7b).
    if str(exchange_id or "").lower() == "hyperliquid" and cl_ord_id:
        _text = str(cl_ord_id)
        if _text.startswith("0x") or _text.isdigit():
            from pnl_cloid_map import resolve_cloid
            resolved = resolve_cloid(_text, exchange_id)
            if resolved:
                cl_ord_id = resolved
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
        "recorded_at_ms": int(time.time() * 1000),
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
        new_pos = prev + signed
        if abs(new_pos) < QTY_EPS:
            new_pos = 0.0
        positions[inst_id] = new_pos
        if prev != 0.0 and ((prev > 0.0 and new_pos < 0.0) or (prev < 0.0 and new_pos > 0.0)):
            logger.warning(
                "reconcile_position_sign_flip inst_id=%s prev=%.8g new=%.8g side=%s filled=%.8g",
                inst_id, prev, positions[inst_id], side, filled,
            )

        reduce_only = row.get("reduceOnly")
        if isinstance(reduce_only, str):
            reduce_only = reduce_only.strip().lower() == "true"
        opposite_side = (prev > 0 and side == "sell") or (prev < 0 and side == "buy")
        is_close = bool(reduce_only) or (prev != 0.0 and opposite_side)
        if is_close and prev != 0.0 and not opposite_side:
            logger.warning(
                "reconcile_sign_guard_mismatch inst_id=%s prev=%.8g side=%s",
                inst_id, prev, side,
            )
        if not is_close:
            continue

        cl_ord_id = row.get("clOrdId") or row.get("clientOrderId")
        if not is_hermx_cl_ord_id(cl_ord_id, exchange_id):
            continue  # attribution: skip external / venue-native closes

        entries.append(_build_entry(row, exchange_id, mode, side, filled))

    return append_closed_trades(entries)
