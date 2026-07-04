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

# Venue order states that are NOT yet terminal — an in-flight partial or resting
# order whose accFillSz is not final. Ledgering such a row would freeze a partial
# fill that the append-only dedupe never updates once the order fully fills, so
# reconcile skips these (unless reduceOnly marks the row an explicit close). A
# conservative allowlist of known-non-terminal states: anything else (including a
# missing state field) fails open — we assume terminal when we don't know.
_NON_TERMINAL_ORDER_STATES = frozenset(
    {"live", "partially_filled", "open", "new", "pending_new"}
)


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

    M1 review (net-safety after C1 attribution): the ``ORDER_PNL_IS_NET is False``
    branch below IS the correct per-venue guard and needs no change. It does NOT
    silently misapply an unverified fee — ``fee_cost`` is the venue's own
    already-signed fee from order history, and ``False`` correctly means "gross
    figure, subtract the signed fee". What is per-venue-unverified is only whether
    the exchange's ``pnl`` already nets fees; that is exactly what the flag encodes,
    and it defaults ``False`` (gross) for every venue. Net is not *displayed* as
    authoritative until ``ORDER_PNL_IS_NET`` is flipped per venue after an empirical
    fee-sign check (gross stays the shown value). This behavior is test-locked by
    ``test_pnl_net.py`` (net == gross + signed fee for all default venues), so the
    fix here is a documented no-op — the safety lives in the flag + the display
    layer, not in re-deriving net.
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


def _ledger_external_fills_enabled() -> bool:
    """B3 flag (read at CALL time): when armed, reconcile ledgers external/manual
    closes (no HermX cl_ord_id) with ``strategy_id=None, source="external"`` instead of
    dropping them. Default OFF preserves today's attribution-only semantics."""
    return str(os.environ.get("HERMX_LEDGER_EXTERNAL_FILLS", "")).strip().lower() in {"1", "true", "yes"}


def check_overfill(inst_id, filled_qty, ordered_qty, tolerance: float = 1.01) -> bool:
    """OBSERVE-ONLY invariant (Opp 10, folded into B1): a close fill must not exceed the
    ordered size beyond a small tolerance.

    Logs a WARNING and returns True when ``filled_qty > ordered_qty * tolerance`` (1%
    by default). NEVER blocks reconcile or execution -- pure observation. An
    unknown/non-positive ordered size is a no-op (can't judge)."""
    f = _as_float(filled_qty)
    o = _as_float(ordered_qty)
    if f is None or o is None or o <= 0:
        return False
    if f > o * tolerance:
        logger.warning("overfill detected: %s filled=%.8g ordered=%.8g", inst_id, f, o)
        return True
    return False


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
    # Operator-initiated close: ``opcls{sha256hex}`` (OKX-safe, current) or the
    # legacy readable ``operator_close_...`` form still present in old ledger rows.
    if text.startswith("opcls"):
        return True
    if text.startswith("operator_close_"):
        return True
    # Hyperliquid rewrites the submitted clientOrderId into a numeric/hex cloid, so
    # the raw ``mxc`` prefix is gone on read-back (Phase 7b). Resolve the cloid
    # against the submit-time map instead of a prefix check.
    if str(exchange_id or "").lower() == "hyperliquid" and (text.startswith("0x") or text.isdigit()):
        from pnl_cloid_map import resolve_cloid
        return resolve_cloid(text, "hyperliquid") is not None
    return False


def record_submit_strategy(
    cl_ord_id: str,
    strategy_id: str,
    venue: str | None = None,
    mode: str | None = None,
) -> None:
    """Record a submit-time ``cl_ord_id -> strategy_id`` mapping (C1).

    Thin re-export of :func:`pnl_strategy_map.record_submit_strategy` so callers
    (and tests) can reach the writer through the ``pnl_ledger`` surface. Lazy
    import keeps the two modules decoupled. Best-effort: a map-write failure must
    never block a trade, so the caller wraps this in try/except.
    """
    from pnl_strategy_map import record_submit_strategy as _record
    _record(cl_ord_id, strategy_id, venue, mode)


def _parse_operator_close_strategy_id(
    cl_ord_id: str | None, inst_id: str | None = None
) -> str | None:
    """Recover the strategy id from a LEGACY ``operator_close_{symbol}_{sid}_{UTCday}`` id.

    Historical-rows-only fallback: current operator closes mint an opaque
    ``opcls{sha256hex}`` id that encodes nothing recoverable — those return None
    here and are attributed solely via the submit-time map (checked BEFORE this
    parser by the caller).

    The UTC day is always exactly 8 digits (``YYYYMMDD``); everything between the
    ``operator_close_`` prefix and that trailing date is ``{symbol}_{sid}``. Both
    ``symbol`` and ``sid`` can contain underscores, so a naive ``rsplit`` is
    ambiguous. We peel the symbol using the row's own ``inst_id`` (normalized to
    the underscore form the id was minted with), leaving the remainder as the sid.

    Falls back to a submit-map progressive-prefix probe when the inst_id doesn't
    line up, and returns None when still ambiguous (preserving the "unattributed
    but persisted" invariant rather than guessing wrong).
    """
    if not cl_ord_id or not str(cl_ord_id).startswith("operator_close_"):
        return None
    body = str(cl_ord_id)[len("operator_close_"):]
    parts = body.split("_")
    # Trailing tokens carry the UTC day (8 digits). Legacy ids end at the day;
    # newer ids append a finer ``_{HHMMSS}`` (6 digits) debounce token after it so
    # two distinct same-day closes get distinct ids. Locate the 8-digit day token in
    # both shapes; everything before it is "{symbol}_{sid}".
    if len(parts) >= 2 and len(parts[-1]) == 8 and parts[-1].isdigit():
        middle_parts = parts[:-1]
    elif (
        len(parts) >= 3
        and len(parts[-1]) == 6 and parts[-1].isdigit()
        and len(parts[-2]) == 8 and parts[-2].isdigit()
    ):
        middle_parts = parts[:-2]
    else:
        return None
    middle = "_".join(middle_parts)  # "{symbol}_{sid}"
    # Primary: peel the symbol prefix using the row's instrument id.
    if inst_id:
        sym_us = str(inst_id).replace("-", "_").replace("/", "_")
        prefix = sym_us + "_"
        if middle.upper().startswith(prefix.upper()):
            sid = middle[len(prefix):]
            if sid:
                return sid
    # Fallback: probe the submit map with progressively shorter sid suffixes
    # (longest-first), i.e. treat more of the middle as symbol until a mapped
    # cl_ord_id's strategy matches. Only accepts an exact recorded mapping.
    from pnl_strategy_map import _load_map
    mapped = _load_map()
    if cl_ord_id in mapped:
        return mapped[cl_ord_id]
    return None


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
            # Back-fill B3 attribution ``source`` for v1/v2/v3 rows written before the
            # field existed: every such close is a HermX-attributed one (externals were
            # dropped pre-B3), so it reads back as "hermx". Read-only, never persisted.
            if "source" not in row:
                row["source"] = "hermx"
            rows.append(row)
    # Read-side dedupe by composite key (last occurrence wins). Backstop against
    # any duplicate that reached disk — legacy data or a pre-fix TOCTOU race. The
    # append path's flock now prevents *new* dupes; this collapses old ones so a
    # single logical close is never double-counted on read (Test H3). A row whose
    # composite key is fully degenerate ((None, None, None, None)) is malformed, not
    # a real duplicate — keep every such row rather than collapsing them into one.
    deduped: dict = {}
    malformed: list = []
    for row in rows:
        key = _composite_key(row)
        if all(part is None for part in key):
            malformed.append(row)
            continue
        deduped[key] = row
    return list(deduped.values()) + malformed


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


def reconcile_health_stats() -> dict:
    """Read-only reconcile-health view for the ``/api`` payload and the lag gate.

    * ``max_recorded_at_ms`` — newest local observation time (``recorded_at_ms``,
      schema v3+) across the ledger, or ``None`` when no row carries one.
    * ``reconcile_lag_ms`` — ``now_ms - max_recorded_at_ms``, or ``None`` when the
      max is unknown.
    * ``recorded_at_rows_pct`` — fraction of rows carrying ``recorded_at_ms``
      (0.0-1.0), or ``None`` on an empty ledger.
    """
    rows = read_closed_trades()
    if not rows:
        return {
            "max_recorded_at_ms": None,
            "reconcile_lag_ms": None,
            "recorded_at_rows_pct": None,
        }
    recorded_count = 0
    best = None
    for row in rows:
        ts = row.get("recorded_at_ms")
        if ts is None:
            continue
        recorded_count += 1
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        if best is None or ts > best:
            best = ts
    now_ms = int(time.time() * 1000)
    return {
        "max_recorded_at_ms": best,
        "reconcile_lag_ms": (now_ms - best) if best is not None else None,
        "recorded_at_rows_pct": recorded_count / len(rows),
    }


def external_fills_count(mode: str | None = None) -> int:
    """B3: count ledgered external / manual closes (``source == "external"``).

    These carry ``strategy_id=None`` so they are excluded from per-strategy sums; this
    account-level count lets the portfolio view surface how many out-of-band closes
    landed in the ledger. Read-only; optionally scoped to a ``mode`` (demo|live)."""
    rows = read_closed_trades()
    if mode is not None:
        rows = [r for r in rows if r.get("mode") == mode]
    return sum(1 for r in rows if r.get("source") == "external")


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


def _live_upl_usd(venue: str, mode: str) -> float | None:
    """Best-effort account-level unrealized P&L for one (venue, mode), or None.

    Builds the receiver's read-only reconciliation executor through the ``_wr``
    seam (the same (venue, simulated_trading) intent resolution as the #20a
    reconcile path) and sums ``upl`` across the account's positions. ANY
    failure — no factory, venue error, missing seam — returns None so the
    caller degrades to the closed-only estimate. Never raises.
    """
    try:
        import webhook_receiver as _wr
        executor = _wr._reconciliation_executor(
            {"venue": venue, "simulated_trading": mode != "live"}
        )
        if executor is None:
            return None
        vals = [
            _as_float(row.get("upl"))
            for row in (executor.get_positions() or [])
            if isinstance(row, dict)
        ]
        return sum(v for v in vals if v is not None)
    except Exception as exc:
        logger.warning("live UPL read failed for %s/%s: %s", venue, mode, exc)
        return None


def _account_equity_estimate(venue: str, mode: str) -> float | None:
    """B1 equity assembler (NAUTILUS_GAP_REMEDIATION_PLAN.md §0.6 item 4.3):
    HermX's synthetic account equity for one (venue, mode) pair — the
    ``expected_equity`` input ``check_balance_drift`` compares the real venue
    balance against.

    Sums, over every loaded strategy belonging to the pair, the seed
    ``budget_usd`` plus that strategy's closed-net realized P&L from this ledger
    (full history, no accounting window — the venue balance reflects all
    history, not the display window; external ``strategy_id=None`` closes are
    likewise out of scope, matching per-strategy attribution). Live UPL is
    best-effort on top: any unavailability degrades to the closed-only figure
    with the omission logged — never raises, never blocks.

    Strategy membership resolves EXACTLY like ``active_venue_modes()`` (the B1
    enumerator): venue from ``strategy_instrument()["exchange"]`` (lowercased;
    unresolvable -> skipped, fail closed) and mode through
    ``strategy.readiness.effective_execution_mode`` (control-state override
    included), collapsed to the same live-vs-not bool — so this figure and the
    enumerated drift-check domain can never disagree. Returns None only when NO
    loaded strategy matches the pair (nothing to estimate). Imports are lazy:
    strategy.readiness imports this module, and STRATEGIES is root-bound /
    reload-reset receiver state (read via ``import webhook_receiver as _wr``).
    """
    from strategy.readiness import effective_execution_mode
    from strategy.records import strategy_budget_usd, strategy_instrument
    import webhook_receiver as _wr

    venue_key = str(venue or "").strip().lower()
    live_wanted = str(mode or "").strip().lower() == "live"
    matching: dict = {}
    for sid, strategy in (getattr(_wr, "STRATEGIES", None) or {}).items():
        s_venue = str((strategy_instrument(strategy) or {}).get("exchange") or "").strip().lower()
        if not s_venue or s_venue != venue_key:
            continue
        if (effective_execution_mode(strategy, sid) == "live") != live_wanted:
            continue
        matching[str(sid)] = strategy
    if not matching:
        return None
    ledger_mode = "live" if live_wanted else "demo"  # ledger mode column is demo|live
    budgets = sum(strategy_budget_usd(s) for s in matching.values())
    closed_net = sum(
        (row.get("net_realized_pnl") or 0.0)
        for row in read_closed_trades()
        if row.get("strategy_id") in matching
        and row.get("mode") == ledger_mode
        and str(row.get("exchange") or "").strip().lower() == venue_key
    )
    equity = float(budgets) + float(closed_net)
    upl = _live_upl_usd(venue_key, ledger_mode)
    if upl is None:
        logger.info(
            "equity estimate %s/%s: live UPL unavailable; closed-only figure %.8g",
            venue_key, ledger_mode, equity,
        )
        return equity
    return equity + float(upl)


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
    written = 0
    with _LOCK:
        # Open "a+" and take the exclusive flock BEFORE reading existing keys, so
        # the whole read-modify-write is atomic across processes. Previously the
        # key-read ran outside the lock: two concurrent writers could both load a
        # stale key set and each append the same ordId (TOCTOU). The flock now
        # serializes the full cycle; _load_existing_keys re-reads the file by path
        # while we hold the lock (also keeps it monkeypatchable for the race test).
        with open(path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                existing_keys = _load_existing_keys(path)
                new_lines: list[str] = []
                for entry in entries:
                    key = _composite_key(entry)
                    if key in existing_keys:
                        continue
                    new_lines.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
                    existing_keys.add(key)
                if new_lines:
                    handle.seek(0, os.SEEK_END)
                    for line in new_lines:
                        handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    written = len(new_lines)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return written


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
    inst_id = row.get("instId") or row.get("inst_id")
    ord_id = row.get("ordId") or row.get("id")
    # Prefer the adapter-normalized realized P&L; fall back to the OKX-native pnl.
    pnl_gross = _as_float(row.get("realized_pnl"))
    if pnl_gross is None:
        pnl_gross = _as_float(row.get("pnl"))
    if pnl_gross is None:
        # The venue exposed no realized P&L for this close (e.g. bybit in order
        # history). Persist the row with gross=None (an honest "unknown", distinct
        # from a real 0.0) but warn: a genuine close with no PnL signal must not be
        # indistinguishable from a real break-even zero.
        logger.warning(
            "pnl_gross_is_none inst_id=%s ord_id=%s filled=%.8g — writing zero pnl, verify venue",
            inst_id, ord_id, filled,
        )
    fee_cost = _as_float(row.get("fee"))
    fee_currency = row.get("feeCcy")
    # A fee paid in a currency other than the instrument's quote (e.g. BNB, base
    # asset) is not USD and must not be summed into closed_fees_usd as if it were.
    # We can't FX-convert here, so warn for operator awareness and still persist the
    # row — the mismatched fee is simply excluded from the USD total downstream.
    if fee_cost and fee_currency and inst_id:
        _parts = str(inst_id).split("-")
        quote = _parts[1].upper() if len(_parts) > 1 else None
        if quote and str(fee_currency).upper() != quote:
            logger.warning(
                "fee_currency_mismatch inst_id=%s fee_currency=%s quote=%s fee_cost=%.8g"
                " — fee excluded from USD total",
                inst_id, fee_currency, quote, fee_cost,
            )
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
    # Strategy attribution (C1): exchange rows carry no strategy_id, so resolve it
    # from the submit-time cl_ord_id -> strategy_id map (written when both were
    # known). For Hyperliquid, cl_ord_id has already been resolved to the original
    # mxc id above, so the map lookup is a direct hop. LEGACY operator-initiated
    # closes encoded the strategy in the cl_ord_id itself -> parse those as a
    # fallback; current ``opcls`` hash ids carry nothing parseable and resolve via
    # the map alone. When nothing resolves, strategy_id stays None (the row still
    # persists; only its per-strategy attribution is best-effort).
    strategy_id = row.get("strategy_id")
    if strategy_id is None and cl_ord_id:
        from pnl_strategy_map import resolve_strategy as _resolve_strategy
        strategy_id = _resolve_strategy(cl_ord_id)
    if strategy_id is None and cl_ord_id and str(cl_ord_id).startswith("operator_close_"):
        strategy_id = _parse_operator_close_strategy_id(cl_ord_id, inst_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "exchange": exchange_id,
        "inst_id": inst_id,
        "ord_id": ord_id,
        "mode": mode,
        # Strategy attribution resolved above from the submit-time cl_ord_id map
        # (C1), or parsed from an operator_close cl_ord_id. None when not derivable.
        "strategy_id": strategy_id,
        "side": side,
        "filled_qty": filled,
        "avg_px": _as_float(row.get("avgPx")),
        "pnl_gross": pnl_gross,
        "fee_cost": fee_cost,
        "fee_currency": fee_currency,
        # Phase 2: fee-correct net, computed from gross+fee per venue semantics.
        # Gross stays the displayed value for now (Decision ②: verify net later).
        "net_realized_pnl": _compute_net_realized(pnl_gross, fee_cost, exchange_id),
        "closed_at_ms": _row_ts(row),
        "recorded_at_ms": int(time.time() * 1000),
        "cl_ord_id": cl_ord_id,
        # B3 attribution tag: a HermX-issued close is "hermx"; an external/manual close
        # (ledgered only when HERMX_LEDGER_EXTERNAL_FILLS is armed) is overwritten to
        # "external" by the caller. Legacy rows without this key read back as "hermx".
        "source": "hermx",
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

        # Opp 10 (folded into B1): observe-only overfill invariant. A close whose filled
        # size exceeds the ordered size (beyond tolerance) logs a WARNING but is never
        # blocked. Runs for every close (HermX + external) before attribution.
        check_overfill(inst_id, filled, row.get("sz"))

        # Terminal-only ledgering: skip an in-flight partial/resting fill whose state
        # is not yet terminal, UNLESS reduceOnly says the venue explicitly marked it a
        # close (an explicit close is processed regardless of state). A missing/None
        # state fails open (assume terminal). Prevents a partial accFillSz from being
        # frozen into the append-only ledger before the order fully fills.
        state = row.get("state")
        if not reduce_only and state is not None \
                and str(state).lower() in _NON_TERMINAL_ORDER_STATES:
            logger.debug(
                "skipping_nonterminal_row inst_id=%s ord_id=%s state=%s",
                inst_id, row.get("ordId") or row.get("id"), state,
            )
            continue

        cl_ord_id = row.get("clOrdId") or row.get("clientOrderId")
        if not is_hermx_cl_ord_id(cl_ord_id, exchange_id):
            # B3: an external / venue-native close. Default -> dropped (unchanged). When
            # HERMX_LEDGER_EXTERNAL_FILLS is armed, ledger it explicitly with
            # strategy_id=None + source="external" so lifetime realized P&L reconciles to
            # the account after out-of-band operator action. Composite-key dedup keeps
            # re-runs idempotent; strategy_id=None keeps it out of per-strategy sums.
            if _ledger_external_fills_enabled():
                external = _build_entry(row, exchange_id, mode, side, filled)
                external["strategy_id"] = None
                external["source"] = "external"
                entries.append(external)
            continue

        entries.append(_build_entry(row, exchange_id, mode, side, filled))

    return append_closed_trades(entries)
