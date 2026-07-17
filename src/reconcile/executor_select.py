"""Per-order (venue, mode) reconcile executor resolution (REFACTOR_PLAN.md Phase 5,
issue #20a).

ROOT and ExecutorFactory are root-bound / reload-sensitive (webhook_receiver.py
resolves ROOT from HERMX_ROOT at import time, and the per-test `wr` fixture does
`importlib.reload(webhook_receiver)` against a fresh temp root), so they are read
lazily via `import webhook_receiver as _wr` rather than imported at module top --
matching the Phase 0-4 pattern.

_reconciliation_executor is ALSO the function tests monkeypatch directly on `wr`
(`monkeypatch.setattr(wr, "_reconciliation_executor", ...)`) and expect callers to
observe -- including _executor_for_order in THIS SAME module. A same-module direct
call would bind to this module's own (unpatched) function object, not the
monkeypatched one now living only in wr's namespace, so _executor_for_order also
dereferences it through `_wr.` rather than calling it directly.
"""
from __future__ import annotations

import logging

from strategy.readiness import effective_execution_mode
from strategy.records import strategy_instrument
from webhook.config import EXEC_BACKEND, EXECUTION_DEFAULTS


def active_venue_modes() -> "set[tuple[str, bool]]":
    """B1 enumerator (NAUTILUS_GAP_REMEDIATION_PLAN.md §0.6 item 4.2): every
    (venue, simulated_trading) pair the LOADED strategy configs can trade on --
    the "should be checked" domain for drift monitors, derived from strategy
    files rather than open orders so an idle account is still covered.

    Tuples use exactly _executor_for_order's cache-key shape: (lowercased venue
    string, simulated bool) where demo -> True, live -> False. The venue comes
    from strategy_instrument() (fail closed: a strategy without a resolvable
    instrument block is skipped); the mode is the effective execution_mode
    INCLUDING the live control-state override, resolved through
    strategy.readiness.effective_execution_mode so this domain can never drift
    from the signal path's own resolution. STRATEGIES is root-bound /
    reload-reset module state, so it is read lazily via ``import
    webhook_receiver as _wr`` (see module docstring)."""
    import webhook_receiver as _wr
    pairs: "set[tuple[str, bool]]" = set()
    for sid, strategy in (getattr(_wr, "STRATEGIES", None) or {}).items():
        venue = str((strategy_instrument(strategy) or {}).get("exchange") or "").strip().lower()
        if not venue:
            continue
        mode = effective_execution_mode(strategy, sid)
        pairs.add((venue, mode != "live"))
    return pairs


def settle_currency(inst_id: "str | None") -> str:
    """PURE: the settlement currency of an inst_id -- the exact key the drift check
    must pull out of the venue's multi-currency balance dict.

    Fail-open to ``"USDT"`` (the pre-fix hardcoded default) on any unparseable
    shape so a bad/unknown id degrades the drift sweep to prior behavior rather
    than crashing it. Handles both id conventions already used in this codebase:

      OKX-native   BTC-USDT-SWAP -> USDT   ETH-USDC-SWAP -> USDC
      CCXT-unified BTC/USDT:USDT -> USDT   SOL/USDC:USDC -> USDC

    and the inverse-contract convention from
    ``ccxt_adapter._inst_id_to_ccxt_symbol`` (``settle = base if quote == "USD"``),
    so a ``BTC-USD-SWAP`` inverse id resolves to ``BTC``, for consistency."""
    text = str(inst_id or "").strip().upper()
    if not text:
        return "USDT"
    try:
        # CCXT-unified "BASE/QUOTE:SETTLE" -- the settle ccy is explicit after ':'.
        if ":" in text:
            settle = text.split(":", 1)[1].strip()
            return settle or "USDT"
        # OKX-native "BASE-QUOTE[-TYPE]" -- the middle segment is the quote ccy;
        # an inverse (quote == USD) settles in the base, matching ccxt_adapter:147.
        if "-" in text:
            parts = [p for p in text.split("-") if p]
            if len(parts) >= 2:
                base, quote = parts[0], parts[1]
                return base if quote == "USD" else quote
        # Spot-style "BASE/QUOTE" (no settle marker) -- the quote is the settle ccy.
        if "/" in text:
            base, quote = (p.strip() for p in text.split("/", 1))
            if quote:
                return base if quote == "USD" else quote
    except Exception:  # pragma: no cover - defensive: any parse surprise -> default
        pass
    return "USDT"


def active_venue_mode_currencies() -> "set[tuple[str, bool, str]]":
    """Currency-aware sibling of :func:`active_venue_modes` (issue #9, Half 1): the
    same ``(venue, simulated_trading)`` domain, PLUS the settlement currency each
    loaded strategy trades in -- so the balance-drift sweep can read the right
    balance key per strategy instead of the hardcoded ``"USDT"``. A USDT strategy
    and a USDC strategy on the same venue yield two distinct tuples, hence two
    drift checks (one per currency) rather than one wrong USDT-only check.

    Kept as a PARALLEL function -- ``active_venue_modes`` stays a 2-tuple set --
    because that function's exact shape is asserted by tests and mirrored by
    ``pnl_ledger._account_equity_estimate``; only the drift path needs currency.
    Venue and mode resolve IDENTICALLY to ``active_venue_modes`` (same lazy
    STRATEGIES read, same ``effective_execution_mode`` override); the currency
    comes from :func:`settle_currency` over the instrument's ``inst_id`` and
    fail-opens to ``"USDT"``, so an all-USDT fleet yields exactly one currency
    per venue -- no behavior change from the pre-fix default."""
    import webhook_receiver as _wr
    triples: "set[tuple[str, bool, str]]" = set()
    for sid, strategy in (getattr(_wr, "STRATEGIES", None) or {}).items():
        inst = strategy_instrument(strategy) or {}
        venue = str(inst.get("exchange") or "").strip().lower()
        if not venue:
            continue
        mode = effective_execution_mode(strategy, sid)
        triples.add((venue, mode != "live", settle_currency(inst.get("inst_id"))))
    return triples


def active_venue_mode_instruments() -> "dict[tuple[str, bool], set[str]]":
    """Instrument-aware sibling of :func:`active_venue_modes` for the ledger sweep.

    Same ``(venue, simulated_trading)`` domain as :func:`active_venue_modes`,
    mapped to the set of ``inst_id`` strings loaded strategies trade on that
    env. ``get_order_history_raw`` requires concrete inst_ids (empty list yields
    no rows), so the ledger fold cannot call it with ``None``.

    Venue and mode resolve IDENTICALLY to ``active_venue_modes`` (lazy
    STRATEGIES, ``effective_execution_mode`` including control-state override).
    Strategies without a resolvable venue or ``inst_id`` are skipped.
    """
    import webhook_receiver as _wr
    out: "dict[tuple[str, bool], set[str]]" = {}
    for sid, strategy in (getattr(_wr, "STRATEGIES", None) or {}).items():
        inst = strategy_instrument(strategy) or {}
        venue = str(inst.get("exchange") or "").strip().lower()
        if not venue:
            continue
        inst_id = str(inst.get("inst_id") or "").strip()
        if not inst_id:
            continue
        mode = effective_execution_mode(strategy, sid)
        key = (venue, mode != "live")
        out.setdefault(key, set()).add(inst_id)
    return out


def _effective_execution_config(order_intent: "dict | None" = None) -> dict:
    """The execution config the write path actually resolves: the adapter selector
    (EXEC_BACKEND, which already honors HERMX_EXEC_BACKEND) plus the venue+mode to
    query.

    Issue #20a: the order-state reconciler must query the SAME (venue, mode) the order
    was submitted to. When an ``order_intent`` (from the order-journal record) is given,
    its persisted ``venue`` / ``simulated_trading`` override the OKX-demo default so a
    Bybit-live order is checked on Bybit-live, not OKX-demo. Absent an intent (or a
    legacy intent without those fields) it falls back to EXECUTION_DEFAULTS'
    ``ccxt_exchange`` (okx) and leaves ``simulated_trading`` unset -> the adapter
    defaults to the demo sandbox (the safe pre-#20a fallback)."""
    exec_cfg = {"exchange": EXEC_BACKEND, "ccxt_exchange": EXECUTION_DEFAULTS["ccxt_exchange"]}
    if isinstance(order_intent, dict):
        venue = order_intent.get("venue")
        if venue:
            exec_cfg["ccxt_exchange"] = str(venue).strip().lower()
        simulated = order_intent.get("simulated_trading")
        if simulated is not None:
            exec_cfg["simulated_trading"] = bool(simulated)
    return {"execution": exec_cfg}


def _reconciliation_executor(order_intent: "dict | None" = None):
    """Build the read-only query executor for an order's (venue, mode), or None if
    unavailable. Constructed lazily so a missing factory / bad config simply disables
    reconciliation rather than crashing the receiver (fail closed to observe-only).

    Uses _effective_execution_config(order_intent) so reconcile always queries the
    venue+account the order was submitted to (#20a). Called with no argument it yields
    the OKX-demo default executor -- the pre-#20a global executor and the fallback for
    orders whose journal record predates venue/mode persistence."""
    import webhook_receiver as _wr
    if _wr.ExecutorFactory is None:
        return None
    try:
        return _wr.ExecutorFactory.create(_effective_execution_config(order_intent), _wr.ROOT)
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("reconciliation executor unavailable: %s", exc)
        return None


def _executor_for_order(intent: "dict | None", cache: dict, default_executor):
    """Resolve the read-only reconcile executor for ONE order from the (venue, mode)
    persisted on its journal intent (#20a).

    Orders journalled before venue/mode persistence carry neither field -> return the
    caller's ``default_executor`` (OKX-demo), i.e. unchanged pre-#20a behavior. Built
    executors are cached by ``(venue, simulated)`` so N orders sharing one account
    reuse a single authenticated client rather than opening N duplicates."""
    import webhook_receiver as _wr
    if not isinstance(intent, dict):
        return default_executor
    venue = intent.get("venue")
    simulated = intent.get("simulated_trading")
    if not venue and simulated is None:
        return default_executor  # legacy order: OKX-demo default, unchanged
    key = (
        str(venue or EXECUTION_DEFAULTS["ccxt_exchange"]).strip().lower(),
        True if simulated is None else bool(simulated),
    )
    if key not in cache:
        cache[key] = _wr._reconciliation_executor(intent)
    return cache[key]
