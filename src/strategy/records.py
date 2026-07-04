"""Strategy-record reads (Phase 3 extraction, REFACTOR_PLAN.md).

Houses the strategy-file read cluster that used to live in webhook_receiver.py:
``strategy_instrument``, ``strategy_asset``, ``strategy_budget_usd``,
``normalize_strategy_record`` and ``load_strategy_files``.

Root-bound / reload-reset module state STAYS defined in webhook_receiver.py
(``STRATEGIES_DIR`` -- derived from HERMX_ROOT + engine-config at import time,
rebound by the per-test ``importlib.reload(webhook_receiver)`` in conftest's
``wr`` fixture and monkeypatched directly by
test_phase5_normalization_cleanup). ``load_strategy_files`` therefore reads it
lazily via ``import webhook_receiver as _wr`` -- the same pattern as
src/alerts.py (Phase 0), src/signals/ (Phase 1) and src/control_state.py
(Phase 2). The ``STRATEGIES = load_strategy_files()`` module-level bind and
the derived ``ALLOWED_SYMBOLS`` also stay in webhook_receiver.py for the same
reload reason. webhook_receiver re-exports every moved name so ``wr.<fn>``
call sites and monkeypatch seams keep working.

NOTE: dashboard.py carries a separate re-implementation of ``strategy_asset``
/ ``load_strategy_files`` (D1, different param name / no type annotations);
reconciling its call sites onto this module is deferred (REFACTOR_PLAN.md D1),
same as Phase 2 deferred D2.
"""
from __future__ import annotations

import json
import logging

from hermx_shared import canonical_timeframe


def strategy_instrument(row: dict) -> dict:
    """PURE: canonical instrument block for a strategy.

    A v2 strategy carries a generic ``instrument`` block ({exchange, inst_id,
    type}); this resolver reads it directly and never touches the legacy
    ``okx_inst_id`` key (Layer C removed that runtime bridge). Every strategy on
    disk is v2, so a record WITHOUT an instrument block resolves to {} -- the
    venue-less top-level ``inst_id`` -> okx fallback is gone (it silently assumed a
    venue, which is a money-safety hazard once non-okx venues exist). Callers fail
    closed on an empty result. The strategy NEVER carries credentials
    (REFACTOR_PLAN.md §0.4) -- this only maps the public venue/instrument selection.
    """
    inst = (row or {}).get("instrument")
    if isinstance(inst, dict) and inst.get("inst_id"):
        return {
            "exchange": str(inst.get("exchange") or "okx").lower(),
            "inst_id": str(inst.get("inst_id")),
            "type": str(inst.get("type") or "swap"),
        }
    return {}


# Instrument-type suffixes that are NOT part of the BASE+QUOTE asset symbol.
_INSTRUMENT_TYPE_SUFFIXES = {"SWAP", "FUTURES", "FUTURE", "PERP", "SPOT", "MARGIN", "OPTION"}


def strategy_asset(strategy: dict) -> str:
    """PURE: the BASE+QUOTE asset symbol for a strategy (e.g. ``BTCUSDT``).

    The v3 strategy shape dropped the explicit ``asset`` field; the symbol is now
    derived from the canonical ``instrument.inst_id``. An OKX-native id
    (``BTC-USDT-SWAP``) or a CCXT-unified id (``BTC/USDT:USDT``) both resolve to
    ``BTCUSDT`` so the alert-symbol match (uppercased, separators stripped) keeps
    working. A still-present top-level ``asset`` is honored as an override.
    """
    explicit = str((strategy or {}).get("asset") or "").strip().upper()
    if explicit:
        return explicit
    inst_id = str((strategy_instrument(strategy) or {}).get("inst_id") or "")
    if not inst_id:
        return ""
    core = inst_id.split(":", 1)[0].replace("/", "-")  # drop settle ccy, unify sep
    parts = [p for p in core.split("-") if p]
    if len(parts) >= 3 and parts[-1].upper() in _INSTRUMENT_TYPE_SUFFIXES:
        parts = parts[:-1]
    return "".join(parts).upper()


def strategy_budget_usd(strategy: dict) -> float:
    """Read budget from capital.budget_usd (v2 nested) with flat fallback."""
    cap = strategy.get("capital")
    if isinstance(cap, dict):
        v = cap.get("budget_usd")
        if v is not None:
            return float(v)
    v = strategy.get("budget_usd")
    return float(v) if v is not None else 0.0


def strategy_reinvest_enabled(strategy: dict) -> bool:
    """Read capital.reinvest (v2 nested) with flat fallback; ABSENT defaults True.

    True (the schemas/strategy.schema.json default) sizes orders off equity =
    seed budget + realized net P&L (compounding); False pins sizing to the
    fixed ``budget_usd`` seed.
    """
    cap = (strategy or {}).get("capital")
    if isinstance(cap, dict) and "reinvest" in cap:
        return bool(cap.get("reinvest"))
    if "reinvest" in (strategy or {}):
        return bool(strategy.get("reinvest"))
    return True


def normalize_strategy_record(row: dict) -> dict:
    """v2 loader shim (REFACTOR_PLAN.md Phase 6 / Layer C).

    A schema_version 2 strategy selects its exchange via the generic
    ``instrument`` block and uses ``submit_orders``. This canonicalizes the
    instrument-first shape in place:

      * v2 records (carry ``instrument``): normalize exchange/type defaults so
        downstream resolvers see a complete block.

    Layer C removes the legacy ``okx_inst_id`` -> ``instrument`` runtime bridge:
    strategy files are now canonical v2 on disk, so no v1 synthesis happens here.
    The ``okx_submit_orders`` bridge is deliberately left untouched (out of scope
    for this slice) so the execution-readiness / submit path keeps byte-identical
    behavior.
    """
    inst = row.get("instrument")
    if isinstance(inst, dict) and inst.get("inst_id"):
        inst["exchange"] = str(inst.get("exchange") or "okx").lower()
        inst["type"] = str(inst.get("type") or "swap")
        if "okx_submit_orders" not in row:
            row["okx_submit_orders"] = bool(row.get("submit_orders", False))
    return row


def load_strategy_files() -> dict:
    import webhook_receiver as _wr
    strategies = {}
    if not _wr.STRATEGIES_DIR.exists():
        return strategies
    for path in sorted(_wr.STRATEGIES_DIR.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            sid = str(row.get("strategy_id") or "").strip()
            if not sid:
                continue
            row = normalize_strategy_record(row)
            row["_path"] = str(path)
            row["timeframe"] = canonical_timeframe(row.get("timeframe"))
            # v3 dropped the explicit asset field; derive the BASE+QUOTE symbol from
            # the canonical instrument so alert-symbol matching keeps working.
            row["asset"] = strategy_asset(row)
            strategies[sid] = row
        except Exception as exc:
            logging.warning("Failed to load strategy file %s: %s", path, exc)
    return strategies
