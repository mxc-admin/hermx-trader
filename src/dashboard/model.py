"""Dashboard data model: projections, health/freshness summaries, P&L contracts
and API payloads (REFACTOR_PLAN.md Phase 7 sub-step 2; monolith lines 357-505 +
1490-1898 pre-sub-step-1 numbering, moved not rewritten).

PACKAGE LAYOUT NOTE: this directory deliberately has NO __init__.py. A regular
package named ``dashboard`` would shadow ``src/dashboard.py`` for every
``import dashboard`` (packages win over same-named modules on sys.path), which
would break the whole test suite and the shim design. Instead, dashboard.py
extends its own ``__path__`` to this directory, which makes
``dashboard.model`` importable as a submodule while ``import dashboard``
keeps resolving to the monolith. Do not add an __init__.py here.

LOGS, the model cache (``_MODEL_CACHE``/``_MODEL_BUILD_LOCK``), REFRESH/TTL
constants, ``_hermes_enabled`` and every dashboard_core import (``SYMBOLS``,
``as_float``, ``parse_dt``, ``LEDGER_READ_STATS``, ...) are root-bound /
reload-sensitive (dashboard.py resolves ROOT from HERMX_ROOT at import time,
and test fixtures do ``importlib.reload(dashboard_core)`` +
``importlib.reload(dashboard)`` against a fresh temp root), so they are read
lazily via ``import dashboard as _dash`` rather than imported at module top --
matching the snapshots.py / reconcile/executor_select.py pattern. In
particular the tests mutate ``dash_mod._MODEL_CACHE`` in place, so the cache
dict itself must keep living on the dashboard module.

Several names called below are ALSO the seams tests monkeypatch directly on
the ``dashboard`` module (``okx_live_snapshot`` via plain attribute
assignment, ``active_strategies``, ``load_strategy_files``, ``trial_symbols``,
``LOGS``, ``_dashboard_executor``/``_strategy_executor`` transitively) and
expect callers -- including callers in THIS SAME module -- to observe. A
same-module direct call would bind to this module's own (unpatched) function
object, so every cross-function call below dereferences through ``_dash.`` too.

The deferred ``from pnl_ledger import ...`` imports are kept function-local ON
PURPOSE (REFACTOR_PLAN.md circular-import notes): pnl_ledger must stay off the
dashboard import-time graph. Do not hoist them to module top.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict


class StrategyPnlContract(TypedDict, total=False):
    """Per-strategy P&L contract returned by :func:`_strategy_pnl_contract`.

    Deliberately a *superset* (hence ``total=False``): the Phase-3 ``*_usd``
    aggregate keys and the Phase-4 React-UI alias keys are both present, as
    views onto the same numbers. tests/test_pnl_api_contracts.py pins the
    exact key set the frontend consumes -- do not rename or drop keys here."""
    # Phase-3 ledger aggregate keys (pnl_ledger.aggregate_strategy_pnl)
    budget_usd: float
    closed_realized_pnl_usd: float
    closed_fees_usd: float
    closed_net_pnl_usd: float
    open_upl_usd: float
    equity_now_usd: float
    closed_order_count: int
    last_close_at_ms: Optional[int]
    accounting_start_at: Optional[int]
    # Phase-4 React UI contract aliases
    strategy_id: Optional[str]
    venue: str
    mode: str
    realized_net: float
    realized_gross: float
    fees: float
    upl: float
    total_net: float
    trade_count: int
    pnl_series: List[Dict[str, Any]]


class PortfolioContract(TypedDict):
    """Portfolio roll-up returned by :func:`portfolio_contract` (React UI
    contract, pinned by tests/test_pnl_api_contracts.py)."""
    realized_net: float
    realized_gross: float
    fees: float
    upl: float
    total_net: float
    trade_count: int
    strategies: int
    unattributed: Dict[str, Any]


class DashboardModel(TypedDict, total=False):
    """The cached dashboard model built by :func:`_build_dashboard_model`.

    ``total=False`` because the dict is built incrementally (executor /
    ledger_health / freshness are stamped after the literal)."""
    config: Dict[str, Any]
    loaded: Dict[str, Any]
    okx_live: Dict[str, Any]
    okx_live_by_mode: Dict[str, Any]
    exch_live_by_env: Dict[str, Any]
    okx_executions: List[Dict[str, Any]]
    strategies: List[Dict[str, Any]]
    active_strategies: List[Dict[str, Any]]
    strategy_alerts: List[Dict[str, Any]]
    generated_at: str
    executor: Dict[str, Any]
    ledger_health: Dict[str, Any]
    freshness: Dict[str, Any]


def active_strategies(strategies=None):
    import dashboard as _dash
    rows = strategies if strategies is not None else _dash.load_strategy_files()
    return [s for s in rows if _dash.is_strategy_active(s)]


def trial_symbols(config=None):
    """Symbols the dashboard cares about, derived from active strategy files (D5).

    Falls back to the legacy static SYMBOLS list only when there are no strategy
    files at all, so an empty/misconfigured deploy still renders something.
    """
    import dashboard as _dash
    seen = []
    for strategy in _dash.active_strategies():
        sym = strategy.get("asset")
        if sym and sym not in seen:
            seen.append(sym)
    if not seen:
        for sym in _dash.SYMBOLS:
            if sym not in seen:
                seen.append(sym)
    return seen


def strategy_inst_id(config, sym):
    # Venue-aware: the strategy file's OWN instrument block is the source of truth (a
    # kucoin spot pair, a hyperliquid perp, an okx swap all resolve to THEIR id), then
    # any explicitly configured per-asset inst_id. No hardcoded -USDT-SWAP transform --
    # an unknown symbol resolves to "" (the caller degrades gracefully, never fabricates
    # a fake okx instrument).
    import dashboard as _dash
    for strategy in _dash.load_strategy_files():
        if strategy.get("asset") == sym:
            inst = strategy.get("instrument") or {}
            inst_id = inst.get("inst_id") or strategy.get("inst_id")
            if inst_id:
                return inst_id
    try:
        configured = _dash.asset_inst_id(config, sym)
        if configured:
            return configured
    except Exception:
        pass
    return ""


def _pipeline_rows(stage, limit, scan=None):
    """Read up to ``limit`` rows of one pipeline ``stage`` from the unified
    pipeline.jsonl. Because the file interleaves stages, we tail a larger ``scan``
    window (bounded by the same OOM-safe reverse-tail reader) and keep the last
    ``limit`` rows matching the stage. Returns ``(rows, stats)``."""
    import dashboard as _dash
    scan = scan if scan is not None else max(int(limit) * 6, 1000)
    rows, stats = _dash.read_jsonl_stats(_dash.LOGS / "pipeline.jsonl", scan)
    rows = [r for r in rows if r.get("stage") == stage]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows, stats


SIGNALS_MAX_N = 500
SIGNALS_DEFAULT_N = 50


def _signal_projection(row):
    """Project one execution-stage pipeline row to the compact /api/signals shape.

    Handles both normal TV-triggered submissions and operator closes
    (symbol/strategy_id/kind/operator/reason stamped at the top level by
    execute_operator_close). The service wraps the adapter's normalized_result() under
    ``okx_execution.payload`` -- that envelope carries the venue payload (symbol,
    target_direction, executed_orders, ...) one level deeper under its own ``payload``
    and fill_summary at its top level. The former OKX-native ``payload.plan`` shape is
    gone; gate-blocked rows have no adapter envelope at all (falsey lookups)."""
    # Dual-read: prefer the new ``exec_result`` key, fall back to the legacy
    # ``okx_execution`` key so historical ledger rows keep resolving forever.
    okx = row.get("exec_result") or row.get("okx_execution") or {}
    adapter = okx.get("payload") or {}
    fill = adapter.get("fill_summary") or {}
    inner = adapter.get("payload") or {}
    return {
        "ts": row.get("ts"),
        "submitted_at": row.get("received_at") or row.get("ts"),
        "symbol": row.get("symbol") or inner.get("symbol"),
        "side": inner.get("target_direction"),
        "strategy_id": row.get("strategy_id"),
        "mode": okx.get("mode") or adapter.get("mode"),
        "reason": okx.get("reason"),
        "kind": row.get("kind"),
        "operator": row.get("operator"),
        "cl_ord_id": row.get("cl_ord_id") or okx.get("cl_ord_id") or fill.get("client_order_id"),
        "ok": okx.get("ok"),
        "elapsed_ms": okx.get("elapsed_ms"),
    }


def signals_payload(n=SIGNALS_DEFAULT_N, symbol=None):
    """Last ``n`` execution-stage events from pipeline.jsonl (the full TV-triggered +
    operator-close trade history), most recent first, optionally filtered by symbol.

    A missing ledger yields ``{"ok": True, "signals": [], "count": 0}``. ``n`` is
    clamped to [1, SIGNALS_MAX_N]; the underlying reverse-tail read stays bounded."""
    import dashboard as _dash
    n = max(1, min(int(n), SIGNALS_MAX_N))
    symbol_filter = str(symbol or "").strip().upper() or None
    # Scan a wider window than n so a symbol filter still surfaces n matches when
    # the stage interleaves many symbols; the read is OOM-bounded regardless.
    scan = max(n * 12, 2000)
    rows, _stats = _dash._pipeline_rows("execution", scan, scan=scan)
    projected = [_dash._signal_projection(r) for r in rows]
    if symbol_filter:
        projected = [p for p in projected if str(p.get("symbol") or "").strip().upper() == symbol_filter]
    projected = list(reversed(projected))  # most recent first
    if len(projected) > n:
        projected = projected[:n]
    return {"ok": True, "signals": projected, "count": len(projected)}


def _alerts_rows(kind, limit, scan=None):
    """Read up to ``limit`` rows of one alert ``kind`` from the unified alerts.jsonl
    (kind in {operator, reconcile, state}). Returns ``(rows, stats)``."""
    import dashboard as _dash
    scan = scan if scan is not None else max(int(limit) * 6, 500)
    rows, stats = _dash.read_jsonl_stats(_dash.LOGS / "alerts.jsonl", scan)
    rows = [r for r in rows if r.get("kind") == kind]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows, stats


def _execution_outcome_label(exec_row):
    """Collapse one execution-stage pipeline row to an alert Outcome label.

    FILLED  -- at least one executed order reached a filled/closed state
    BLOCKED -- the service refused to submit (mode=not_submitted gate outcome)
    NO FILL -- an execution attempt exists but no order filled
    None    -- unrecognizable row (caller fails open and renders a dash)
    """
    result = exec_row.get("exec_result") or exec_row.get("okx_execution") or {}
    if not isinstance(result, dict) or not result:
        return None
    if str(result.get("mode") or "").lower() == "not_submitted":
        return "BLOCKED"
    adapter = result.get("payload") or {}
    fill = adapter.get("fill_summary") or {}
    inner = adapter.get("payload") or {}
    statuses = [str(fill.get("status") or "").lower()]
    for order in inner.get("executed_orders") or []:
        if not isinstance(order, dict):
            continue
        ccxt_order = order.get("order") if isinstance(order.get("order"), dict) else {}
        statuses.append(str(ccxt_order.get("status") or order.get("status") or "").lower())
    if any(s in ("filled", "closed") for s in statuses):
        return "FILLED"
    return "NO FILL"


def _execution_outcomes_by_received_at(limit):
    """Map intake ``received_at`` -> {outcome, strategy_id} from execution-stage rows.

    ``received_at`` is the microsecond-ISO intake join key: the ExecutionService
    writes it back verbatim on the outcome row, so an exact string match is the
    correlation (never fuzzy symbol+time). Later rows win -- the final outcome for
    a signal supersedes earlier gate rows. Fail-open: unreadable rows are skipped."""
    import dashboard as _dash
    exec_rows, _stats = _dash._pipeline_rows("execution", limit)
    outcomes = {}
    for ex in exec_rows:
        received_at = ex.get("received_at")
        if not received_at:
            continue
        label = _execution_outcome_label(ex)
        if label:
            outcomes[str(received_at)] = {"outcome": label, "strategy_id": ex.get("strategy_id")}
    return outcomes


def strategy_alert_rows(limit=500):
    import dashboard as _dash
    rows, _stats = _dash._pipeline_rows("strategy_match", limit)
    try:
        outcomes = _execution_outcomes_by_received_at(limit)
    except Exception:  # observability join; must never break the alert log
        outcomes = {}
    out = []
    for row in rows:
        norm = row.get("normalized") or {}
        strategy = row.get("strategy_config") or {}
        if not norm.get("strategy_id"):
            continue
        # Exact received_at join to the execution outcome. Go-forward safety: when
        # the outcome row carries a strategy_id (new stamped rows) it must agree
        # with the alert's; historical unstamped rows join on the key alone.
        # No match -> outcome None (renders as a dash, never "orphan").
        matched = outcomes.get(str(row.get("received_at") or ""))
        outcome = None
        if matched and (
            not matched.get("strategy_id")
            or str(matched["strategy_id"]) == str(norm.get("strategy_id"))
        ):
            outcome = matched["outcome"]
        out.append({
            "outcome": outcome,
            "received_at": row.get("received_at"),
            "received_colombia": _dash.colombia_time(row.get("received_at")),
            "strategy_id": norm.get("strategy_id"),
            "strategy_name": strategy.get("name") or norm.get("strategy_name") or norm.get("strategy_id"),
            "asset": norm.get("symbol") or strategy.get("asset"),
            "timeframe": norm.get("timeframe") or strategy.get("timeframe"),
            "side": str(norm.get("action") or "").upper(),
            "price": norm.get("tv_signal_price"),
            "tv_time": norm.get("tv_time"),
            "tv_time_colombia": _dash.colombia_time(norm.get("tv_time")),
            "duplicate": bool(row.get("duplicate")),
            "decision": _dash.nested_get(row, "strategy_decision", "decision") or _dash.nested_get(row, "decision", "decision"),
            "mode": row.get("mode"),
            "okx_mode": _dash.nested_get(row, "exec_result", "mode") or _dash.nested_get(row, "okx_execution", "mode"),
            "block_reason": _dash.nested_get(row, "execution_readiness", "block_reason"),
            "latency": _dash.nested_get(row, "latency", "latency_seconds"),
        })
    out.sort(key=lambda r: _dash.parse_dt(r.get("tv_time") or r.get("received_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return out


def executor_health_summary(okx_live, now=None):
    """Collapse the executor read into an explicit health verdict (D3).

    ``degraded`` drives the visible banner; we never let an errored or stale
    executor read render as a healthy flat view.
    """
    import dashboard as _dash
    now = now if now is not None else time.time()
    okx_live = okx_live or {}
    healthy = bool(okx_live.get("ok"))
    error = None if healthy else str(okx_live.get("error") or "executor_unavailable")
    age = None
    stale = False
    dt = _dash.parse_dt(okx_live.get("generated_at"))
    if dt is not None:
        age = max(0.0, now - dt.timestamp())
        stale = age > _dash.REFRESH_INTERVAL_SECONDS
    elif healthy:
        # Reported ok but no timestamp to prove freshness -> treat as stale.
        stale = True
    return {
        "ok": healthy and not stale,
        "healthy": healthy,
        "error": error,
        "stale": stale,
        "degraded": (not healthy) or stale,
        "age_seconds": age,
        "generated_at": okx_live.get("generated_at"),
    }


def _active_env_keys(strategies, overrides):
    """Distinct ``{venue}:{mode_key}`` environments used by ACTIVE strategies, in
    strategy-file order — the same (venue, effective-mode) derivation
    :func:`_build_dashboard_model` uses to build ``exch_live_by_env``, so every key
    returned here has a per-env snapshot there. Module-local on purpose (pure
    derivation, not a monkeypatched seam, and not re-exported by dashboard.py)."""
    import dashboard as _dash
    keys = []
    for s in _dash.active_strategies(strategies):
        venue = _dash._strategy_venue(s)
        mode = _dash._effective_strategy_mode(s, overrides)
        key = f"{venue}:{'live' if mode == 'live' else 'demo'}"
        if key not in keys:
            keys.append(key)
    return keys


def executor_env_health_summary(exch_live_by_env, env_keys, now=None):
    """Collapse the per-(venue, mode) snapshots of the given environments into one
    aggregate executor verdict (the red/green Engine widget).

    Same top-level shape as :func:`executor_health_summary` — ``ok`` / ``healthy``
    / ``error`` / ``stale`` / ``degraded`` / ``age_seconds`` / ``generated_at``;
    render.py's banners/cards and the React ExecutorHealthCard read exactly those
    keys — plus an additive ``envs`` map carrying each environment's own summary.
    Verdict semantics: ``ok``/``healthy`` only when EVERY env is; any env
    stale/degraded taints the aggregate; ``error`` is the first env's error,
    env-prefixed; ``age_seconds``/``generated_at`` report the OLDEST env read. An
    env key with no fetched snapshot surfaces as an explicit error, never a
    KeyError — an unfetched venue must not render green."""
    import dashboard as _dash
    now = now if now is not None else time.time()
    by_env = exch_live_by_env or {}
    envs = {}
    for key in env_keys:
        snap = by_env.get(key)
        if snap is None:
            envs[key] = {
                "ok": False,
                "healthy": False,
                "error": "missing_env_snapshot",
                "stale": False,
                "degraded": True,
                "age_seconds": None,
                "generated_at": None,
            }
        else:
            envs[key] = _dash.executor_health_summary(snap, now)
    error = None
    for key, summary in envs.items():
        if summary.get("error"):
            error = f"{key}: {summary['error']}"
            break
    oldest = None
    for summary in envs.values():
        age = summary.get("age_seconds")
        if age is not None and (oldest is None or age > oldest["age_seconds"]):
            oldest = summary
    return {
        "ok": bool(envs) and all(s.get("ok") for s in envs.values()),
        "healthy": bool(envs) and all(s.get("healthy") for s in envs.values()),
        "error": error,
        "stale": any(s.get("stale") for s in envs.values()),
        "degraded": (not envs) or any(s.get("degraded") for s in envs.values()),
        "age_seconds": oldest["age_seconds"] if oldest else None,
        "generated_at": oldest.get("generated_at") if oldest else None,
        "envs": envs,
    }


def ledger_health_summary():
    """Aggregate corrupt/skipped-line counts surfaced by the bounded reader (D1/D2)."""
    import dashboard as _dash
    ledgers = {}
    total_skipped = 0
    truncated_tails = 0
    for path, st in _dash.LEDGER_READ_STATS.items():
        skipped = int(st.get("skipped") or 0)
        total_skipped += skipped
        if st.get("truncated_tail"):
            truncated_tails += 1
        if skipped or st.get("truncated_tail") or st.get("more"):
            ledgers[Path(path).name] = {
                "skipped": skipped,
                "truncated_tail": bool(st.get("truncated_tail")),
                "more": bool(st.get("more")),
                "read": int(st.get("read") or 0),
            }
    return {
        "total_skipped": total_skipped,
        "truncated_tails": truncated_tails,
        "ledgers": ledgers,
    }


def freshness_summary(model, now=None):
    """True data age vs. the refresh interval, for the "Updated" badge (D7)."""
    import dashboard as _dash
    now = now if now is not None else time.time()
    candidates = []
    for row in model.get("strategy_alerts") or []:
        for key in ("received_at", "tv_time"):
            dt = _dash.parse_dt(row.get(key))
            if dt is not None:
                candidates.append(dt.timestamp())
    dt = _dash.parse_dt((model.get("okx_live") or {}).get("generated_at"))
    if dt is not None:
        candidates.append(dt.timestamp())
    data_at = max(candidates) if candidates else None
    age = (now - data_at) if data_at is not None else None
    stale = (age is None) or (age > _dash.REFRESH_INTERVAL_SECONDS)
    return {
        "generated_at": model.get("generated_at"),
        "data_at": datetime.fromtimestamp(data_at, timezone.utc).isoformat() if data_at is not None else None,
        "age_seconds": age,
        "stale": stale,
        "no_data": data_at is None,
        "refresh_interval_seconds": _dash.REFRESH_INTERVAL_SECONDS,
    }


def dashboard_model() -> DashboardModel:
    # Stale-while-revalidate: once the cache holds ANY model, return it
    # immediately -- even if past its TTL. Freshness is the background refresh
    # loop's job (_refresh_dashboard_cache_loop), so /api never blocks on a
    # rebuild in steady state. A genuinely empty cache (model is None, e.g.
    # first boot or a test that reset it) still does one synchronous build,
    # single-flighted under _MODEL_BUILD_LOCK so a concurrent cold /api request
    # and the loop's first pass don't both pay the ~15s cost.
    import dashboard as _dash
    cached = _dash._MODEL_CACHE.get("model")
    if cached is not None:
        return cached
    with _dash._MODEL_BUILD_LOCK:
        # Re-check under the lock: another thread may have built while we waited.
        cached = _dash._MODEL_CACHE.get("model")
        if cached is not None:
            return cached
        return _dash._build_dashboard_model()


def _build_dashboard_model() -> DashboardModel:
    """Build the dashboard model and refresh _MODEL_CACHE. Always does the full
    work -- callers decide when a rebuild is warranted (cold cache in
    dashboard_model(), or the periodic background loop)."""
    import dashboard as _dash
    now = time.time()
    # Clear per-read ledger stats so ledger_health reflects THIS build only (D1/D2).
    _dash.LEDGER_READ_STATS.clear()
    cfg = _dash.shadow_config()
    loaded = _dash.load_events()
    strategies = _dash.load_strategy_files()
    # Phase 0 (demo/live separation): read the account that matches each strategy's
    # mode. Always fetch the demo snapshot (today's behavior). Only fetch the live
    # snapshot when at least one strategy is effectively live -- okx_live_snapshot
    # itself fail-closes to demo when HERMX_LIVE_TRADING is disarmed. If no strategy
    # is live, the live slot reuses the demo snapshot so behavior is unchanged.
    _ctrl = _dash._load_control_state()
    _overrides = _ctrl.get("strategy_overrides") if isinstance(_ctrl.get("strategy_overrides"), dict) else {}
    _any_live = any(_dash._effective_strategy_mode(s, _overrides) == "live" for s in strategies)
    # Call the demo path with no kwarg (default simulated_trading=True) so any test
    # stub or legacy caller with a 1-arg signature stays compatible. Only reach for
    # the live path when a strategy is actually live.
    okx_live_demo = _dash.okx_live_snapshot(cfg)
    okx_live_live = _dash.okx_live_snapshot(cfg, simulated_trading=False) if _any_live else okx_live_demo
    okx_live_by_mode = {"demo": okx_live_demo, "live": okx_live_live}
    # Phase 0.5 (per-strategy venue+mode): each strategy is an independent
    # (asset, venue, mode) environment. Group active strategies by (venue, mode) so
    # one executor per account serves every strategy on it, and fetch that account's
    # positions + order history from the strategy's OWN venue -- a KuCoin strategy
    # reads KuCoin, an OKX-live strategy reads OKX live. The per-env map is keyed
    # "{venue}:{mode}"; the legacy okx_live_by_mode above is retained for callers that
    # only distinguish demo/live (executor_health_summary, freshness).
    exch_live_by_env = {}
    seen_envs = {}  # (venue, mode) -> representative strategy_config
    for s in strategies:
        venue = _dash._strategy_venue(s)
        mode = _dash._effective_strategy_mode(s, _overrides)
        mode_key = "live" if mode == "live" else "demo"
        seen_envs.setdefault((venue, mode_key), s)
    for (venue, mode_key), rep in seen_envs.items():
        env_key = f"{venue}:{mode_key}"
        exch_live_by_env[env_key] = _dash.strategy_live_snapshot(rep, mode_key)
        # Reconcile that account's order history into the durable ledger (venue+mode
        # correct, never hardcoded). Read-only; failures are swallowed inside.
        _dash.strategy_order_history_snapshot(rep, mode_key)
    # Singular okx_live stays the demo snapshot for backward-compatible consumers
    # (executor_health_summary, freshness, the demo ledger section).
    okx_live = okx_live_demo
    model: DashboardModel = {
        "config": cfg,
        "loaded": loaded,
        "okx_live": okx_live,
        "okx_live_by_mode": okx_live_by_mode,
        "exch_live_by_env": exch_live_by_env,
        "okx_executions": _dash.exchange_execution_records(cfg),
        "strategies": strategies,
        "active_strategies": _dash.active_strategies(strategies),
        "strategy_alerts": _dash.strategy_alert_rows(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # The Engine verdict aggregates every ACTIVE strategy's own (venue, mode)
    # environment — the per-env snapshots fetched above — instead of only the
    # legacy OKX-demo read, which could render green while every real trading
    # venue was down (or red while all were fine). Zero active strategies keeps
    # the legacy single-snapshot verdict unchanged. Direct (non-_dash) calls on
    # purpose: these two helpers are pure and not re-exported by dashboard.py.
    _active_envs = _active_env_keys(strategies, _overrides)
    if _active_envs:
        model["executor"] = executor_env_health_summary(exch_live_by_env, _active_envs, now)
    else:
        model["executor"] = _dash.executor_health_summary(okx_live, now)
    model["ledger_health"] = _dash.ledger_health_summary()
    model["freshness"] = _dash.freshness_summary(model, now)
    _dash._MODEL_CACHE["model"] = model
    # Stamp expiry from build-completion time, not the build-start `now`. A slow
    # (~15s) build previously produced a cache that was already expired the
    # instant it was stored (born-expired), forcing a rebuild on the very next
    # request.
    _dash._MODEL_CACHE["expires_at"] = time.time() + _dash.MODEL_CACHE_TTL_SECONDS
    return model


def _effective_strategy_mode(strategy: dict, overrides: dict) -> str:
    """Resolve a strategy's effective UI mode (pause/demo/live).

    An override (control-state.json) wins; otherwise derive from the strategy file:
    ``submit_orders`` explicitly False -> "pause", else ``execution_mode``."""
    sid = (strategy or {}).get("strategy_id") or ""
    ov = (overrides or {}).get(sid)
    if isinstance(ov, dict) and ov.get("mode"):
        return str(ov["mode"]).lower()
    if (strategy or {}).get("submit_orders") is False:
        return "pause"
    return str((strategy or {}).get("execution_mode") or "demo").lower()


def _strategy_pnl_contract(strategy, accounting_start_at, by_env, by_mode) -> StrategyPnlContract:
    """Build the per-strategy P&L contract (ledger closed figures + live UPnL).

    Read-only and additive: sums the durable closed-trade ledger scoped to the
    strategy, its account mode (demo|live), and the ``accounting_start_at`` clean
    window, then adds the open UPnL from THIS strategy's own (venue, mode) snapshot.
    A missing ledger yields all-zero closed figures, never an error.

    The returned dict is a *superset*: it carries the Phase-3 ``*_usd`` keys the
    aggregate produces AND the Phase-4 contract keys (``strategy_id``, ``venue``,
    ``mode``, ``realized_net``, ``realized_gross``, ``fees``, ``upl``, ``total_net``,
    ``trade_count``, ``last_close_at_ms``) the React UI consumes. Both name-sets are
    views onto the same numbers so neither consumer breaks."""
    import dashboard as _dash
    sid = strategy.get("strategy_id")
    venue = _dash._strategy_venue(strategy)
    mode = (strategy.get("effective_mode") or strategy.get("execution_mode") or "demo").lower()
    mode_key = "live" if mode == "live" else "demo"  # ledger mode column is demo|live
    budget = _dash.as_float((strategy.get("capital") or {}).get("budget_usd") or strategy.get("budget_usd")) or 0.0
    # Open UPnL from the strategy's own environment snapshot (Phase 0.5 per-env read).
    snap = _dash._snapshot_for_env(by_env, by_mode, venue, mode)
    pos = ((snap or {}).get("positions") or {}).get(strategy.get("asset")) or {}
    open_upl = _dash.as_float(pos.get("upl")) or 0.0
    try:
        from pnl_ledger import aggregate_strategy_pnl

        agg = aggregate_strategy_pnl(
            sid,
            budget_usd=budget,
            mode=mode_key,
            accounting_start_at=accounting_start_at,
            open_upl_usd=open_upl,
        )
    except Exception:
        # Never let a ledger read break the API payload (fail-safe, like the reconcile
        # feed). Degrade to budget + open UPnL only.
        agg = {
            "budget_usd": budget,
            "closed_realized_pnl_usd": 0.0,
            "closed_fees_usd": 0.0,
            "closed_net_pnl_usd": 0.0,
            "open_upl_usd": open_upl,
            "equity_now_usd": budget + open_upl,
            "closed_order_count": 0,
            "last_close_at_ms": None,
            "accounting_start_at": accounting_start_at,
        }
    # Phase-4 aliases (React UI contract) layered on top of the Phase-3 aggregate.
    realized_net = agg.get("closed_net_pnl_usd") or 0.0
    upl = agg.get("open_upl_usd") or 0.0
    agg.update({
        "strategy_id": sid,
        "venue": venue,
        "mode": mode_key,
        "realized_net": realized_net,
        "realized_gross": agg.get("closed_realized_pnl_usd") or 0.0,
        "fees": agg.get("closed_fees_usd") or 0.0,
        "upl": upl,
        "total_net": realized_net + upl,
        "trade_count": agg.get("closed_order_count") or 0,
        "last_close_at_ms": agg.get("last_close_at_ms"),
    })
    # Equity-curve source: cumulative realized-net over closed episodes, same
    # filters as the aggregate. Closed-only — UPnL stays a separate scalar.
    try:
        from pnl_positions import pnl_series

        agg["pnl_series"] = pnl_series(
            strategy_id=sid, mode=mode_key, accounting_start_at=accounting_start_at
        )
    except Exception:
        agg["pnl_series"] = []
    return agg


def _ledger_net_realized(strategy_id, mode, accounting_start_at):
    """Durable realized-net P&L for a strategy from the closed-trade ledger.

    Fail-safe: any ledger error (missing/corrupt file, import failure) degrades to
    0.0 so a card / budget computation never breaks on a bad read — mirrors the
    reconcile-feed and ``_strategy_pnl_contract`` fail-open posture."""
    if not strategy_id:
        return 0.0
    try:
        from pnl_ledger import net_realized_for_strategy

        return net_realized_for_strategy(
            strategy_id, mode=mode, accounting_start_at=accounting_start_at
        )
    except Exception:
        return 0.0


def portfolio_contract(strategy_pnls) -> PortfolioContract:
    """Aggregate the per-strategy P&L contracts into a single portfolio object.

    ``strategy_pnls`` is the list of dicts produced by :func:`_strategy_pnl_contract`.
    Sums are additive across strategies; ``strategies`` counts those carrying any P&L
    data (a ledger row OR a live open position), so a portfolio of untouched demo
    strategies reports 0 rather than inflating the count. Read-only."""
    realized_net = realized_gross = fees = upl = 0.0
    trade_count = 0
    active = 0
    for p in strategy_pnls or []:
        if not isinstance(p, dict):
            continue
        realized_net += p.get("realized_net") or 0.0
        realized_gross += p.get("realized_gross") or 0.0
        fees += p.get("fees") or 0.0
        upl += p.get("upl") or 0.0
        trade_count += int(p.get("trade_count") or 0)
        if (p.get("trade_count") or 0) or (p.get("upl") or 0.0):
            active += 1
    # Rows with strategy_id=None (pre-attribution history, external closes) are
    # invisible to every per-strategy sum above — disclose them so "0 closes"
    # can't hide unattributed history. Fail-open zeros on any ledger error.
    try:
        from pnl_ledger import unattributed_stats

        unattributed = unattributed_stats()
    except Exception:
        unattributed = {"count": 0, "net_realized_pnl": 0.0, "mode": "all"}
    return {
        "realized_net": realized_net,
        "realized_gross": realized_gross,
        "fees": fees,
        "upl": upl,
        "total_net": realized_net + upl,
        "trade_count": trade_count,
        "strategies": active,
        "unattributed": unattributed,
    }


def positions_contract(annotated_strategies, by_env) -> Dict[str, Any]:
    """Positions-first view for the /api payload (Positions-First design).

    ``closed`` — ledger-folded closed episodes (pnl_positions.list_positions),
    each strategy's accounting window applied so the sum of its closed rows'
    ``realized_pnl_net`` matches the strategy card's ledger aggregate.
    ``open`` — venue-truth rows (qty/upl from the live env snapshots), enriched
    with ``opened_at_ms``/``strategy_id``/entry from the matching ledger open
    episode when one exists.
    ``drift`` — the observe-only reconcile-by-position comparison, scoped to
    envs whose snapshot read succeeded (a dead executor is not position drift).
    Fail-open: any ledger error degrades to empty lists, never a failed payload."""
    import dashboard as _dash
    try:
        from pnl_positions import diff_open_positions, list_positions

        episodes = list_positions()
    except Exception:
        return {"open": [], "closed": [], "drift": {"count": 0, "rows": []}}
    windows = {}
    for s in annotated_strategies or []:
        sid = s.get("strategy_id")
        if sid and s.get("accounting_start_at") is not None:
            windows[sid] = s["accounting_start_at"]
    closed = []
    ledger_open = []
    for ep in episodes:
        if ep.get("status") == "open":
            ledger_open.append(ep)
            continue
        window = windows.get(ep.get("strategy_id"))
        if window is not None and (ep.get("closed_at_ms") or 0) < window:
            continue
        closed.append(ep)
    open_rows = []
    venue_open = []
    healthy_envs = set()
    for env_key, snap in (by_env or {}).items():
        if not (snap or {}).get("ok"):
            continue
        venue, _, mode = str(env_key).partition(":")
        healthy_envs.add((venue, mode))
        for sym, pos in ((snap or {}).get("positions") or {}).items():
            qty = _dash.as_float(pos.get("pos")) or 0.0
            if qty == 0.0:
                continue
            inst_id = pos.get("inst_id")
            venue_open.append(
                {"venue": venue, "mode": mode, "inst_id": inst_id, "qty": qty}
            )
            row = {
                "status": "open",
                "venue": venue,
                "mode": mode,
                "inst_id": inst_id,
                "symbol": sym,
                "side": "long" if qty > 0 else "short",
                "qty": abs(qty),
                "entry_px": _dash.as_float(pos.get("avg_px")),
                "upl": _dash.as_float(pos.get("upl")),
                "mark_px": _dash.as_float(pos.get("mark_px")),
                "notional_usd": _dash.as_float(pos.get("notional_usd")),
                "strategy_id": None,
                "opened_at_ms": None,
                "realized_pnl_net": None,
            }
            for ep in ledger_open:
                if (ep.get("venue"), ep.get("mode"), ep.get("inst_id")) == (venue, mode, inst_id):
                    row["strategy_id"] = ep.get("strategy_id")
                    row["opened_at_ms"] = ep.get("opened_at_ms")
                    row["realized_pnl_net"] = ep.get("realized_pnl_net")
                    if row["entry_px"] is None:
                        row["entry_px"] = ep.get("entry_px")
                    break
            if row["strategy_id"] is None:
                for s in annotated_strategies or []:
                    if s.get("asset") == sym and s.get("env_key") == env_key:
                        row["strategy_id"] = s.get("strategy_id")
                        break
            open_rows.append(row)
    try:
        drift_rows = diff_open_positions(
            [ep for ep in ledger_open if (ep.get("venue"), ep.get("mode")) in healthy_envs],
            venue_open,
        )
    except Exception:
        drift_rows = []
    return {
        "open": open_rows,
        "closed": closed,
        "drift": {"count": len(drift_rows), "rows": drift_rows},
    }


def api_payload():
    """The structured model the ``/api`` route serializes.

    Legacy `policies` field removed (D4): it was always ``{}`` because no policies
    are configured. Executor/ledger/freshness health is surfaced explicitly so a
    consumer can never mistake an executor failure for a healthy flat view (D3).
    """
    import dashboard as _dash
    model = _dash.dashboard_model()
    loaded = model["loaded"]
    open_orders, oo_stats = _dash.order_journal_open_orders()
    recon, rc_stats = _dash.reconcile_alert_records()
    ops, op_stats = _dash.operator_alert_records()

    # Merge strategy-mode overrides (control-state.json) into the payload. An override
    # takes precedence over the strategy file; otherwise effective_mode is derived from
    # the file: submit_orders explicitly False -> "pause", else execution_mode (demo/live).
    _ctrl = _dash._load_control_state()
    _overrides = _ctrl.get("strategy_overrides") if isinstance(_ctrl.get("strategy_overrides"), dict) else {}
    _acct_windows = _ctrl.get("accounting_windows") if isinstance(_ctrl.get("accounting_windows"), dict) else {}
    _by_env = model.get("exch_live_by_env") or {}
    _by_mode = model.get("okx_live_by_mode") or {}
    annotated_strategies = []
    for s in model.get("active_strategies") or []:
        s = dict(s)
        s["effective_mode"] = _dash._effective_strategy_mode(s, _overrides)
        # Phase 0.5: this strategy's own environment -- venue + which account (demo/live)
        # its positions are read from. The React UI keys per-strategy reads off these.
        s["venue"] = _dash._strategy_venue(s)
        s["okx_account_source"] = "live" if s["effective_mode"] == "live" else "demo"
        s["env_key"] = f"{s['venue']}:{s['okx_account_source']}"
        # Phase 3: accounting window + ledger-backed P&L contract (additive; the React
        # UI adopts it in Phase 4). closed_* come from the durable ledger scoped to the
        # clean window; open UPnL from this strategy's own (venue, mode) snapshot.
        _acct_start = _dash._accounting_start_for(s.get("strategy_id"), _ctrl)
        s["accounting_start_at"] = _acct_start
        s["strategy_pnl"] = _dash._strategy_pnl_contract(s, _acct_start, _by_env, _by_mode)
        annotated_strategies.append(s)

    # Phase 4: top-level portfolio roll-up across every strategy's durable P&L.
    portfolio = _dash.portfolio_contract([s.get("strategy_pnl") for s in annotated_strategies])

    # Positions-First: open (venue-truth, ledger-enriched) + closed (ledger-folded)
    # positions and the observe-only reconcile-by-position drift.
    positions = _dash.positions_contract(annotated_strategies, _by_env)

    # P1-2: read-only reconcile-health (max recorded_at_ms, lag, v3-coverage pct).
    # Never fail the /api response on a ledger read error.
    try:
        from pnl_ledger import reconcile_health_stats

        reconcile_health = reconcile_health_stats()
    except Exception:
        reconcile_health = None

    return {
        "generated_at": model["generated_at"],
        "source_counts": {k: loaded[k] for k in ("historical_count", "backfill_count", "live_count")},
        "backfill": loaded["backfill"],
        "strategies": annotated_strategies,
        "portfolio": portfolio,
        "positions": positions,
        "strategy_overrides": _overrides,
        "accounting_windows": _acct_windows,
        "trading_state": _dash._get_trading_state(_ctrl),
        "strategy_alerts": model.get("strategy_alerts") or [],
        "okx_live": model.get("okx_live"),
        "okx_live_by_mode": model.get("okx_live_by_mode") or {},
        "exch_live_by_env": model.get("exch_live_by_env") or {},
        "okx_executions": model.get("okx_executions") or [],
        "executor": model.get("executor") or {},
        "hermes": {
            "enabled": _dash._hermes_enabled,
            "ok": _dash._hermes_enabled,
        },
        "ledger_health": model.get("ledger_health") or {},
        "reconcile_health": reconcile_health,
        "freshness": model.get("freshness") or {},
        # Read-only order/reconcile/operator observability (each with its read stats).
        "open_orders": {"rows": open_orders, "stats": oo_stats},
        "reconcile_alerts": {"rows": recon, "stats": rc_stats},
        "operator_alerts": {"rows": ops, "stats": op_stats},
    }


def health_payload():
    import dashboard as _dash
    cfg = _dash.shadow_config()
    policies = list(((cfg.get("policies") or {}).get("enabled")) or [])

    # Arming is driven by the 2-control model (execution_mode per strategy +
    # the global HERMX_LIVE_TRADING kill switch), not the dead config-flag chain.
    # ``live_trading_enabled()`` is the single source of truth for the global gate;
    # kill_switch_engaged means live trading is DISABLED.
    live_enabled, _live_raw = _dash.live_trading_enabled()
    kill_switch_engaged = not live_enabled

    strategies = _dash.active_strategies()
    demo_count = sum(1 for s in strategies if (s.get("execution_mode") or "demo") != "live")
    live_count = sum(1 for s in strategies if (s.get("execution_mode") or "demo") == "live")
    armed = live_count > 0 and live_enabled

    return {
        "ok": True,
        "service": "hermx_dashboard",
        "mode": "demo_live",
        "policies": policies,
        "primary_policy": cfg.get("primary_policy"),
        "arm": {
            "kill_switch_engaged": kill_switch_engaged,
            "live_trading_enabled": live_enabled,
            "demo_strategies": demo_count,
            "live_strategies": live_count,
            "armed": armed,
        },
        "strategy_files": [row.get("strategy_id") for row in strategies],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
