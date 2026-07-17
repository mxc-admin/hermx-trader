"""Positions-first read model folded from the trade-leg ledger (pnl_ledger).

A *position episode* is one flat-to-flat round trip on an
``(exchange, mode, inst_id, strategy_id)`` key: open legs accumulate the entry,
close legs reduce it, and the episode closes when the running signed qty returns
to zero. Closed episodes carry realized P&L (sum of their close legs' net — the
same figures ``aggregate_strategy_pnl`` sums, so the strategy-card identity
holds); open episodes carry entry/qty and get live UPnL joined at the API layer.

Read-only over ``read_trade_legs``: never writes, never prunes. A missing ledger
folds to ``[]``.
"""
from __future__ import annotations

import logging

from pnl_ledger import QTY_EPS, _as_float, _usd_fee_cost, read_trade_legs

logger = logging.getLogger(__name__)


def _leg_ts(leg: dict) -> int:
    try:
        return int(leg.get("closed_at_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _new_episode(leg: dict) -> dict:
    return {
        "strategy_id": leg.get("strategy_id"),
        "venue": leg.get("exchange"),
        "mode": leg.get("mode"),
        "inst_id": leg.get("inst_id"),
        "side": None,
        "signed": 0.0,
        "open_qty": 0.0,
        "open_notional": 0.0,
        "open_px_qty": 0.0,
        "close_qty": 0.0,
        "close_notional": 0.0,
        "close_px_qty": 0.0,
        "opened_at_ms": None,
        "closed_at_ms": None,
        "pnl_gross": 0.0,
        "fees": 0.0,
        "pnl_net": 0.0,
        "open_leg_count": 0,
        "close_leg_count": 0,
    }


def _apply_open(ep: dict, leg: dict, qty: float, px: float | None) -> None:
    side = str(leg.get("side") or "").lower()
    ep["signed"] += qty if side == "buy" else -qty
    ep["open_qty"] += qty
    if px is not None:
        ep["open_notional"] += px * qty
        ep["open_px_qty"] += qty
    if ep["side"] is None:
        ep["side"] = "long" if side == "buy" else "short"
    ts = _leg_ts(leg)
    if ts and (ep["opened_at_ms"] is None or ts < ep["opened_at_ms"]):
        ep["opened_at_ms"] = ts
    ep["open_leg_count"] += 1


def _apply_close(ep: dict, leg: dict, qty: float, px: float | None) -> None:
    side = str(leg.get("side") or "").lower()
    ep["signed"] += qty if side == "buy" else -qty
    ep["close_qty"] += qty
    if px is not None:
        ep["close_notional"] += px * qty
        ep["close_px_qty"] += qty
    ts = _leg_ts(leg)
    if ts and (ep["closed_at_ms"] is None or ts > ep["closed_at_ms"]):
        ep["closed_at_ms"] = ts
    ep["pnl_gross"] += _as_float(leg.get("pnl_gross")) or 0.0
    ep["fees"] += _usd_fee_cost(leg) or 0.0
    ep["pnl_net"] += leg.get("net_realized_pnl") or 0.0
    ep["close_leg_count"] += 1


def _project(ep: dict, status: str) -> dict:
    entry_px = ep["open_notional"] / ep["open_px_qty"] if ep["open_px_qty"] > 0 else None
    exit_px = ep["close_notional"] / ep["close_px_qty"] if ep["close_px_qty"] > 0 else None
    # Closed qty = sum of close-leg fills (not the peak open size); open qty = the
    # remaining net exposure.
    qty = ep["close_qty"] if status == "closed" else abs(ep["signed"])
    return {
        "status": status,
        "strategy_id": ep["strategy_id"],
        "venue": ep["venue"],
        "mode": ep["mode"],
        "inst_id": ep["inst_id"],
        "side": ep["side"],
        "qty": qty,
        "entry_px": entry_px,
        "exit_px": exit_px if status == "closed" else None,
        "opened_at_ms": ep["opened_at_ms"],
        "closed_at_ms": ep["closed_at_ms"] if status == "closed" else None,
        "realized_pnl_gross": ep["pnl_gross"],
        "fees": ep["fees"],
        # For an open episode this is the realized-so-far figure from partial
        # closes — sum(closed) + sum(open realized) always equals the close-leg
        # aggregate, so nothing is ever hidden between the two views.
        "realized_pnl_net": ep["pnl_net"],
        "upl": None,
        "open_leg_count": ep["open_leg_count"],
        "close_leg_count": ep["close_leg_count"],
    }


def _build_episodes_from_legs(legs: list[dict]) -> list[dict]:
    episodes: list[dict] = []
    state: dict = {}
    for leg in sorted(legs or [], key=_leg_ts):
        key = (leg.get("exchange"), leg.get("mode"), leg.get("inst_id"), leg.get("strategy_id"))
        kind = leg.get("leg_kind") or "close"
        qty = _as_float(leg.get("filled_qty")) or 0.0
        px = _as_float(leg.get("avg_px"))
        ep = state.get(key)
        if kind == "open":
            if ep is None:
                ep = state[key] = _new_episode(leg)
            _apply_open(ep, leg, qty, px)
            continue
        if ep is None:
            # Orphan close (no prior open leg — legacy history or an externally
            # opened position): one immediately-closed single-leg episode so its
            # realized P&L still lands in exactly one closed row (identity).
            ep = _new_episode(leg)
            side = str(leg.get("side") or "").lower()
            ep["side"] = "short" if side == "buy" else "long"  # a sell closes a long
            _apply_close(ep, leg, qty, px)
            ep["signed"] = 0.0
            episodes.append(_project(ep, "closed"))
            continue
        before = ep["signed"]
        _apply_close(ep, leg, qty, px)
        after = ep["signed"]
        if abs(after) <= QTY_EPS:
            ep["signed"] = 0.0
            episodes.append(_project(ep, "closed"))
            del state[key]
        elif (before > 0 > after) or (before < 0 < after):
            # Reversal fill: the close leg overshot flat. Finalize the round trip
            # and start a fresh episode with the remainder as its entry.
            episodes.append(_project(ep, "closed"))
            fresh = _new_episode(leg)
            fresh["side"] = "long" if after > 0 else "short"
            fresh["signed"] = after
            fresh["open_qty"] = abs(after)
            if px is not None:
                fresh["open_notional"] = px * abs(after)
                fresh["open_px_qty"] = abs(after)
            fresh["opened_at_ms"] = _leg_ts(leg) or None
            fresh["open_leg_count"] = 1
            state[key] = fresh
    for ep in state.values():
        if abs(ep["signed"]) > QTY_EPS:
            episodes.append(_project(ep, "open"))
        elif ep["close_leg_count"] or ep["open_qty"] > 0:
            # Fully-offset legs that never crossed the close path (defensive).
            episodes.append(_project(ep, "closed"))
    return episodes


def list_positions(
    strategy_id: str | None = None,
    status: str | None = None,
    mode: str | None = None,
    venue: str | None = None,
    accounting_start_at: int | None = None,
) -> list[dict]:
    """Open + closed position episodes folded from the durable leg ledger.

    Filters are ANDed. ``accounting_start_at`` (ms) drops closed episodes whose
    final close predates the window; open episodes are always in scope. Sort:
    open episodes first, then each group newest-first (opened_at / closed_at
    desc) — the operator reads current exposure before history. Missing ledger
    -> ``[]``, never an error.
    """
    episodes = _build_episodes_from_legs(read_trade_legs())
    out = []
    for ep in episodes:
        if strategy_id is not None and ep.get("strategy_id") != strategy_id:
            continue
        if status is not None and ep.get("status") != status:
            continue
        if mode is not None and ep.get("mode") != mode:
            continue
        if venue is not None and ep.get("venue") != venue:
            continue
        if (
            accounting_start_at is not None
            and ep.get("status") == "closed"
            and (ep.get("closed_at_ms") or 0) < accounting_start_at
        ):
            continue
        out.append(ep)
    def _sort_ts(p):
        ts = p.get("opened_at_ms") if p.get("status") == "open" else p.get("closed_at_ms")
        return ts or 0

    out.sort(key=lambda p: (0 if p.get("status") == "open" else 1, -_sort_ts(p)))
    return out


def pnl_series(
    strategy_id: str | None = None,
    mode: str | None = None,
    accounting_start_at: int | None = None,
    max_points: int = 200,
) -> list[dict]:
    """Cumulative realized-net curve from CLOSED episodes (equity curve source).

    Points are closed episodes sorted by ``closed_at_ms`` ascending; ``cum_net``
    is the running sum of ``realized_pnl_net`` — the same figures the strategy
    aggregate sums, scoped by the same filters (strategy_id, mode,
    accounting_start_at). Open UPnL is deliberately NOT folded in. Capped to the
    most recent ``max_points`` points (cumulative sum still spans everything in
    the window). Missing ledger -> ``[]``.
    """
    closed = list_positions(
        strategy_id=strategy_id,
        status="closed",
        mode=mode,
        accounting_start_at=accounting_start_at,
    )
    closed.sort(key=lambda p: p.get("closed_at_ms") or 0)
    points = []
    cum = 0.0
    for ep in closed:
        cum += ep.get("realized_pnl_net") or 0.0
        points.append(
            {
                "closed_at_ms": ep.get("closed_at_ms"),
                "pnl_net": ep.get("realized_pnl_net") or 0.0,
                "cum_net": cum,
            }
        )
    if max_points and len(points) > max_points:
        points = points[-max_points:]
    return points


def diff_open_positions(
    ledger_open: list[dict], venue_open: list[dict], qty_tolerance: float = 0.01
) -> list[dict]:
    """OBSERVE-ONLY reconcile-by-position: ledger-derived open exposure vs the
    venue's live positions. Returns drift rows (warned by the caller); NEVER
    submits or cancels anything.

    Compared per ``(venue, mode, inst_id)`` with ledger qty summed across
    strategies. Kinds: ``ledger_open_venue_flat``, ``venue_open_ledger_unknown``,
    ``qty_mismatch`` (relative tolerance, 1% default)."""
    ledger_qty: dict = {}
    for p in ledger_open or []:
        key = (p.get("venue"), p.get("mode"), p.get("inst_id"))
        signed = (p.get("qty") or 0.0) * (1.0 if p.get("side") == "long" else -1.0)
        ledger_qty[key] = ledger_qty.get(key, 0.0) + signed
    venue_qty: dict = {}
    for p in venue_open or []:
        key = (p.get("venue"), p.get("mode"), p.get("inst_id"))
        signed = _as_float(p.get("qty")) or 0.0
        if str(p.get("side") or "").lower() in {"short", "sell"} and signed > 0:
            signed = -signed
        venue_qty[key] = venue_qty.get(key, 0.0) + signed
    drift = []
    for key, lq in ledger_qty.items():
        vq = venue_qty.get(key)
        row = {"venue": key[0], "mode": key[1], "inst_id": key[2],
               "ledger_qty": lq, "venue_qty": vq}
        if vq is None or abs(vq) <= QTY_EPS:
            drift.append({**row, "kind": "ledger_open_venue_flat"})
        elif abs(vq - lq) > abs(lq) * qty_tolerance:
            drift.append({**row, "kind": "qty_mismatch"})
    for key, vq in venue_qty.items():
        if abs(vq) <= QTY_EPS or key in ledger_qty:
            continue
        drift.append({"venue": key[0], "mode": key[1], "inst_id": key[2],
                      "ledger_qty": None, "venue_qty": vq,
                      "kind": "venue_open_ledger_unknown"})
    for row in drift:
        logger.warning("position_drift %s", row)
    return drift
