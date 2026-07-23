"""Submit-time cloid mapping for venues that hash client order IDs.

Hyperliquid accepts a ``clientOrderId`` but returns a numeric/hex ``cloid`` in
order history — the original ``mxc-...`` prefix is gone on read-back. We record
the mapping at submit time so reconciliation can attribute the order back to
HermX (P&L Master Plan, Phase 7b).

Paths resolve at call time from the environment (HERMX_DATA_DIR -> HERMX_ROOT ->
repo root), mirroring ``pnl_ledger`` so tests that rebind the root via env need
no module reload.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _map_path() -> Path:
    return Path(
        os.environ.get("HERMX_DATA_DIR")
        or os.environ.get("HERMX_ROOT")
        or _REPO_ROOT
    ) / "cloid-map.jsonl"


def record_cloid_mapping(mxc_id: str, numeric_cloid: str, exchange_id: str) -> None:
    """Persist a submit-time mapping so reconciliation can resolve numeric cloids."""
    path = _map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "mxc_id": mxc_id,
        "cloid": str(numeric_cloid),
        "exchange": str(exchange_id).lower(),
        "ts_ms": int(time.time() * 1000),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def load_cloid_mappings() -> dict:
    """Return every recorded mapping as ``{(exchange, cloid): mxc_id}``, last-wins.

    Bulk companion to :func:`resolve_cloid` for readers that resolve many rows per
    call (e.g. the dashboard execution ledger) without re-reading the file per row.
    File order means later (newer) lines overwrite earlier ones, matching
    ``resolve_cloid``'s newest-first semantics.
    """
    path = _map_path()
    if not path.exists():
        return {}
    out: dict = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("cloid") and entry.get("exchange"):
                out[(str(entry["exchange"]).lower(), str(entry["cloid"]))] = entry.get("mxc_id")
    return out


def resolve_cloid(numeric_cloid: str | None, exchange_id: str | None) -> str | None:
    """Return the original mxc_id for a numeric cloid, or None if not found.

    Reads newest-first so a reused cloid resolves to its latest mapping.
    """
    if not numeric_cloid or not exchange_id:
        return None
    path = _map_path()
    if not path.exists():
        return None
    target = str(numeric_cloid)
    venue = str(exchange_id).lower()
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("cloid") == target and entry.get("exchange") == venue:
            return entry.get("mxc_id")
    return None
