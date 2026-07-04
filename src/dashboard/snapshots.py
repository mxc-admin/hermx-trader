"""Dashboard executor construction + live/history snapshots (REFACTOR_PLAN.md
Phase 7 sub-step 1; monolith lines 676-1489, moved not rewritten).

PACKAGE LAYOUT NOTE: this directory deliberately has NO __init__.py. A regular
package named ``dashboard`` would shadow ``src/dashboard.py`` for every
``import dashboard`` (packages win over same-named modules on sys.path), which
would break the whole test suite and the shim design. Instead, dashboard.py
extends its own ``__path__`` to this directory, which makes
``dashboard.snapshots`` importable as a submodule while ``import dashboard``
keeps resolving to the monolith. Do not add an __init__.py here.

ROOT, ExecutorFactory, the OKX snapshot caches and their TTLs are root-bound /
reload-sensitive (dashboard.py resolves ROOT from HERMX_ROOT at import time, and
test fixtures do ``importlib.reload(dashboard)`` against a fresh temp root), so
they are read lazily via ``import dashboard as _dash`` rather than imported at
module top -- matching the Phase 0-5 pattern (see reconcile/executor_select.py).

Several functions here are ALSO the seams tests monkeypatch directly on the
``dashboard`` module (``_dashboard_executor``, ``_strategy_executor``,
``okx_live_snapshot``, ``okx_order_history_snapshot``, ``trial_symbols``, ...)
and expect callers -- including callers in THIS SAME module -- to observe. A
same-module direct call would bind to this module's own (unpatched) function
object, so every cross-function call below dereferences through ``_dash.`` too.

The deferred ``from pnl_ledger import ...`` imports are kept function-local ON
PURPOSE (REFACTOR_PLAN.md circular-import notes): pnl_ledger must stay off the
dashboard import-time graph. Do not hoist them to module top.

Executor-build divergence note (plan asked to consider folding with
reconcile/executor_select.py): the reconcile selector derives (venue, mode) from
a persisted order-journal intent under receiver config (EXEC_BACKEND /
EXECUTION_DEFAULTS) and returns executor-or-None; the dashboard builder derives
them from a strategy config / simulated_trading flag under dashboard config and
returns an (executor, err) tuple consumed by snapshot error fields. They are
kept separate deliberately.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def real_decisions(limit=10000):
    import dashboard as _dash
    rows, _stats = _dash._pipeline_rows("decision", limit)
    out = []
    seen = set()
    symbol_set = set(_dash.trial_symbols())
    for row in rows:
        norm = row.get("normalized") or {}
        if row.get("duplicate") or norm.get("source") != "tradingview":
            continue
        if norm.get("symbol") not in symbol_set or norm.get("side") not in {"buy", "sell"}:
            continue
        sid = norm.get("signal_id") or f"{norm.get('symbol')}|{norm.get('side')}|{norm.get('tv_time')}"
        if sid in seen:
            continue
        seen.add(sid)
        out.append(row)
    return out


def backfill_state():
    import dashboard as _dash
    return _dash.load_json(_dash.BACKFILL_FILE, {"status": "missing", "decisions": []})


def load_events():
    import dashboard as _dash
    backfill = _dash.backfill_state()
    live_rows = _dash.real_decisions()
    _historical_with_live, hist_only, live = _dash.build_combined_events({}, live_rows)

    live_record_by_key = {}
    for record in live_rows:
        norm = record.get("normalized") or {}
        key = (norm.get("symbol"), norm.get("side"), norm.get("tv_time") or record.get("received_at"))
        live_record_by_key[key] = record
    for event in live:
        key = (event.get("symbol"), event.get("side"), event.get("time"))
        record = live_record_by_key.get(key) or {}
        event["received_at"] = record.get("received_at")
        event["latency"] = record.get("latency") or {}
        event["execution_readiness"] = record.get("execution_readiness") or {}

    backfill_events = []
    for row in backfill.get("decisions") or []:
        event = dict(row)
        event["source"] = "manual_backfill"
        event["time_colombia"] = event.get("time_colombia") or _dash.colombia_time(event.get("time"))
        backfill_events.append(event)

    seen = {(e.get("symbol"), e.get("side"), e.get("time")) for e in hist_only}
    combined = list(hist_only)
    for event in backfill_events:
        key = (event.get("symbol"), event.get("side"), event.get("time"))
        if key not in seen:
            combined.append(event)
            seen.add(key)
    last_known = max([_dash.parse_dt(e.get("time")) for e in combined if _dash.parse_dt(e.get("time"))] or [None])
    for event in live:
        dt = _dash.parse_dt(event.get("time"))
        key = (event.get("symbol"), event.get("side"), event.get("time"))
        if last_known and dt and dt <= last_known:
            continue
        if key not in seen:
            combined.append(event)
            seen.add(key)
    combined.sort(key=lambda e: _dash.parse_dt(e.get("time")) or datetime.min.replace(tzinfo=timezone.utc))
    return {
        "events": combined,
        "historical_count": len(hist_only),
        "backfill_count": len(backfill_events),
        "live_count": sum(1 for event in combined if event.get("source") == "live"),
        "backfill": backfill,
    }


def target_side(event):
    return "long" if str(event.get("side")).lower() == "buy" else "short"


def mark_prices(config):
    import dashboard as _dash
    tickers = _dash.okx_swap_tickers()
    marks = {}
    for sym in _dash.trial_symbols(config):
        inst = _dash.strategy_inst_id(config, sym)
        row = tickers.get(inst) or {}
        marks[sym] = _dash.as_float(row.get("last"))
    return marks


def _dashboard_executor(config, simulated_trading=True):
    import dashboard as _dash
    if _dash.ExecutorFactory is None:
        return None, "executor_factory_unavailable"
    try:
        cfg = dict(config or {})
        exec_cfg = dict((cfg.get("execution") or {}))
        if not exec_cfg.get("exchange"):
            exec_cfg["exchange"] = "ccxt"
        # ccxt is the backend, not a venue. CcxtExecutor._exchange_id() only
        # defaults to "okx" when BOTH ccxt_exchange and exchange are absent; with
        # exchange="ccxt" it would resolve to a bogus "ccxt" venue -> getattr(ccxt,
        # "ccxt") is None -> "unsupported_ccxt_exchange:ccxt". Pin the venue here so
        # the dashboard executor connects to OKX (its default) after the shadow-config
        # removal left this config empty.
        if not exec_cfg.get("ccxt_exchange"):
            exec_cfg["ccxt_exchange"] = "okx"
        # Phase 0 (demo/live separation): the dashboard must read the account that
        # matches each strategy's mode. simulated_trading=True -> OKX demo sandbox;
        # False -> the live real venue (still gated by HERMX_LIVE_TRADING inside the
        # adapter, ccxt_adapter._client). The unset default stays True so demo-only
        # deployments are byte-for-byte unchanged from before this phase.
        exec_cfg["simulated_trading"] = bool(simulated_trading)
        cfg["execution"] = exec_cfg
        return _dash.ExecutorFactory.create(cfg, _dash.ROOT), None
    except Exception as exc:
        return None, str(exc)


def _strategy_venue(strategy_config) -> str:
    """Resolve the venue a strategy trades on (its own environment).

    Source of truth is the strategy's ``instrument.exchange``; an explicit
    ``execution.ccxt_exchange`` override wins. Defaults to "okx" so a legacy
    strategy file with no venue keeps today's behavior. "ccxt" is a backend name,
    not a venue, so it is treated as unset."""
    strategy_config = strategy_config or {}
    inst = strategy_config.get("instrument") or {}
    exec_blk = strategy_config.get("execution") or {}
    venue = str(exec_blk.get("ccxt_exchange") or inst.get("exchange") or "").strip().lower()
    if venue in ("", "ccxt"):
        venue = "okx"
    return venue


def _venue_symbols(venue) -> list:
    """Assets of active strategies whose venue matches ``venue`` (dedup, ordered).

    Falls back to all trial symbols when no active strategy declares that venue, so
    a legacy/empty deploy still resolves the OKX default instead of an empty read."""
    import dashboard as _dash
    want = str(venue or "").strip().lower()
    out = []
    for strategy in _dash.active_strategies():
        if _dash._strategy_venue(strategy) != want:
            continue
        sym = strategy.get("asset")
        if sym and sym not in out:
            out.append(sym)
    return out or _dash.trial_symbols()


def _strategy_executor(strategy_config, mode):
    """Build a CCXT executor for a strategy's specific ``(venue, mode)`` environment.

    Delegates to :func:`_dashboard_executor` (the single executor seam tests stub)
    after pinning the strategy's own venue and mapping mode -> ``simulated_trading``
    ("demo"/"pause" -> sandbox, "live" -> real venue). Returns ``(executor, err)``."""
    import dashboard as _dash
    venue = _dash._strategy_venue(strategy_config)
    simulated = str(mode or "").lower() != "live"
    cfg = {
        "execution": {
            "exchange": "ccxt",  # backend name; venue is ccxt_exchange
            "ccxt_exchange": venue,
            "simulated_trading": simulated,
        },
        "instrument": (strategy_config or {}).get("instrument") or {},
    }
    return _dash._dashboard_executor(cfg, simulated_trading=simulated)


def strategy_live_snapshot(strategy_config, mode):
    """Live position snapshot for a strategy's specific ``(venue, mode)`` environment.

    Same read path as :func:`okx_live_snapshot` but venue-aware: builds the executor
    for the strategy's own venue, iterates only that venue's symbols, caches under
    ``snapshot:{venue}:{mode}`` (venue+mode share one exchange account), and tags the
    result with ``venue``. Fail-closed: a live read with HERMX_LIVE_TRADING disarmed
    degrades to the demo snapshot with a logged warning (Principle 5)."""
    import dashboard as _dash
    venue = _dash._strategy_venue(strategy_config)
    simulated = str(mode or "").lower() != "live"
    mode_key = "demo" if simulated else "live"
    if not simulated and not _dash.live_trading_enabled()[0]:
        print(
            f"[dashboard] live positions snapshot requested for {venue} but "
            "HERMX_LIVE_TRADING is disarmed; falling back to demo read (fail-closed).",
            file=sys.stderr,
        )
        return _dash.strategy_live_snapshot(strategy_config, "demo")

    now = time.time()
    snap_cache_key = f"snapshot:{venue}:{mode_key}"
    expiry_cache_key = f"expires_at:{venue}:{mode_key}"
    cached = _dash._OKX_LIVE_CACHE.get(snap_cache_key)
    if cached is not None and now < float(_dash._OKX_LIVE_CACHE.get(expiry_cache_key) or 0):
        return cached
    snapshot = {
        "ok": False,
        "positions": {},
        "error": None,
        "generated_at": None,
        "venue": venue,
        "mode": mode_key,
        "simulated_trading": simulated,
    }
    executor, exec_err = _dash._strategy_executor(strategy_config, mode_key)
    if executor is None:
        snapshot["error"] = exec_err or "dashboard_executor_unavailable"
        return snapshot
    config = {"instrument": (strategy_config or {}).get("instrument") or {}}
    try:
        payload = executor.health() or {}
        if not bool(payload.get("ok")):
            snapshot["error"] = str(payload.get("error") or "executor_health_failed")
        else:
            by_inst = {row.get("instId"): row for row in (payload.get("positions") or [])}
            public_marks = _dash.mark_prices(config)
            positions = {}
            for sym in _dash._venue_symbols(venue):
                inst = _dash.strategy_inst_id(config, sym)
                row = by_inst.get(inst) or {}
                pos_qty = _dash.as_float(row.get("pos")) or 0.0
                side = "FLAT"
                if pos_qty > 0:
                    side = "LONG"
                elif pos_qty < 0:
                    side = "SHORT"
                positions[sym] = {
                    "inst_id": inst,
                    "side": side,
                    "pos": pos_qty,
                    "avg_px": _dash.as_float(row.get("avgPx")),
                    "notional_usd": _dash.as_float(row.get("notionalUsd")),
                    "upl": _dash.as_float(row.get("upl")),
                    "realized_pnl": _dash.as_float(row.get("realizedPnl")),
                    "leverage": row.get("lever"),
                    "margin_mode": row.get("mgnMode"),
                    "mark_px": _dash.as_float(row.get("markPx")),
                    "last": _dash.as_float(row.get("last")) or public_marks.get(sym),
                    "imr": _dash.as_float(row.get("imr")),
                }
            snapshot = {
                "ok": True,
                "generated_at": payload.get("generated_at"),
                "account": payload.get("account") or {},
                "positions": positions,
                "error": None,
                "venue": venue,
                "mode": mode_key,
                "simulated_trading": simulated,
            }
    except Exception as exc:
        snapshot["error"] = str(exc)
    _dash._OKX_LIVE_CACHE[snap_cache_key] = snapshot
    _dash._OKX_LIVE_CACHE[expiry_cache_key] = now + _dash.OKX_LIVE_CACHE_TTL_SECONDS
    return snapshot


def strategy_order_history_snapshot(strategy_config, mode):
    """Order-history snapshot + ledger reconcile for a strategy's ``(venue, mode)``.

    The reconcile is fed the strategy's OWN venue and mode -- never the hardcoded
    ("okx", "demo") literals -- so a KuCoin-live strategy's closes land in the ledger
    tagged kucoin/live, not misattributed to okx/demo."""
    import dashboard as _dash
    venue = _dash._strategy_venue(strategy_config)
    simulated = str(mode or "").lower() != "live"
    mode_key = "demo" if simulated else "live"
    now = time.time()
    snap_cache_key = f"snapshot:{venue}:{mode_key}"
    expiry_cache_key = f"expires_at:{venue}:{mode_key}"
    cached = _dash._OKX_ORDER_HISTORY_CACHE.get(snap_cache_key)
    if cached is not None and now < float(_dash._OKX_ORDER_HISTORY_CACHE.get(expiry_cache_key) or 0):
        return cached
    snapshot = {"ok": False, "rows": [], "error": None, "generated_at": None,
                "venue": venue, "mode": mode_key}
    executor, exec_err = _dash._strategy_executor(strategy_config, mode_key)
    if executor is None:
        snapshot["error"] = exec_err or "dashboard_executor_unavailable"
        return snapshot
    config = {"instrument": (strategy_config or {}).get("instrument") or {}}
    try:
        inst_ids = [_dash.strategy_inst_id(config, sym) for sym in _dash._venue_symbols(venue)]
        rows = executor.get_order_history_raw(inst_ids, limit=100)
        # P0-1: age-out detector. A saturated (100-row) window whose oldest row
        # post-dates our newest recorded close means a close may have aged out of
        # the readable window before any reconcile folded it into the ledger.
        # Detection-only; wrapped so it can never fail the read-only snapshot.
        try:
            from pnl_ledger import _row_ts, max_recorded_closed_at

            saturated = len(rows or []) >= 100
            if saturated and rows:
                oldest_ms = min(_row_ts(r) for r in rows)
                high_water = max_recorded_closed_at(venue, mode_key)
                if high_water is not None and oldest_ms > high_water:
                    from webhook_receiver import (
                        RECONCILE_ALERT_MISMATCH,
                        emit_reconcile_alert,
                    )

                    emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, {
                        "stage": "history_window_ageout",
                        "venue": venue,
                        "mode": mode_key,
                        "oldest_ms": oldest_ms,
                        "high_water_ms": high_water,
                        "gap_ms": oldest_ms - high_water,
                    })
        except Exception:
            pass
        # Fold HermX close rows into the durable ledger with THIS strategy's venue+mode.
        # Wrapped so a reconcile failure can never fail the (read-only) snapshot.
        try:
            from pnl_ledger import reconcile_from_order_history

            reconcile_from_order_history(rows or [], venue, mode_key)
        except Exception:
            pass
        snapshot = {
            "ok": True,
            "generated_at": now,
            "rows": rows or [],
            "error": None,
            "venue": venue,
            "mode": mode_key,
        }
    except Exception as exc:
        snapshot["error"] = str(exc)
    _dash._OKX_ORDER_HISTORY_CACHE[snap_cache_key] = snapshot
    _dash._OKX_ORDER_HISTORY_CACHE[expiry_cache_key] = now + _dash.OKX_ORDER_HISTORY_CACHE_TTL_SECONDS
    return snapshot


def _snapshot_for_env(okx_live_by_env, okx_live_by_mode, venue, mode):
    """Pick the positions snapshot matching a strategy's ``(venue, mode)`` environment.

    Prefers the per-``{venue}:{mode}`` map; falls back to the legacy mode-only map
    (:func:`_snapshot_for_mode`) so a caller that only has the demo/live snapshots
    still resolves, then to an empty snapshot so a missing entry never raises."""
    import dashboard as _dash
    by_env = okx_live_by_env or {}
    key = f"{str(venue or 'okx').strip().lower()}:{'live' if str(mode or '').lower() == 'live' else 'demo'}"
    hit = by_env.get(key)
    if hit is not None:
        return hit
    return _dash._snapshot_for_mode(okx_live_by_mode, mode)


def okx_live_snapshot(config, simulated_trading=True):
    import dashboard as _dash
    simulated = bool(simulated_trading)
    mode_key = "demo" if simulated else "live"
    # Fail-closed (Principle 5): never attempt a live venue read unless the global
    # HERMX_LIVE_TRADING kill switch is armed. A strategy toggled live while the
    # switch is off degrades to the demo snapshot with a logged warning instead of
    # surfacing a connect error or, worse, silently reading the wrong account.
    if not simulated and not _dash.live_trading_enabled()[0]:
        print(
            "[dashboard] live positions snapshot requested but HERMX_LIVE_TRADING is "
            "disarmed; falling back to demo read (fail-closed).",
            file=sys.stderr,
        )
        return _dash.okx_live_snapshot(config, simulated_trading=True)

    now = time.time()
    snap_cache_key = f"snapshot:{mode_key}"
    expiry_cache_key = f"expires_at:{mode_key}"
    cached = _dash._OKX_LIVE_CACHE.get(snap_cache_key)
    if cached is not None and now < float(_dash._OKX_LIVE_CACHE.get(expiry_cache_key) or 0):
        return cached
    snapshot = {
        "ok": False,
        "positions": {},
        "error": None,
        "generated_at": None,
        "mode": mode_key,
        "simulated_trading": simulated,
    }
    executor, exec_err = _dash._dashboard_executor(config, simulated_trading=simulated)
    if executor is None:
        snapshot["error"] = exec_err or "dashboard_executor_unavailable"
        return snapshot
    try:
        payload = executor.health() or {}
        if not bool(payload.get("ok")):
            snapshot["error"] = str(payload.get("error") or "executor_health_failed")
        else:
            by_inst = {row.get("instId"): row for row in (payload.get("positions") or [])}
            public_marks = _dash.mark_prices(config)
            positions = {}
            for sym in _dash.trial_symbols(config):
                inst = _dash.strategy_inst_id(config, sym)
                row = by_inst.get(inst) or {}
                pos_qty = _dash.as_float(row.get("pos")) or 0.0
                side = "FLAT"
                if pos_qty > 0:
                    side = "LONG"
                elif pos_qty < 0:
                    side = "SHORT"
                positions[sym] = {
                    "inst_id": inst,
                    "side": side,
                    "pos": pos_qty,
                    "avg_px": _dash.as_float(row.get("avgPx")),
                    "notional_usd": _dash.as_float(row.get("notionalUsd")),
                    "upl": _dash.as_float(row.get("upl")),
                    "realized_pnl": _dash.as_float(row.get("realizedPnl")),
                    "leverage": row.get("lever"),
                    "margin_mode": row.get("mgnMode"),
                    "mark_px": _dash.as_float(row.get("markPx")),
                    "last": _dash.as_float(row.get("last")) or public_marks.get(sym),
                    "imr": _dash.as_float(row.get("imr")),
                }
            snapshot = {
                "ok": True,
                "generated_at": payload.get("generated_at"),
                "account": payload.get("account") or {},
                "positions": positions,
                "error": None,
                "mode": mode_key,
                "simulated_trading": simulated,
            }
    except Exception as exc:
        snapshot["error"] = str(exc)
    _dash._OKX_LIVE_CACHE[snap_cache_key] = snapshot
    _dash._OKX_LIVE_CACHE[expiry_cache_key] = now + _dash.OKX_LIVE_CACHE_TTL_SECONDS
    return snapshot


def _snapshot_for_mode(okx_live_by_mode, mode):
    """Pick the positions snapshot matching a strategy's effective mode.

    Only 'live' reads the live account; 'demo' and 'pause' both read the demo
    sandbox (pause's execution_mode is demo). Falls back to the demo snapshot, then
    to an empty snapshot, so a missing live snapshot never raises."""
    by_mode = okx_live_by_mode or {}
    key = "live" if str(mode or "").lower() == "live" else "demo"
    return by_mode.get(key) or by_mode.get("demo") or {"ok": False, "positions": {}, "error": None}


def _executor_venue_mode(executor, config):
    """Resolve the (venue, mode) an executor actually connected to.

    Read from the executor's own ``execution_cfg`` (the venue it dialed and its
    ``simulated_trading`` flag) so reconcile is never mislabeled by a hardcoded
    literal. Falls back to the config-derived venue, and returns ``mode=None``
    (unknown) rather than a fabricated ``"demo"`` when the executor can't report
    it — an honest unknown is safer than a wrong default."""
    import dashboard as _dash
    venue = None
    mode = None
    exec_cfg = getattr(executor, "execution_cfg", None)
    if isinstance(exec_cfg, dict):
        raw = str(exec_cfg.get("ccxt_exchange") or exec_cfg.get("exchange") or "").strip().lower()
        if raw and raw != "ccxt":
            venue = raw
        if "simulated_trading" in exec_cfg:
            mode = "demo" if bool(exec_cfg.get("simulated_trading", True)) else "live"
    if venue is None:
        venue = _dash._strategy_venue(config)
    return venue, mode


def okx_order_history_snapshot(config):
    import dashboard as _dash
    now = time.time()
    cached = _dash._OKX_ORDER_HISTORY_CACHE.get("snapshot")
    if cached is not None and now < float(_dash._OKX_ORDER_HISTORY_CACHE.get("expires_at") or 0):
        return cached
    snapshot = {"ok": False, "rows": [], "error": None, "generated_at": None}
    executor, exec_err = _dash._dashboard_executor(config)
    if executor is None:
        snapshot["error"] = exec_err or "dashboard_executor_unavailable"
        return snapshot
    try:
        inst_ids = [_dash.strategy_inst_id(config, sym) for sym in _dash.trial_symbols(config)]
        rows = executor.get_order_history_raw(inst_ids, limit=100)
        # Fold any HermX close rows into the durable closed-trade ledger. Deduped
        # by composite key, so re-running the snapshot is idempotent. Wrapped so a
        # reconcile failure can never fail the (read-only) snapshot.
        #
        # Thread the ACTUAL (venue, mode) read off the executor that fetched these
        # rows — never the hardcoded ("okx","demo") literal, which would mislabel
        # every non-OKX / live close (code-quality rule: reconcile call sites must
        # use the actual (venue, mode)). Mode defaults to None (unknown), never a
        # fabricated "demo", when the executor can't report it.
        try:
            from pnl_ledger import reconcile_from_order_history

            venue, mode = _dash._executor_venue_mode(executor, config)
            reconcile_from_order_history(rows or [], venue, mode)
        except Exception:
            pass
        snapshot = {
            "ok": True,
            "generated_at": now,
            "rows": rows or [],
            "error": None,
        }
    except Exception as exc:
        snapshot["error"] = str(exc)
    _dash._OKX_ORDER_HISTORY_CACHE["snapshot"] = snapshot
    _dash._OKX_ORDER_HISTORY_CACHE["expires_at"] = now + _dash.OKX_ORDER_HISTORY_CACHE_TTL_SECONDS
    return snapshot


def symbol_from_inst_id(config, inst_id):
    import dashboard as _dash
    for sym in _dash.trial_symbols(config):
        if _dash.strategy_inst_id(config, sym) == inst_id:
            return sym
    if not inst_id:
        return "-"
    # Venue-neutral derivation (shared helper): drop the instrument-type suffix + unify
    # separators -- handles OKX-native BTC-USDT-SWAP and CCXT-unified BTC/USDT:USDT alike,
    # instead of a hardcoded -USDT-SWAP replace that only understood okx swaps.
    return _dash.strategy_asset({"inst_id": inst_id})


def signed_position_side(value):
    import dashboard as _dash
    qty = _dash.as_float(value) or 0.0
    if qty > 0:
        return "LONG"
    if qty < 0:
        return "SHORT"
    return "FLAT"


def okx_order_notional(order, plan):
    import dashboard as _dash
    state = order.get("order_state") or {}
    planned = order.get("planned") or {}
    instrument = plan.get("instrument") or {}
    size = _dash.as_float(_dash.first_present(state.get("accFillSz"), planned.get("sz")))
    price = _dash.as_float(_dash.first_present(state.get("avgPx"), state.get("fillPx")))
    ct_val = _dash.as_float(instrument.get("ctVal")) or 1.0
    if size is None or price is None:
        return None
    return abs(size) * price * ct_val


def epoch_ms(value):
    import dashboard as _dash
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if text.isdigit():
            return int(text)
        dt = _dash.parse_dt(text)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def okx_history_side_for_close(action):
    action = str(action or "").upper()
    if action == "CLOSE_LONG":
        return "sell"
    if action == "CLOSE_SHORT":
        return "buy"
    return None


def bool_text(value):
    return str(value).lower() in {"true", "1", "yes", "y"}


def order_history_notional(row, ct_val=1.0):
    import dashboard as _dash
    size = _dash.as_float(_dash.first_present(row.get("accFillSz"), row.get("fillSz"), row.get("sz")))
    price = _dash.as_float(row.get("avgPx"))
    if size is None or price is None:
        return None
    return abs(size) * price * (_dash.as_float(ct_val) or 1.0)


def enrich_close_rows_with_okx_history(records, history_rows):
    import dashboard as _dash
    if not history_rows:
        return records
    used = set()
    for row in records:
        action = str(row.get("okx_action") or "").upper()
        if not action.startswith("CLOSE_"):
            continue
        if row.get("okx_price") is not None or row.get("realized_pnl") is not None:
            continue
        wanted_side = _dash.okx_history_side_for_close(action)
        row_ms = _dash.epoch_ms(row.get("received_at"))
        best = None
        best_delta = None
        for idx, hist in enumerate(history_rows):
            if idx in used:
                continue
            if hist.get("instId") != row.get("inst_id"):
                continue
            if str(hist.get("side") or "").lower() != wanted_side:
                continue
            if not _dash.bool_text(hist.get("reduceOnly")):
                continue
            hist_ms = _dash.epoch_ms(hist.get("uTime") or hist.get("cTime"))
            if row_ms is None or hist_ms is None:
                continue
            delta = abs(hist_ms - row_ms)
            if delta > 120000:
                continue
            if best is None or delta < best_delta:
                best = (idx, hist)
                best_delta = delta
        if not best:
            continue
        idx, hist = best
        used.add(idx)
        avg_px = _dash.as_float(hist.get("avgPx"))
        if avg_px is not None and row.get("alert_price"):
            row["slippage_pct"] = (avg_px / float(row["alert_price"]) - 1.0) * 100.0
        row.update(
            {
                "okx_side": hist.get("side") or row.get("okx_side"),
                "contracts": _dash.as_float(_dash.first_present(hist.get("accFillSz"), hist.get("fillSz"), hist.get("sz"))),
                "notional": _dash.order_history_notional(hist, row.get("ct_val")),
                "okx_price": avg_px,
                "fee": _dash.as_float(hist.get("fee")),
                "realized_pnl": _dash.as_float(hist.get("pnl")),
                "order_id": hist.get("ordId") or row.get("order_id"),
                "client_order_id": hist.get("clOrdId") or row.get("client_order_id"),
                "order_status": hist.get("state") or row.get("order_status"),
                "margin_mode": hist.get("tdMode") or row.get("margin_mode"),
                "leverage": hist.get("lever") or row.get("leverage"),
                "history_enriched": True,
                "history_time_delta_ms": best_delta,
            }
        )
    return records


def okx_execution_records(config, limit=500):
    import dashboard as _dash
    # Execution outcomes now live in the unified pipeline.jsonl under stage="execution"
    # (consolidated from the former executions.jsonl). Each row is {received_at,
    # okx_execution, ...} exactly as before, plus the stage/signal_id stamp.
    rows, _stats = _dash._pipeline_rows("execution", limit)
    out = []
    for row in rows:
        received_at = row.get("received_at")
        result = row.get("okx_execution") or {}
        payload = result.get("payload") or {}
        plan = payload.get("plan") or {}
        fill = payload.get("okx_fill_summary") or {}
        orders = payload.get("executed_orders") or []
        plan_inst_id = plan.get("inst_id") or plan.get("instId")
        symbol = plan.get("symbol") or _dash.symbol_from_inst_id(config, plan_inst_id)
        signal_side = str(plan.get("signal_side") or "").upper()
        signal_price = _dash.as_float(plan.get("signal_price"))
        base = {
            "received_at": received_at,
            "received_colombia": _dash.colombia_time(received_at),
            "tv_time": plan.get("tv_time"),
            "symbol": symbol,
            "inst_id": plan_inst_id,
            "signal": signal_side,
            "alert_price": signal_price,
            "mode": result.get("mode") or payload.get("mode"),
            "elapsed_ms": result.get("elapsed_ms"),
            "ok": result.get("ok"),
            "status": fill.get("status") or result.get("mode"),
            "policy": plan.get("execution_policy_label") or plan.get("execution_policy"),
            "planned_notional": _dash.nested_get(plan, "execution_intent", "planned_notional_usd"),
            "risk_weight": _dash.nested_get(plan, "execution_intent", "risk_weight"),
            "ct_val": _dash.nested_get(plan, "instrument", "ctVal"),
        }
        if not orders:
            out.append({**base, "okx_action": "-", "order_status": base["status"], "position_after": _dash.signed_position_side(_dash.nested_get(fill, "position_after_order", "pos"))})
            continue
        for order in orders:
            planned = order.get("planned") or {}
            state = order.get("order_state") or {}
            avg_px = _dash.as_float(_dash.first_present(state.get("avgPx"), state.get("fillPx")))
            slippage = None
            if avg_px is not None and signal_price:
                slippage = (avg_px / signal_price - 1.0) * 100.0
            pos_after = fill.get("position_after_order") or {}
            out.append({
                **base,
                "okx_action": order.get("action"),
                "okx_side": state.get("side") or planned.get("side"),
                "contracts": _dash.as_float(_dash.first_present(state.get("accFillSz"), planned.get("sz"))),
                "notional": _dash.okx_order_notional(order, plan),
                "okx_price": avg_px,
                "slippage_pct": slippage,
                "fee": _dash.as_float(state.get("fee")),
                "realized_pnl": _dash.as_float(state.get("pnl")),
                "position_after": _dash.signed_position_side(pos_after.get("pos")),
                "leverage": state.get("lever") or planned.get("lever") or _dash.nested_get(plan, "expected_settings", "leverage"),
                "margin_mode": state.get("tdMode") or planned.get("tdMode"),
                "order_id": order.get("ordId"),
                "client_order_id": order.get("clOrdId"),
                "order_status": state.get("state") or order.get("status"),
                "elapsed_ms": order.get("elapsed_ms") or base["elapsed_ms"],
            })
    history = _dash.okx_order_history_snapshot(config)
    if history.get("ok"):
        out = _dash.enrich_close_rows_with_okx_history(out, history.get("rows") or [])
    return out


def okx_status_label(row):
    status = row.get("order_status") or row.get("status") or "-"
    action = str(row.get("okx_action") or "").upper()
    if str(status).lower() == "skipped" and action.startswith("CLOSE_"):
        return "no position to close"
    if str(status).lower() == "skipped":
        return "not sent"
    return status


def okx_status_kind(row):
    import dashboard as _dash
    label = str(_dash.okx_status_label(row)).lower()
    if label == "filled":
        return "good"
    if "no position" in label or label == "not sent":
        return "muted"
    if "fail" in label or "error" in label or "reject" in label:
        return "bad"
    return "neutral"


def okx_leg_label(row):
    action = str(row.get("okx_action") or "").upper()
    labels = {
        "OPEN_LONG": "Open Long",
        "OPEN_SHORT": "Open Short",
        "CLOSE_LONG": "Close Long",
        "CLOSE_SHORT": "Close Short",
    }
    return labels.get(action, action.replace("_", " ").title() if action else "-")


def okx_leg_kind(row):
    action = str(row.get("okx_action") or "").upper()
    if action.startswith("OPEN_LONG") or action.startswith("CLOSE_SHORT"):
        return "good"
    if action.startswith("OPEN_SHORT") or action.startswith("CLOSE_LONG"):
        return "bad"
    return "neutral"


def okx_reduce_only_label(row):
    action = str(row.get("okx_action") or "").upper()
    if action.startswith("CLOSE_"):
        return "Yes"
    if action.startswith("OPEN_"):
        return "No"
    return "-"


def okx_display_status(row, is_live=False):
    import dashboard as _dash
    if is_live:
        return "LIVE"
    return str(_dash.okx_status_label(row) or "-").upper()


def okx_display_status_kind(row, is_live=False):
    import dashboard as _dash
    if is_live:
        return "good"
    return _dash.okx_status_kind(row)


def okx_row_details(row, is_live=False):
    import dashboard as _dash
    payload = {
        "received_at": row.get("received_at"),
        "tv_time": row.get("tv_time"),
        "mode": row.get("mode"),
        "policy": row.get("policy"),
        "action": row.get("okx_action"),
        "side": row.get("okx_side"),
        "status": _dash.okx_display_status(row, is_live),
        "reduce_only": _dash.okx_reduce_only_label(row),
        "position_after": row.get("position_after"),
        "order_id": row.get("order_id"),
        "client_order_id": row.get("client_order_id"),
        "latency_ms": row.get("elapsed_ms"),
        "history_enriched": bool(row.get("history_enriched")),
        "history_time_delta_ms": row.get("history_time_delta_ms"),
    }
    note = ""
    action = str(row.get("okx_action") or "").upper()
    if action.startswith("CLOSE_") and row.get("okx_price") is None:
        note = "Close was executed through OKX close-position. Execution is verified; fill/PnL enrichment requires OKX bills/order-history sync."
    elif action.startswith("CLOSE_") and row.get("history_enriched"):
        note = "Close was executed through OKX close-position; fill, fee and realized PnL were reconciled from OKX order history."
    elif is_live:
        note = "This is the current open OKX position. PnL is live UPL from OKX."
    return (
        '<details class="row-details"><summary>i</summary>'
        + (f'<p>{_dash.esc(note)}</p>' if note else "")
        + '<pre>'
        + _dash.esc(json.dumps(payload, indent=2, ensure_ascii=False))
        + '</pre></details>'
    )


def human_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
