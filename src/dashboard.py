#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from dashboard_core import (
    LEDGER_READ_STATS,
    SYMBOLS,
    as_float,
    asset_inst_id,
    build_combined_events,
    colombia_time,
    display_time,
    okx_swap_tickers,
    parse_dt,
    read_jsonl_stats,
    shadow_config,
)
from hermx_shared import canonical_timeframe, live_trading_enabled

try:
    from executors import ExecutorFactory
except Exception:
    ExecutorFactory = None

# Order-journal lookup used by exchange_execution_records to backfill sparse (gate-blocked,
# no-payload) execution rows. Re-exported here so snapshot readers reach it via
# ``import dashboard as _dash`` (and tests can monkeypatch ``dash.latest_order_record``).
# Fail-open: if the journal module can't import, the enrichment simply finds nothing.
try:
    from orders.journal import latest_order_record
except Exception:
    def latest_order_record(cl_ord_id):  # type: ignore[misc]
        return None

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("HERMX_ROOT") or REPO_ROOT)
LOGS = ROOT / "logs"
PORT = int(os.environ.get("HERMX_DASHBOARD_PORT") or os.environ.get("CLEAN_DASHBOARD_PORT", "8098"))
# Address the dashboard HTTP server binds to. Default 127.0.0.1 keeps
# bare-host/systemd deploys loopback-only (unchanged); the Docker bridge compose
# sets HERMX_BIND_HOST=0.0.0.0 so the container is reachable on the host port map.
HERMX_BIND_HOST = (os.environ.get("HERMX_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"
DASH_AUTH_ENABLED = (os.environ.get("HERMX_DASH_AUTH") or "true").strip().lower() not in {"0", "false", "no", ""}
# Unified secret: HERMX_SECRET is the sole dashboard token (X-Dashboard-Token,
# Bearer, or Basic password). Empty/missing => fail closed (protected routes 401).
DASH_AUTH_TOKEN = (os.environ.get("HERMX_SECRET") or "").strip()
_hermes_enabled = (os.environ.get("HERMX_ADVISOR_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
BACKFILL_FILE = ROOT / "research" / "mxc-backfill-jun11-jun16.json"
STRATEGIES_DIR = ROOT / "strategies"
# Strategy-mode override state, shared (read+write) with webhook_receiver. Both
# processes resolve the same path: receiver uses DATA_DIR = HERMX_DATA_DIR (or ROOT),
# so we mirror that here. Writes are atomic (mkstemp + os.replace) -> cross-process
# concurrent writes are last-writer-wins, never corruption. See _set_strategy_override.
CONTROL_STATE_FILE = Path(os.environ.get("HERMX_DATA_DIR", ROOT)) / "control-state.json"
# Unified consolidated ledgers (JSONL ledger consolidation). pipeline.jsonl holds
# every signal-processing event tagged with a ``stage`` field; alerts.jsonl holds
# operator/reconcile/state alerts tagged with a ``kind`` field. The dashboard reads
# them through stage/kind filters (see _pipeline_rows / _alerts_rows).
PIPELINE_FILE = LOGS / "pipeline.jsonl"
ALERTS_FILE = LOGS / "alerts.jsonl"
# Read-only observability sources for the order / reconcile / operator panels.
ORDER_JOURNAL_FILE = LOGS / "order-journal.jsonl"
# Verified checkpoint written by the receiver before it seals+rotates the live segment.
# It carries the latest record per cl_ord_id (index_records) for every order folded out
# of the now-sealed segments, so the open-orders panel must merge it with the live tail
# or it would miss any order whose latest record rotated into a sealed file.
ORDER_JOURNAL_CHECKPOINT_FILE = LOGS / "order-journal.checkpoint.json"
# Order states the open-orders panel filters out (terminal); mirrors the receiver's set.
ORDER_TERMINAL_STATES_DASH = {"FILLED", "REJECTED"}
TRIAL_TAB_ID = "duo_base_dev_trial"

POLICIES = ()

# Client silent-refresh cadence (must match setInterval below). Data older than
# this is considered stale for the freshness badge / executor banner (D7).
REFRESH_INTERVAL_SECONDS = 20

MODEL_CACHE_TTL_SECONDS = 30
_MODEL_CACHE = {"expires_at": 0.0, "model": None}
# Single-flight lock: serializes cold synchronous builds (first /api request)
# with the background refresh loop so only one thread pays the ~15s build cost.
_MODEL_BUILD_LOCK = threading.Lock()
OKX_LIVE_CACHE_TTL_SECONDS = 5
_OKX_LIVE_CACHE = {"expires_at": 0.0, "snapshot": None}
OKX_ORDER_HISTORY_CACHE_TTL_SECONDS = 15
_OKX_ORDER_HISTORY_CACHE = {"expires_at": 0.0, "snapshot": None}

POLICY_DESCRIPTIONS = {
    "duo_raw": "Baseline. Every valid Duo Crypto BUY/SELL flips the position at full size.",
    "duo_regime_rsi_30m": "Selected candidate. Duo Crypto is the trigger; 30m Regime/Price Pressure and RSI decide whether the new reverse entry is full, quarter size, or skipped. Opposite signals always close first.",
}

ASSET_META = {
    "XRPUSDT": {
        "name": "XRP",
        "logo": "https://assets.coingecko.com/coins/images/44/large/xrp-symbol-white-128.png",
    },
    "SOLUSDT": {
        "name": "Solana",
        "logo": "https://assets.coingecko.com/coins/images/4128/large/solana.png",
    },
    "ETHUSDT": {
        "name": "Ethereum",
        "logo": "https://assets.coingecko.com/coins/images/279/large/ethereum.png",
    },
    "BTCUSDT": {
        "name": "Bitcoin",
        "logo": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png",
    },
}


# canonical_timeframe is imported from hermx_shared (Phase 4 / D8) — the receiver
# and dashboard now share one implementation so the alias tables cannot drift.


_INSTRUMENT_TYPE_SUFFIXES = {"SWAP", "FUTURES", "FUTURE", "PERP", "SPOT", "MARGIN", "OPTION"}


def strategy_asset(row) -> str:
    """BASE+QUOTE asset symbol for a strategy card (e.g. ``BTCUSDT``).

    v3 dropped the explicit ``asset`` field; derive it from the canonical
    ``instrument.inst_id`` (OKX-native ``BTC-USDT-SWAP`` or CCXT-unified
    ``BTC/USDT:USDT``). A still-present top-level ``asset`` is honored.
    """
    explicit = str((row or {}).get("asset") or "").strip().upper()
    if explicit:
        return explicit
    inst = (row or {}).get("instrument") or {}
    inst_id = str(inst.get("inst_id") or (row or {}).get("inst_id") or "")
    if not inst_id:
        return ""
    core = inst_id.split(":", 1)[0].replace("/", "-")
    parts = [p for p in core.split("-") if p]
    if len(parts) >= 3 and parts[-1].upper() in _INSTRUMENT_TYPE_SUFFIXES:
        parts = parts[:-1]
    return "".join(parts).upper()


def load_strategy_files():
    rows = []
    if not STRATEGIES_DIR.exists():
        return rows
    for path in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            row["_path"] = str(path)
            row["asset"] = strategy_asset(row)
            row["timeframe"] = canonical_timeframe(row.get("timeframe"))
            rows.append(row)
        except Exception:
            continue
    # No hardcoded per-symbol sort map (D5) — order deterministically by the
    # strategy's own identity so adding/removing a file needs no code change.
    return sorted(rows, key=lambda r: (r.get("asset") or "", r.get("strategy_id") or ""))


def is_strategy_active(strategy) -> bool:
    """Whether a strategy file should render a card (D5).

    A card appears for any strategy with a valid file. All strategies are active;
    the per-strategy execution_mode (demo/live) decides sandbox vs real venue.
    """
    return True


_VALID_STRATEGY_MODES = frozenset({"pause", "demo", "live"})
# Flag mapping must stay in sync with webhook_receiver._STRATEGY_MODE_FLAGS.
# ``submit_orders`` gates submission (pause = off); ``execution_mode`` selects the
# account (demo sandbox vs live real).
_STRATEGY_MODE_FLAGS = {
    "pause": {"execution_mode": "demo", "submit_orders": False},
    "demo":  {"execution_mode": "demo", "submit_orders": True},
    "live":  {"execution_mode": "live", "submit_orders": True},
}
# Legacy UI-mode label remap: old control-state.json may carry "shadow" or "paper".
# "shadow" was the old pause concept (validate+ledger, no orders) -> "pause";
# "paper" was the sandbox-submit concept -> "demo".
_LEGACY_STRATEGY_MODE_ALIASES = {"shadow": "pause", "paper": "demo"}


def _normalize_override_modes(overrides: dict) -> dict:
    """Remap legacy override 'mode' labels (shadow -> pause, paper -> demo) in-place.
    Only the display label is touched; execution_mode/submit_orders flags are left as-is."""
    if not isinstance(overrides, dict):
        return overrides
    for entry in overrides.values():
        if isinstance(entry, dict):
            alias = _LEGACY_STRATEGY_MODE_ALIASES.get(entry.get("mode"))
            if alias:
                entry["mode"] = alias
    return overrides


def _load_control_state() -> dict:
    """Read control-state.json read-only. Fail-safe: returns {} on any error so a
    missing/corrupt file never breaks the dashboard. We do NOT write a default file
    here (unlike the receiver) — the dashboard is a pure reader for this path except
    via the explicit override setters below."""
    try:
        if not CONTROL_STATE_FILE.exists():
            return {}
        state = json.loads(CONTROL_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return {}
        if isinstance(state.get("strategy_overrides"), dict):
            _normalize_override_modes(state["strategy_overrides"])
        return state
    except Exception:
        return {}


def _save_control_state(state: dict) -> None:
    """Atomic write: mkstemp + fsync + os.replace, mirroring
    webhook_receiver.save_control_state. No cross-process lock (the receiver's
    _STATE_WRITE_LOCK is per-process); atomic replace makes concurrent writers
    last-writer-wins, never corrupt."""
    fd, tmp_path = tempfile.mkstemp(prefix=f"{CONTROL_STATE_FILE.name}.", suffix=".tmp", dir=str(CONTROL_STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_path).replace(CONTROL_STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _set_strategy_override(strategy_id: str, mode: str) -> bool:
    """Set a per-strategy execution-mode override. Read-modify-write of control-state.
    Mirrors webhook_receiver.set_strategy_override (same flag mapping + entry shape)."""
    sid = str(strategy_id or "").strip()
    mode = str(mode or "").strip().lower()
    if not sid or mode not in _VALID_STRATEGY_MODES:
        return False
    now = datetime.now(timezone.utc).isoformat()
    state = _load_control_state()
    overrides = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
    overrides[sid] = {"mode": mode, **_STRATEGY_MODE_FLAGS[mode], "set_at": now}
    state["strategy_overrides"] = overrides
    state["updated_at"] = now
    _save_control_state(state)
    return True


def _clear_strategy_override(strategy_id: str) -> bool:
    """Remove a strategy override, reverting to the strategy file's mode."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    state = _load_control_state()
    overrides = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
    if sid not in overrides:
        return False
    overrides.pop(sid)
    state["strategy_overrides"] = overrides
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_control_state(state)
    return True


def _set_accounting_start(strategy_id: str, start_ms) -> bool:
    """Set/clear a per-strategy accounting-window start (ms epoch) in control-state.
    Mirrors webhook_receiver.set_accounting_start; None/absent clears the window.
    Additive: touches only the accounting_windows key, never strategy_overrides."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    if start_ms is None:
        return _clear_accounting_start(sid)
    try:
        ts = int(start_ms)
    except (TypeError, ValueError):
        return False
    if ts < 0:
        return False
    now = datetime.now(timezone.utc).isoformat()
    state = _load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    windows[sid] = {"accounting_start_at": ts, "set_at": now}
    state["accounting_windows"] = windows
    state["updated_at"] = now
    _save_control_state(state)
    return True


def _clear_accounting_start(strategy_id: str) -> bool:
    """Remove a strategy's accounting window (revert to whole-ledger total)."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    state = _load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    if sid not in windows:
        return False
    windows.pop(sid)
    state["accounting_windows"] = windows
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_control_state(state)
    return True


def _accounting_start_for(strategy_id: str, ctrl_state=None):
    """The strategy's accounting-window start (ms epoch), or None if unset.
    ``ctrl_state`` may be a pre-loaded control-state dict to avoid re-reading."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    state = ctrl_state if isinstance(ctrl_state, dict) else _load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    entry = windows.get(sid)
    if isinstance(entry, dict):
        try:
            v = entry.get("accounting_start_at")
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


# Phase A (A2) global trading_state mirror. Mirrors webhook_receiver.get/set_trading_state
# (control-state.json is the single shared artifact). Only 'active'/'reducing' are valid;
# an unknown/legacy value reads as 'active' (fail-open to normal trading).
_VALID_TRADING_STATES = frozenset({"active", "reducing"})


def _get_trading_state(ctrl_state=None) -> str:
    """The global trading_state, defaulting to 'active'. ``ctrl_state`` may be a
    pre-loaded control-state dict to avoid re-reading."""
    state = ctrl_state if isinstance(ctrl_state, dict) else _load_control_state()
    st = str(state.get("trading_state") or "active").strip().lower()
    return st if st in _VALID_TRADING_STATES else "active"


def _set_trading_state(state: str) -> bool:
    """Set the global trading_state. Read-modify-write of control-state; validates the
    input ('active'|'reducing'). Mirrors webhook_receiver.set_trading_state."""
    st = str(state or "").strip().lower()
    if st not in _VALID_TRADING_STATES:
        return False
    now = datetime.now(timezone.utc).isoformat()
    cs = _load_control_state()
    cs["trading_state"] = st
    cs["updated_at"] = now
    _save_control_state(cs)
    return True


# --- REFACTOR_PLAN Phase 7 sub-step 1 shim -----------------------------------
# Executor construction + live/history snapshots moved to src/dashboard/snapshots.py.
# This file must keep resolving as the top-level module `dashboard` (tests and the
# deploy entrypoint depend on it), so src/dashboard/ deliberately has NO
# __init__.py -- a regular `dashboard` package would shadow this file on import.
# Instead, extend __path__ so `dashboard.snapshots` imports as a submodule of this
# module. The re-export keeps every dashboard.<fn> attribute real and
# monkeypatchable; snapshots.py dereferences back through `import dashboard` at
# call time, so patches applied here are observed by the moved callers.
__path__ = [str(Path(__file__).resolve().parent / "dashboard")]

from dashboard.snapshots import (  # noqa: E402  re-export shim
    load_json,
    real_decisions,
    backfill_state,
    load_events,
    mark_prices,
    _dashboard_executor,
    _strategy_venue,
    _venue_symbols,
    _strategy_executor,
    strategy_live_snapshot,
    strategy_order_history_snapshot,
    _snapshot_for_env,
    okx_live_snapshot,
    _snapshot_for_mode,
    _executor_venue_mode,
    okx_order_history_snapshot,
    symbol_from_inst_id,
    signed_position_side,
    epoch_ms,
    okx_history_side_for_close,
    bool_text,
    order_history_notional,
    enrich_close_rows_with_okx_history,
    exchange_execution_records,
    exchange_status_label,
    exchange_status_kind,
    exchange_leg_label,
    exchange_leg_kind,
    exchange_reduce_only_label,
    exchange_display_status,
    exchange_display_status_kind,
    okx_row_details,
    human_age,
)


# --- REFACTOR_PLAN Phase 7 sub-step 2 shim -----------------------------------
# Data projections, health/freshness summaries, dashboard model + P&L contracts
# and API payloads moved to src/dashboard/model.py (same no-__init__ layout as
# sub-step 1 above; model.py dereferences back through `import dashboard` at
# call time, so monkeypatches applied on this module are observed by the moved
# callers). _MODEL_CACHE / _MODEL_BUILD_LOCK stay defined at the top of this
# file because tests mutate dash_mod._MODEL_CACHE in place.
from dashboard.model import (  # noqa: E402  re-export shim
    active_strategies,
    trial_symbols,
    strategy_inst_id,
    _pipeline_rows,
    SIGNALS_DEFAULT_N,
    _signal_projection,
    signals_payload,
    _alerts_rows,
    strategy_alert_rows,
    executor_health_summary,
    ledger_health_summary,
    freshness_summary,
    dashboard_model,
    _build_dashboard_model,
    _effective_strategy_mode,
    _strategy_pnl_contract,
    _ledger_net_realized,
    portfolio_contract,
    api_payload,
    health_payload,
)


# --- REFACTOR_PLAN Phase 7 sub-step 3 shim -----------------------------------
# Formatting/HTML-escape helpers, HTML section builders and the legacy
# server-rendered page (render() + CSS) moved to src/dashboard/render.py (same
# no-__init__ layout as sub-steps 1-2 above; render.py dereferences back
# through `import dashboard` at call time, so monkeypatches applied on this
# module are observed by the moved callers -- including render.py functions
# calling each other).
from dashboard.render import (  # noqa: E402  re-export shim
    money,
    pct,
    num,
    esc,
    badge,
    side_kind,
    action_kind,
    trade_effect,
    first_present,
    nested_get,
    fmt_seconds,
    metric_cards,
    reason_details,
    first_okx_trade_map,
    okx_live_entry_state,
    okx_live_card,
    okx_execution_table,
    metric_cards_colored,
    strategy_card,
    strategy_alert_table,
    strategy_execution_rows,
    _read_order_checkpoint_index,
    order_journal_open_orders,
    reconcile_alert_records,
    operator_alert_records,
    _ledger_stat_note,
    _alert_detail_str,
    _state_kind,
    open_orders_table,
    reconcile_alert_table,
    operator_alert_table,
    order_state_section,
    strategy_trial_tab,
    banner,
    status_banners,
    strategy_indicator_label,
    summary_cards,
    render,
    CSS,
)


# --- Built Next.js SPA (dashboard-ui/out) + dev CORS --------------------------
# When dashboard-ui/out/ exists (after `npm run build`), the static export takes
# over "/" and all asset paths; until then the legacy server-rendered HTML is the
# fallback. DEV_CORS_ENABLED (dev-only; hard-coded off per the flag fluff audit --
# security-adjacent, never on in prod) would let the Next dev server (localhost:3001)
# call /api and /health cross-origin; flip in-source only for local development.
STATIC_DIR = REPO_ROOT / "dashboard-ui" / "out"
DEV_CORS_ENABLED = False
DEV_CORS_ORIGIN = "http://localhost:3001"
STATIC_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8",
}


# --- REFACTOR_PLAN Phase 7 sub-step 4 shim -----------------------------------
# The HTTP request Handler (read + control routes) and the background model-
# cache refresh loop moved to src/dashboard/server.py (same no-__init__ layout
# as sub-steps 1-3 above). Handler methods dereference back through
# `import dashboard` at request time, so DASH_AUTH_*/STATIC_DIR monkeypatches
# and route-callable patches applied on this module are observed by the moved
# server. STATIC_DIR / DEV_CORS_* / STATIC_MIME_TYPES stay defined above
# because tests monkeypatch them here and they are repo/env-bound.
from dashboard.server import (  # noqa: E402  re-export shim
    Handler,
    _refresh_dashboard_cache_loop,
)


if __name__ == "__main__":
    if DASH_AUTH_ENABLED and not DASH_AUTH_TOKEN:
        print("[dashboard] HERMX_DASH_AUTH enabled but HERMX_SECRET is blank; failing closed with 401 for protected routes.", file=sys.stderr)
    server = ThreadingHTTPServer((HERMX_BIND_HOST, PORT), Handler)
    # Keep the model cache warm with a periodic background rebuild; the server
    # binds and serves immediately while refreshes happen off the request path.
    threading.Thread(target=_refresh_dashboard_cache_loop, daemon=True).start()
    server.serve_forever()
