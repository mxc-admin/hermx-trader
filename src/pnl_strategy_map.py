"""Submit-time ``cl_ord_id -> strategy_id`` map for P&L attribution (C1).

At reconcile time, exchange order-history rows carry no ``strategy_id`` — so a
reconciled close would land with ``strategy_id=None`` and every per-strategy P&L
would sum to zero forever (the "realized P&L = $0" regression). The fix records
the mapping at SUBMIT time, when both the ``cl_ord_id`` and the ``strategy_id``
are known, and has reconcile resolve it.

File: ``HERMX_DATA_DIR / "cl-ord-strategy-map.jsonl"`` (append-only JSONL).
Schema: ``{"cl_ord_id", "strategy_id", "venue", "mode", "ts_ms"}``.
Dedup key: ``cl_ord_id`` — submit is one-time, so the FIRST record wins.

Paths resolve at call time from the environment (HERMX_DATA_DIR -> HERMX_ROOT ->
repo root), mirroring ``pnl_ledger`` / ``pnl_cloid_map`` so tests that rebind the
root via env need no module reload.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _map_path() -> Path:
    return Path(
        os.environ.get("HERMX_DATA_DIR")
        or os.environ.get("HERMX_ROOT")
        or _REPO_ROOT
    ) / "cl-ord-strategy-map.jsonl"


def _load_map() -> dict:
    """Load the full map as ``{cl_ord_id: strategy_id}`` (first record wins).

    Corrupt/partial lines are skipped (with a debug trace), never raised — the
    reconcile path must stay robust against a torn write.
    """
    path = _map_path()
    if not path.exists():
        return {}
    out: dict = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("cl-ord-strategy-map: skipping corrupt line")
                continue
            if not isinstance(entry, dict):
                continue
            cid = entry.get("cl_ord_id")
            sid = entry.get("strategy_id")
            if cid and cid not in out and sid is not None:
                out[cid] = sid  # first-wins: submit is one-time
    return out


def resolve_strategy(cl_ord_id: str | None) -> str | None:
    """Return the strategy_id recorded for a cl_ord_id at submit time, or None."""
    if not cl_ord_id:
        return None
    return _load_map().get(str(cl_ord_id))


def record_submit_strategy(
    cl_ord_id: str,
    strategy_id: str,
    venue: str | None = None,
    mode: str | None = None,
) -> None:
    """Append a submit-time ``cl_ord_id -> strategy_id`` mapping (fsync'd).

    No-ops when ``cl_ord_id``/``strategy_id`` is missing, or when ``cl_ord_id`` is
    already mapped (first-write-wins — submit is one-time). Best-effort by design:
    the caller wraps this in try/except so a map-write failure can never block a
    trade.
    """
    if not cl_ord_id or strategy_id is None:
        return
    cid = str(cl_ord_id)
    # First-write-wins: a re-submit under the same cl_ord_id keeps the original
    # attribution and avoids unbounded duplicate growth.
    if cid in _load_map():
        return
    path = _map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "cl_ord_id": cid,
        "strategy_id": str(strategy_id),
        "venue": (str(venue).lower() if venue else None),
        "mode": (str(mode).lower() if mode else None),
        "ts_ms": int(time.time() * 1000),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
