#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import hmac
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dashboard_core import (
    LEDGER_READ_STATS,
    SYMBOLS,
    as_float,
    asset_inst_id,
    build_combined_events,
    colombia_time,
    display_time,
    normalize_policy_decision,
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

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("SHADOW_ROOT", REPO_ROOT))
LOGS = ROOT / "logs"
PORT = int(os.environ.get("CLEAN_DASHBOARD_PORT", "8098"))
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
REFRESH_INTERVAL_SECONDS = int(os.environ.get("HERMX_DASH_REFRESH_SECONDS") or "20")

MODEL_CACHE_TTL_SECONDS = 10
_MODEL_CACHE = {"expires_at": 0.0, "model": None}
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


def active_strategies(strategies=None):
    rows = strategies if strategies is not None else load_strategy_files()
    return [s for s in rows if is_strategy_active(s)]


def trial_symbols(config=None):
    """Symbols the dashboard cares about, derived from active strategy files (D5).

    Falls back to the legacy static SYMBOLS list only when there are no strategy
    files at all, so an empty/misconfigured deploy still renders something.
    """
    seen = []
    for strategy in active_strategies():
        sym = strategy.get("asset")
        if sym and sym not in seen:
            seen.append(sym)
    if not seen:
        for sym in SYMBOLS:
            if sym not in seen:
                seen.append(sym)
    return seen


def strategy_inst_id(config, sym):
    # Venue-aware: the strategy file's OWN instrument block is the source of truth (a
    # kucoin spot pair, a hyperliquid perp, an okx swap all resolve to THEIR id), then
    # any explicitly configured per-asset inst_id. No hardcoded -USDT-SWAP transform --
    # an unknown symbol resolves to "" (the caller degrades gracefully, never fabricates
    # a fake okx instrument).
    for strategy in load_strategy_files():
        if strategy.get("asset") == sym:
            inst = strategy.get("instrument") or {}
            inst_id = inst.get("inst_id") or strategy.get("inst_id")
            if inst_id:
                return inst_id
    try:
        configured = asset_inst_id(config, sym)
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
    scan = scan if scan is not None else max(int(limit) * 6, 1000)
    rows, stats = read_jsonl_stats(LOGS / "pipeline.jsonl", scan)
    rows = [r for r in rows if r.get("stage") == stage]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows, stats


SIGNALS_MAX_N = 500
SIGNALS_DEFAULT_N = 50


def _signal_projection(row):
    """Project one execution-stage pipeline row to the compact /api/signals shape.

    Handles both normal TV-triggered submissions (fields nested under
    ``okx_execution.payload.plan``) and operator closes (symbol/strategy_id/kind/
    operator/reason stamped at the top level by execute_operator_close)."""
    okx = row.get("okx_execution") or {}
    payload = okx.get("payload") or {}
    plan = payload.get("plan") or {}
    intent = plan.get("execution_intent") or {}
    return {
        "ts": row.get("ts"),
        "submitted_at": row.get("received_at") or row.get("ts"),
        "symbol": row.get("symbol") or plan.get("symbol"),
        "side": plan.get("signal_side") or plan.get("target_side"),
        "strategy_id": row.get("strategy_id") or plan.get("strategy_id"),
        "mode": okx.get("mode") or payload.get("mode"),
        "reason": okx.get("reason"),
        "kind": row.get("kind"),
        "operator": row.get("operator"),
        "cl_ord_id": row.get("cl_ord_id") or intent.get("client_order_id"),
        "ok": okx.get("ok"),
        "elapsed_ms": okx.get("elapsed_ms"),
    }


def signals_payload(n=SIGNALS_DEFAULT_N, symbol=None):
    """Last ``n`` execution-stage events from pipeline.jsonl (the full TV-triggered +
    operator-close trade history), most recent first, optionally filtered by symbol.

    A missing ledger yields ``{"ok": True, "signals": [], "count": 0}``. ``n`` is
    clamped to [1, SIGNALS_MAX_N]; the underlying reverse-tail read stays bounded."""
    n = max(1, min(int(n), SIGNALS_MAX_N))
    symbol_filter = str(symbol or "").strip().upper() or None
    # Scan a wider window than n so a symbol filter still surfaces n matches when
    # the stage interleaves many symbols; the read is OOM-bounded regardless.
    scan = max(n * 12, 2000)
    rows, _stats = _pipeline_rows("execution", scan, scan=scan)
    projected = [_signal_projection(r) for r in rows]
    if symbol_filter:
        projected = [p for p in projected if str(p.get("symbol") or "").strip().upper() == symbol_filter]
    projected = list(reversed(projected))  # most recent first
    if len(projected) > n:
        projected = projected[:n]
    return {"ok": True, "signals": projected, "count": len(projected)}


def _alerts_rows(kind, limit, scan=None):
    """Read up to ``limit`` rows of one alert ``kind`` from the unified alerts.jsonl
    (kind in {operator, reconcile, state}). Returns ``(rows, stats)``."""
    scan = scan if scan is not None else max(int(limit) * 6, 500)
    rows, stats = read_jsonl_stats(LOGS / "alerts.jsonl", scan)
    rows = [r for r in rows if r.get("kind") == kind]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows, stats


def strategy_alert_rows(limit=500):
    rows, _stats = _pipeline_rows("strategy_match", limit)
    out = []
    for row in rows:
        norm = row.get("normalized") or {}
        strategy = row.get("strategy_config") or {}
        if not norm.get("strategy_id"):
            continue
        out.append({
            "received_at": row.get("received_at"),
            "received_colombia": colombia_time(row.get("received_at")),
            "strategy_id": norm.get("strategy_id"),
            "strategy_name": strategy.get("name") or norm.get("strategy_name") or norm.get("strategy_id"),
            "asset": norm.get("symbol") or strategy.get("asset"),
            "timeframe": norm.get("timeframe") or strategy.get("timeframe"),
            "side": str(norm.get("side") or "").upper(),
            "price": norm.get("tv_signal_price"),
            "tv_time": norm.get("tv_time"),
            "tv_time_colombia": colombia_time(norm.get("tv_time")),
            "duplicate": bool(row.get("duplicate")),
            "decision": nested_get(row, "strategy_decision", "decision") or nested_get(row, "decision", "decision"),
            "mode": row.get("mode"),
            "okx_mode": nested_get(row, "okx_execution", "mode"),
            "block_reason": nested_get(row, "execution_readiness", "block_reason"),
            "latency": nested_get(row, "latency", "latency_seconds"),
        })
    out.sort(key=lambda r: parse_dt(r.get("tv_time") or r.get("received_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return out


def money(value, digits=2):
    value = as_float(value)
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{digits}f}"


def pct(value):
    value = as_float(value)
    if value is None:
        return "-"
    return f"{value:,.2f}%"


def num(value, digits=4):
    value = as_float(value)
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def esc(value):
    return html.escape(str(value if value is not None else ""))


def badge(text, kind="neutral"):
    return f'<span class="badge {kind}">{esc(text)}</span>'


def side_kind(side):
    side = str(side or "").lower()
    if side in {"buy", "long"}:
        return "good"
    if side in {"sell", "short"}:
        return "bad"
    return "neutral"


def action_kind(action):
    text = str(action or "").upper()
    if "FLIP" in text:
        return "good"
    if "CLOSE ONLY" in text:
        return "warn"
    if "DUPLICATE" in text:
        return "muted"
    if "SKIP" in text:
        return "muted"
    if "CLOSE" in text and "OPEN" not in text:
        return "warn"
    if "OPEN_LONG" in text or "BUY" in text or "TRADE" in text:
        return "good"
    if "OPEN_SHORT" in text or "SELL" in text:
        return "bad"
    return "neutral"


def trade_effect(row):
    action = str(row.get("position_action") or "").upper()
    decision = str(row.get("decision") or "").upper()
    closes = "CLOSE_LONG" in action or "CLOSE_SHORT" in action
    opens = "OPEN_LONG" in action or "OPEN_SHORT" in action
    duplicate = "DUPLICATE" in action
    skips = "SKIP" in action or decision == "SKIP"
    if closes and opens:
        return "FLIP"
    if closes and skips and not opens:
        return "CLOSE ONLY"
    if opens and not closes:
        return "OPEN"
    if duplicate:
        return "DUPLICATE"
    if skips:
        return "SKIP"
    if closes:
        return "CLOSE"
    return decision or "-"


def first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def nested_get(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def fmt_seconds(value):
    value = as_float(value)
    if value is None:
        return "-"
    if abs(value) >= 60:
        return f"{value / 60.0:,.2f}m"
    return f"{value:,.2f}s"


def execution_fields(event, alert_price):
    payload = event.get("payload") or {}
    okx_fill = event.get("okx_fill") or nested_get(event, "execution_readiness", "okx_fill") or {}
    okx_price = as_float(first_present(
        event.get("okx_execution_price"),
        event.get("execution_price"),
        event.get("fill_price"),
        event.get("avg_fill_price"),
        okx_fill.get("avg_fill_price"),
        payload.get("okx_execution_price"),
        payload.get("execution_price"),
        payload.get("fill_price"),
        payload.get("avg_fill_price"),
    ))
    latency_seconds = as_float(first_present(
        event.get("execution_latency_seconds"),
        event.get("okx_latency_seconds"),
        event.get("latency_seconds"),
        nested_get(event, "latency", "latency_seconds"),
        payload.get("execution_latency_seconds"),
        payload.get("okx_latency_seconds"),
    ))
    slippage_pct = as_float(first_present(
        event.get("slippage_pct"),
        event.get("alert_execution_diff_pct"),
        okx_fill.get("slippage_pct"),
        payload.get("slippage_pct"),
        payload.get("alert_execution_diff_pct"),
    ))
    if slippage_pct is None and okx_price is not None and alert_price:
        slippage_pct = (float(okx_price) / float(alert_price) - 1.0) * 100.0
    okx_status = str(first_present(
        event.get("okx_status"),
        event.get("execution_status"),
        okx_fill.get("status"),
        payload.get("okx_status"),
        payload.get("execution_status"),
    ) or "").lower()
    okx_executed = bool(okx_price is not None or okx_status in {"filled", "executed", "partially_filled", "live_order_sent"})
    source = "OKX" if okx_executed else "PAPER"
    status = first_present(okx_status.upper() if okx_status else None, "executed" if okx_executed else None)
    if source == "PAPER":
        raw_source = str(event.get("source") or "").lower()
        if raw_source == "historical":
            status = "historical replay"
        elif raw_source == "manual_backfill":
            status = "manual backfill"
        elif raw_source == "live":
            status = "live paper"
        else:
            status = raw_source or "paper"
    return {
        "execution_source": source,
        "execution_status": status,
        "okx_price": okx_price,
        "latency_seconds": latency_seconds,
        "slippage_pct": slippage_pct,
    }


def execution_badge(row):
    source = str(row.get("execution_source") or "PAPER").upper()
    return badge("OKX" if source == "OKX" else "PAPER", "good" if source == "OKX" else "neutral")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def real_decisions(limit=10000):
    rows, _stats = _pipeline_rows("decision", limit)
    out = []
    seen = set()
    symbol_set = set(trial_symbols())
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
    return load_json(BACKFILL_FILE, {"status": "missing", "decisions": []})


def load_events():
    backfill = backfill_state()
    live_rows = real_decisions()
    _historical_with_live, hist_only, live = build_combined_events({}, live_rows)

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
        event["time_colombia"] = event.get("time_colombia") or colombia_time(event.get("time"))
        backfill_events.append(event)

    seen = {(e.get("symbol"), e.get("side"), e.get("time")) for e in hist_only}
    combined = list(hist_only)
    for event in backfill_events:
        key = (event.get("symbol"), event.get("side"), event.get("time"))
        if key not in seen:
            combined.append(event)
            seen.add(key)
    last_known = max([parse_dt(e.get("time")) for e in combined if parse_dt(e.get("time"))] or [None])
    for event in live:
        dt = parse_dt(event.get("time"))
        key = (event.get("symbol"), event.get("side"), event.get("time"))
        if last_known and dt and dt <= last_known:
            continue
        if key not in seen:
            combined.append(event)
            seen.add(key)
    combined.sort(key=lambda e: parse_dt(e.get("time")) or datetime.min.replace(tzinfo=timezone.utc))
    return {
        "events": combined,
        "historical_count": len(hist_only),
        "backfill_count": len(backfill_events),
        "live_count": sum(1 for event in combined if event.get("source") == "live"),
        "backfill": backfill,
    }


def target_side(event):
    return "long" if str(event.get("side")).lower() == "buy" else "short"


def position_pnl(pos, price, fee_rate):
    if not pos or not price:
        return {"gross": 0.0, "exit_fee": 0.0, "net": 0.0}
    entry = float(pos["entry"])
    notional = float(pos["notional"])
    if pos["side"] == "long":
        gross = notional * (float(price) / entry - 1.0)
        exit_notional = notional * (float(price) / entry)
    else:
        gross = notional * (entry / float(price) - 1.0)
        exit_notional = notional * (entry / float(price))
    exit_fee = exit_notional * fee_rate
    net = gross - float(pos.get("entry_fee") or 0.0) - exit_fee
    return {"gross": gross, "exit_fee": exit_fee, "net": net}


def mark_prices(config):
    tickers = okx_swap_tickers()
    marks = {}
    for sym in trial_symbols(config):
        inst = strategy_inst_id(config, sym)
        row = tickers.get(inst) or {}
        marks[sym] = as_float(row.get("last"))
    return marks


def _dashboard_executor(config):
    if ExecutorFactory is None:
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
        cfg["execution"] = exec_cfg
        return ExecutorFactory.create(cfg, ROOT), None
    except Exception as exc:
        return None, str(exc)


def okx_live_snapshot(config):
    now = time.time()
    cached = _OKX_LIVE_CACHE.get("snapshot")
    if cached is not None and now < float(_OKX_LIVE_CACHE.get("expires_at") or 0):
        return cached
    snapshot = {"ok": False, "positions": {}, "error": None, "generated_at": None}
    executor, exec_err = _dashboard_executor(config)
    if executor is None:
        snapshot["error"] = exec_err or "dashboard_executor_unavailable"
        return snapshot
    try:
        payload = executor.health() or {}
        if not bool(payload.get("ok")):
            snapshot["error"] = str(payload.get("error") or "executor_health_failed")
        else:
            by_inst = {row.get("instId"): row for row in (payload.get("positions") or [])}
            public_marks = mark_prices(config)
            positions = {}
            for sym in trial_symbols(config):
                inst = strategy_inst_id(config, sym)
                row = by_inst.get(inst) or {}
                pos_qty = as_float(row.get("pos")) or 0.0
                side = "FLAT"
                if pos_qty > 0:
                    side = "LONG"
                elif pos_qty < 0:
                    side = "SHORT"
                positions[sym] = {
                    "inst_id": inst,
                    "side": side,
                    "pos": pos_qty,
                    "avg_px": as_float(row.get("avgPx")),
                    "notional_usd": as_float(row.get("notionalUsd")),
                    "upl": as_float(row.get("upl")),
                    "realized_pnl": as_float(row.get("realizedPnl")),
                    "leverage": row.get("lever"),
                    "margin_mode": row.get("mgnMode"),
                    "mark_px": as_float(row.get("markPx")),
                    "last": as_float(row.get("last")) or public_marks.get(sym),
                    "imr": as_float(row.get("imr")),
                }
            snapshot = {
                "ok": True,
                "generated_at": payload.get("generated_at"),
                "account": payload.get("account") or {},
                "positions": positions,
                "error": None,
            }
    except Exception as exc:
        snapshot["error"] = str(exc)
    _OKX_LIVE_CACHE["snapshot"] = snapshot
    _OKX_LIVE_CACHE["expires_at"] = now + OKX_LIVE_CACHE_TTL_SECONDS
    return snapshot


def okx_order_history_snapshot(config):
    now = time.time()
    cached = _OKX_ORDER_HISTORY_CACHE.get("snapshot")
    if cached is not None and now < float(_OKX_ORDER_HISTORY_CACHE.get("expires_at") or 0):
        return cached
    snapshot = {"ok": False, "rows": [], "error": None, "generated_at": None}
    executor, exec_err = _dashboard_executor(config)
    if executor is None:
        snapshot["error"] = exec_err or "dashboard_executor_unavailable"
        return snapshot
    try:
        inst_ids = [strategy_inst_id(config, sym) for sym in trial_symbols(config)]
        rows = executor.get_order_history_raw(inst_ids, limit=100)
        snapshot = {
            "ok": True,
            "generated_at": now,
            "rows": rows or [],
            "error": None,
        }
    except Exception as exc:
        snapshot["error"] = str(exc)
    _OKX_ORDER_HISTORY_CACHE["snapshot"] = snapshot
    _OKX_ORDER_HISTORY_CACHE["expires_at"] = now + OKX_ORDER_HISTORY_CACHE_TTL_SECONDS
    return snapshot


def symbol_from_inst_id(config, inst_id):
    for sym in trial_symbols(config):
        if strategy_inst_id(config, sym) == inst_id:
            return sym
    if not inst_id:
        return "-"
    # Venue-neutral derivation (shared helper): drop the instrument-type suffix + unify
    # separators -- handles OKX-native BTC-USDT-SWAP and CCXT-unified BTC/USDT:USDT alike,
    # instead of a hardcoded -USDT-SWAP replace that only understood okx swaps.
    return strategy_asset({"inst_id": inst_id})


def signed_position_side(value):
    qty = as_float(value) or 0.0
    if qty > 0:
        return "LONG"
    if qty < 0:
        return "SHORT"
    return "FLAT"


def okx_order_notional(order, plan):
    state = order.get("order_state") or {}
    planned = order.get("planned") or {}
    instrument = plan.get("instrument") or {}
    size = as_float(first_present(state.get("accFillSz"), planned.get("sz")))
    price = as_float(first_present(state.get("avgPx"), state.get("fillPx")))
    ct_val = as_float(instrument.get("ctVal")) or 1.0
    if size is None or price is None:
        return None
    return abs(size) * price * ct_val


def epoch_ms(value):
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if text.isdigit():
            return int(text)
        dt = parse_dt(text)
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
    size = as_float(first_present(row.get("accFillSz"), row.get("fillSz"), row.get("sz")))
    price = as_float(row.get("avgPx"))
    if size is None or price is None:
        return None
    return abs(size) * price * (as_float(ct_val) or 1.0)


def enrich_close_rows_with_okx_history(records, history_rows):
    if not history_rows:
        return records
    used = set()
    for row in records:
        action = str(row.get("okx_action") or "").upper()
        if not action.startswith("CLOSE_"):
            continue
        if row.get("okx_price") is not None or row.get("realized_pnl") is not None:
            continue
        wanted_side = okx_history_side_for_close(action)
        row_ms = epoch_ms(row.get("received_at"))
        best = None
        best_delta = None
        for idx, hist in enumerate(history_rows):
            if idx in used:
                continue
            if hist.get("instId") != row.get("inst_id"):
                continue
            if str(hist.get("side") or "").lower() != wanted_side:
                continue
            if not bool_text(hist.get("reduceOnly")):
                continue
            hist_ms = epoch_ms(hist.get("uTime") or hist.get("cTime"))
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
        avg_px = as_float(hist.get("avgPx"))
        if avg_px is not None and row.get("alert_price"):
            row["slippage_pct"] = (avg_px / float(row["alert_price"]) - 1.0) * 100.0
        row.update(
            {
                "okx_side": hist.get("side") or row.get("okx_side"),
                "contracts": as_float(first_present(hist.get("accFillSz"), hist.get("fillSz"), hist.get("sz"))),
                "notional": order_history_notional(hist, row.get("ct_val")),
                "okx_price": avg_px,
                "fee": as_float(hist.get("fee")),
                "realized_pnl": as_float(hist.get("pnl")),
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
    # Execution outcomes now live in the unified pipeline.jsonl under stage="execution"
    # (consolidated from the former executions.jsonl). Each row is {received_at,
    # okx_execution, ...} exactly as before, plus the stage/signal_id stamp.
    rows, _stats = _pipeline_rows("execution", limit)
    out = []
    for row in rows:
        received_at = row.get("received_at")
        result = row.get("okx_execution") or {}
        payload = result.get("payload") or {}
        plan = payload.get("plan") or {}
        fill = payload.get("okx_fill_summary") or {}
        orders = payload.get("executed_orders") or []
        plan_inst_id = plan.get("inst_id") or plan.get("instId")
        symbol = plan.get("symbol") or symbol_from_inst_id(config, plan_inst_id)
        signal_side = str(plan.get("signal_side") or "").upper()
        signal_price = as_float(plan.get("signal_price"))
        base = {
            "received_at": received_at,
            "received_colombia": colombia_time(received_at),
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
            "planned_notional": nested_get(plan, "execution_intent", "planned_notional_usd"),
            "risk_weight": nested_get(plan, "execution_intent", "risk_weight"),
            "ct_val": nested_get(plan, "instrument", "ctVal"),
        }
        if not orders:
            out.append({**base, "okx_action": "-", "order_status": base["status"], "position_after": signed_position_side(nested_get(fill, "position_after_order", "pos"))})
            continue
        for order in orders:
            planned = order.get("planned") or {}
            state = order.get("order_state") or {}
            avg_px = as_float(first_present(state.get("avgPx"), state.get("fillPx")))
            slippage = None
            if avg_px is not None and signal_price:
                slippage = (avg_px / signal_price - 1.0) * 100.0
            pos_after = fill.get("position_after_order") or {}
            out.append({
                **base,
                "okx_action": order.get("action"),
                "okx_side": state.get("side") or planned.get("side"),
                "contracts": as_float(first_present(state.get("accFillSz"), planned.get("sz"))),
                "notional": okx_order_notional(order, plan),
                "okx_price": avg_px,
                "slippage_pct": slippage,
                "fee": as_float(state.get("fee")),
                "realized_pnl": as_float(state.get("pnl")),
                "position_after": signed_position_side(pos_after.get("pos")),
                "leverage": state.get("lever") or planned.get("lever") or nested_get(plan, "expected_settings", "leverage"),
                "margin_mode": state.get("tdMode") or planned.get("tdMode"),
                "order_id": order.get("ordId"),
                "client_order_id": order.get("clOrdId"),
                "order_status": state.get("state") or order.get("status"),
                "elapsed_ms": order.get("elapsed_ms") or base["elapsed_ms"],
            })
    history = okx_order_history_snapshot(config)
    if history.get("ok"):
        out = enrich_close_rows_with_okx_history(out, history.get("rows") or [])
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
    label = str(okx_status_label(row)).lower()
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
    if is_live:
        return "LIVE"
    return str(okx_status_label(row) or "-").upper()


def okx_display_status_kind(row, is_live=False):
    if is_live:
        return "good"
    return okx_status_kind(row)


def okx_row_details(row, is_live=False):
    payload = {
        "received_at": row.get("received_at"),
        "tv_time": row.get("tv_time"),
        "mode": row.get("mode"),
        "policy": row.get("policy"),
        "action": row.get("okx_action"),
        "side": row.get("okx_side"),
        "status": okx_display_status(row, is_live),
        "reduce_only": okx_reduce_only_label(row),
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
        + (f'<p>{esc(note)}</p>' if note else "")
        + '<pre>'
        + esc(json.dumps(payload, indent=2, ensure_ascii=False))
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


def executor_health_summary(okx_live, now=None):
    """Collapse the executor read into an explicit health verdict (D3).

    ``degraded`` drives the visible banner; we never let an errored or stale
    executor read render as a healthy flat view.
    """
    now = now if now is not None else time.time()
    okx_live = okx_live or {}
    healthy = bool(okx_live.get("ok"))
    error = None if healthy else str(okx_live.get("error") or "executor_unavailable")
    age = None
    stale = False
    dt = parse_dt(okx_live.get("generated_at"))
    if dt is not None:
        age = max(0.0, now - dt.timestamp())
        stale = age > REFRESH_INTERVAL_SECONDS
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


def ledger_health_summary():
    """Aggregate corrupt/skipped-line counts surfaced by the bounded reader (D1/D2)."""
    ledgers = {}
    total_skipped = 0
    truncated_tails = 0
    for path, st in LEDGER_READ_STATS.items():
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
    now = now if now is not None else time.time()
    candidates = []
    for row in model.get("strategy_alerts") or []:
        for key in ("received_at", "tv_time"):
            dt = parse_dt(row.get(key))
            if dt is not None:
                candidates.append(dt.timestamp())
    dt = parse_dt((model.get("okx_live") or {}).get("generated_at"))
    if dt is not None:
        candidates.append(dt.timestamp())
    data_at = max(candidates) if candidates else None
    age = (now - data_at) if data_at is not None else None
    stale = (age is None) or (age > REFRESH_INTERVAL_SECONDS)
    return {
        "generated_at": model.get("generated_at"),
        "data_at": datetime.fromtimestamp(data_at, timezone.utc).isoformat() if data_at is not None else None,
        "age_seconds": age,
        "stale": stale,
        "no_data": data_at is None,
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
    }


def dashboard_model():
    now = time.time()
    cached = _MODEL_CACHE.get("model")
    if cached is not None and now < float(_MODEL_CACHE.get("expires_at") or 0):
        return cached
    # Clear per-read ledger stats so ledger_health reflects THIS build only (D1/D2).
    LEDGER_READ_STATS.clear()
    cfg = shadow_config()
    loaded = load_events()
    okx_live = okx_live_snapshot(cfg)
    strategies = load_strategy_files()
    model = {
        "config": cfg,
        "loaded": loaded,
        "okx_live": okx_live,
        "okx_executions": okx_execution_records(cfg),
        "strategies": strategies,
        "active_strategies": active_strategies(strategies),
        "strategy_alerts": strategy_alert_rows(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    model["executor"] = executor_health_summary(okx_live, now)
    model["ledger_health"] = ledger_health_summary()
    model["freshness"] = freshness_summary(model, now)
    _MODEL_CACHE["model"] = model
    _MODEL_CACHE["expires_at"] = now + MODEL_CACHE_TTL_SECONDS
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


def api_payload():
    """The structured model the ``/api`` route serializes.

    Legacy `policies` field removed (D4): it was always ``{}`` because no policies
    are configured. Executor/ledger/freshness health is surfaced explicitly so a
    consumer can never mistake an executor failure for a healthy flat view (D3).
    """
    model = dashboard_model()
    loaded = model["loaded"]
    open_orders, oo_stats = order_journal_open_orders()
    recon, rc_stats = reconcile_alert_records()
    ops, op_stats = operator_alert_records()

    # Merge strategy-mode overrides (control-state.json) into the payload. An override
    # takes precedence over the strategy file; otherwise effective_mode is derived from
    # the file: submit_orders explicitly False -> "pause", else execution_mode (demo/live).
    _ctrl = _load_control_state()
    _overrides = _ctrl.get("strategy_overrides") if isinstance(_ctrl.get("strategy_overrides"), dict) else {}
    annotated_strategies = []
    for s in model.get("active_strategies") or []:
        s = dict(s)
        s["effective_mode"] = _effective_strategy_mode(s, _overrides)
        annotated_strategies.append(s)

    return {
        "generated_at": model["generated_at"],
        "source_counts": {k: loaded[k] for k in ("historical_count", "backfill_count", "live_count")},
        "backfill": loaded["backfill"],
        "strategies": annotated_strategies,
        "strategy_overrides": _overrides,
        "strategy_alerts": model.get("strategy_alerts") or [],
        "okx_live": model.get("okx_live"),
        "okx_executions": model.get("okx_executions") or [],
        "executor": model.get("executor") or {},
        "hermes": {
            "enabled": _hermes_enabled,
            "ok": _hermes_enabled,
        },
        "ledger_health": model.get("ledger_health") or {},
        "freshness": model.get("freshness") or {},
        # Read-only order/reconcile/operator observability (each with its read stats).
        "open_orders": {"rows": open_orders, "stats": oo_stats},
        "reconcile_alerts": {"rows": recon, "stats": rc_stats},
        "operator_alerts": {"rows": ops, "stats": op_stats},
    }


def health_payload():
    cfg = shadow_config()
    policies = list(((cfg.get("policies") or {}).get("enabled")) or [])

    # Arming is driven by the 2-control model (execution_mode per strategy +
    # the global HERMX_LIVE_TRADING kill switch), not the dead config-flag chain.
    # ``live_trading_enabled()`` is the single source of truth for the global gate;
    # kill_switch_engaged means live trading is DISABLED.
    live_enabled, _live_raw = live_trading_enabled()
    kill_switch_engaged = not live_enabled

    strategies = active_strategies()
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


def metric_cards(items):
    return '<div class="metrics">' + ''.join(f'<div class="metric"><span>{esc(k)}</span><b>{esc(v)}</b></div>' for k, v in items.items()) + '</div>'


def reason_details(row):
    visible_reasons = [r for r in (row.get("reasons") or []) if "pulse" not in str(r).lower()]
    reasons = ''.join(f'<li>{esc(r)}</li>' for r in visible_reasons)
    ctx = row.get("ctx30") or {}
    ctx_line = f"Regime {ctx.get('regime','-')} / Phase {ctx.get('phase','-')} / Alignment {num(ctx.get('no_pulse_score'),2)} / RSI {num(ctx.get('jrsx'),2)} / Acc {num(ctx.get('pp_acc'),2)} / Vel {num(ctx.get('pp_vel'),2)}"
    raw = f"Decision {row.get('decision','-')} / Policy {row.get('policy_action','-')}"
    return f'<details class="why"><summary>{badge(trade_effect(row), action_kind(trade_effect(row)))}</summary><p>{esc(raw)}</p><p>{esc(ctx_line)}</p><ul>{reasons}</ul></details>'


def asset_table(rows):
    if not rows:
        return '<table><tbody><tr><td>No signals yet.</td></tr></tbody></table>'
    body = []
    for row in reversed(rows):
        pnl_kind = "good" if (row.get("closed_pnl") or 0) > 0 else ("bad" if row.get("closed_pnl") is not None else "neutral")
        body.append(f"""
        <tr>
          <td>{esc(row.get('time_colombia'))}</td>
          <td>{esc(row.get('timeframe'))}</td>
          <td>{badge(row.get('signal'), side_kind(row.get('signal')))}</td>
          <td>{num(row.get('price'), 4)}</td>
          <td>{esc(row.get('position_action'))}</td>
          <td>{badge(trade_effect(row), action_kind(trade_effect(row)))}</td>
          <td>{reason_details(row)}</td>
          <td>{num(row.get('weight'), 2)}x</td>
          <td>{money(row.get('fees'), 2)}</td>
          <td>{badge(money(row.get('closed_pnl'), 2) if row.get('closed_pnl') is not None else "-", pnl_kind)}</td>
          <td>{money(row.get('equity_after'), 2)}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Fecha</th><th>TF</th><th>Signal</th><th>Precio</th><th>Position action</th><th>Trade effect</th><th>Why</th><th>Weight</th><th>Fees</th><th>PnL</th><th>Budget after</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def first_okx_trade_map(rows):
    first = {}
    for row in rows:
        sym = row.get("symbol")
        if sym in first:
            continue
        if not sym or row.get("okx_price") is None:
            continue
        first[sym] = row.get("received_colombia") or colombia_time(row.get("received_at"))
    return first


def okx_live_entry_state(okx_live, rows):
    live = {}
    positions = (okx_live or {}).get("positions") or {}
    for sym, pos in positions.items():
        side = str(pos.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            continue
        wanted_action = "OPEN_" + side
        for row in reversed(rows):
            if row.get("symbol") != sym:
                continue
            if str(row.get("okx_action") or "").upper() != wanted_action:
                continue
            if str(row.get("position_after") or "").upper() != side:
                continue
            key = row.get("order_id") or row.get("client_order_id") or f"{row.get('received_at')}:{sym}:{wanted_action}"
            live[key] = {"symbol": sym, "side": side, "open_pnl": as_float(pos.get("upl")) or 0.0}
            break
    return live


def okx_live_card(config, okx_live, sym, first_trade_time=None):
    asset_cfg = (config.get("assets") or {}).get(sym) or {}
    meta = ASSET_META.get(sym, {"name": sym, "logo": ""})
    pos = ((okx_live or {}).get("positions") or {}).get(sym) or {}
    side = str(pos.get("side") or "UNKNOWN").upper()
    side_class = side.lower() if side in {"LONG", "SHORT"} else "flat"
    live_price = first_present(pos.get("mark_px"), pos.get("last"))
    budget = as_float(asset_cfg.get("budget_usd")) or 0.0
    realized = as_float(pos.get("realized_pnl")) or 0.0
    upl = as_float(pos.get("upl")) or 0.0
    total_pnl = realized + upl
    budget_now = budget + total_pnl
    status_badge = badge("LIVE", "good") if side in {"LONG", "SHORT"} else badge("FLAT", "muted")
    start_value = money(budget, 0)
    if first_trade_time:
        start_value = f"{start_value} since {first_trade_time}"
    return f"""
    <section class="asset-card okx-card" data-symbol="{esc(sym)}">
      <div class="asset-head">
        <div class="asset-title">
          <img class="asset-logo" src="{esc(meta.get('logo'))}" alt="{esc(meta.get('name'))} logo" loading="lazy">
          <div>
            <h3>{esc(sym)}</h3>
            <p>{esc(meta.get('name'))}</p>
          </div>
        </div>
        <div>{badge("OKX demo", "good" if okx_live.get("ok") else "warn")} {status_badge}</div>
      </div>
      <div class="card-status">
        <div>
          <span class="label">Actual OKX position</span>
          <div class="position-line"><span class="position-pill {esc(side_class)}">{esc(side)}</span></div>
        </div>
        <div class="live-price">
          <span class="label">OKX live price</span>
          <b>{num(live_price, 4)}</b>
        </div>
      </div>
      {metric_cards({
        "Budget start": start_value,
        "Budget now": money(budget_now, 2),
        "PnL now": money(total_pnl, 2),
      })}
    </section>
    """


def okx_execution_table(rows, live_info=None):
    if not rows:
        return '<table><tbody><tr><td>No OKX demo executions logged yet.</td></tr></tbody></table>'
    live_info = live_info or {}
    body = []
    for row in reversed(rows[-80:]):
        row_key = row.get("order_id") or row.get("client_order_id") or f"{row.get('received_at')}:{row.get('symbol')}:{row.get('okx_action')}"
        is_live = row_key in live_info
        pnl = live_info[row_key]["open_pnl"] if is_live else row.get("realized_pnl")
        show_pnl = is_live or str(row.get("okx_action") or "").upper().startswith("CLOSE")
        pnl_kind = "good" if (pnl or 0) > 0 else ("bad" if pnl is not None and pnl < 0 else "neutral")
        tr_class = ' class="live-row"' if is_live else ""
        body.append(f"""
        <tr{tr_class}>
          <td>{esc(row.get('received_colombia'))}</td>
          <td><b>{esc(row.get('symbol'))}</b></td>
          <td>{badge(row.get('signal'), side_kind(row.get('signal')))}</td>
          <td>{badge(okx_leg_label(row), okx_leg_kind(row))}</td>
          <td>{badge(okx_display_status(row, is_live), okx_display_status_kind(row, is_live))}</td>
          <td>{num(row.get('alert_price'), 4)}</td>
          <td>{num(row.get('okx_price'), 4)}</td>
          <td>{pct(row.get('slippage_pct'))}</td>
          <td>{num(row.get('contracts'), 4)}</td>
          <td>{money(row.get('notional'), 0)}</td>
          <td>{badge(okx_reduce_only_label(row), 'muted' if okx_reduce_only_label(row) == 'Yes' else 'neutral')}</td>
          <td>{money(row.get('fee'), 4)}</td>
          <td>{badge(money(pnl, 2) if show_pnl and pnl is not None else "-", pnl_kind)}</td>
          <td>{esc(row.get('margin_mode') or '-')} / {esc(row.get('leverage') or '-')}x</td>
          <td>{okx_row_details(row, is_live)}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Fecha</th><th>Asset</th><th>Signal</th><th>Leg</th><th>Status</th><th>Alert</th><th>Fill</th><th>Slip</th><th>Size</th><th>Value</th><th>RO</th><th>Fee</th><th>PnL</th><th>Mode</th><th>Details</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def trade_log(rows, live_row_ids=None):
    if not rows:
        return '<table><tbody><tr><td>No signals yet.</td></tr></tbody></table>'
    live_row_ids = live_row_ids or set()
    body = []
    for row in reversed(rows):
        pnl_kind = "good" if (row.get("closed_pnl") or 0) > 0 else ("bad" if row.get("closed_pnl") is not None else "neutral")
        effect = trade_effect(row)
        is_live = row.get("row_id") in live_row_ids
        live_cell = '<span class="live-dot"></span> LIVE' if is_live else ""
        tr_class = ' class="live-row"' if is_live else ""
        body.append(f"""
        <tr id="{esc(row.get('row_id'))}"{tr_class}>
          <td>{esc(row.get('time_colombia'))}</td>
          <td>{live_cell}</td>
          <td><b>{esc(row.get('symbol'))}</b></td>
          <td>{esc(row.get('timeframe'))}</td>
          <td>{badge(row.get('signal'), side_kind(row.get('signal')))}</td>
          <td>{num(row.get('price'), 4)}</td>
          <td>{esc(row.get('position_action'))}</td>
          <td>{badge(effect, action_kind(effect))}</td>
          <td>{reason_details(row)}</td>
          <td>{num(row.get('weight'), 2)}x</td>
          <td>{money(row.get('fees'), 2)}</td>
          <td>{badge(money(row.get('closed_pnl'), 2) if row.get('closed_pnl') is not None else "-", pnl_kind)}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Fecha</th><th>Live</th><th>Asset</th><th>TF</th><th>Signal</th><th>Mark price</th><th>Position action</th><th>Trade effect</th><th>Why</th><th>Weight</th><th>Fees</th><th>PnL</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def okx_demo_live_section(config, okx_live, okx_executions):
    error = ""
    if not okx_live.get("ok"):
        error = f'<div class="notice warn"><b>OKX read unavailable:</b> {esc(okx_live.get("error") or "unknown error")}</div>'
    live_info = okx_live_entry_state(okx_live, okx_executions)
    first_trades = first_okx_trade_map(okx_executions)
    return f"""
    <section class="subsection okx-section">
      <div class="log-head">
        <div>
          <h3>OKX Demo Live</h3>
          <p>Actual sandbox positions and order fills. This is separate from the historical paper replay.</p>
        </div>
      </div>
      {error}
      <div class="asset-grid">{''.join(okx_live_card(config, okx_live, sym, first_trades.get(sym)) for sym in SYMBOLS)}</div>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Execution Ledger</h3>
          <p>Only real demo submissions appear here. Close and open orders can be separate rows for the same alert.</p>
        </div>
        <div class="table-wrap unified-log">{okx_execution_table(okx_executions, live_info)}</div>
      </section>
    </section>
    """


def metric_cards_colored(items):
    """Like metric_cards but each item is (value_str, color_kind_or_None)."""
    cells = []
    for label, (val, kind) in items.items():
        color_style = ""
        if kind == "good":
            color_style = " style=\"color:var(--positive)\""
        elif kind == "bad":
            color_style = " style=\"color:var(--negative)\""
        cells.append(f'<div class="metric"><span>{esc(label)}</span><b{color_style}>{esc(val)}</b></div>')
    return f'<div class="metrics">{"".join(cells)}</div>'


def strategy_card(strategy, okx_live, alerts):
    sym = strategy.get("asset")
    meta = ASSET_META.get(sym, {"name": sym, "logo": ""})
    live = (okx_live.get("positions") or {}).get(sym) or {}
    rows = [row for row in alerts if row.get("strategy_id") == strategy.get("strategy_id")]
    # effective_mode (pause/demo/live) is annotated upstream in render(); fall back to
    # the file's execution_mode if a caller passes an un-annotated strategy.
    mode = (strategy.get("effective_mode") or strategy.get("execution_mode") or "demo").lower()
    _mode_labels = {"pause": "Pause", "demo": "Demo", "live": "Live"}
    mode_label = _mode_labels.get(mode, mode.title())
    mode_kind = "good" if mode == "live" else "muted" if mode == "pause" else "neutral"
    position = live.get("side") or "FLAT"
    is_live = position != "FLAT"
    budget = as_float((strategy.get("capital") or {}).get("budget_usd") or strategy.get("budget_usd")) or 0.0
    upl = as_float(live.get("upl")) or 0.0
    realized = as_float(live.get("realized_pnl")) or 0.0
    pnl_now = realized + upl
    budget_now = budget + pnl_now
    live_badge = badge("LIVE", "good") if is_live else badge("FLAT", "muted")
    return f"""
    <section class="asset-card clean-card strategy-card">
      <div class="asset-head">
        <div class="asset-title">
          <img class="asset-logo" src="{esc(meta.get('logo'))}" alt="{esc(meta.get('name'))} logo" loading="lazy">
          <div>
            <h3>{esc(sym)} <span class="tf-chip">{esc(strategy.get('timeframe'))}</span></h3>
            <p>{esc(strategy.get('name') or meta.get('name'))}</p>
          </div>
        </div>
        <div>{badge(mode_label, mode_kind)} {live_badge}</div>
      </div>
      <div class="card-status">
        <div>
          <span class="metric-label">Strategy config</span>
          <div class="position-line">
            {badge(strategy.get("indicator") or "-", "neutral")}
            {badge(str(strategy.get("leverage") or "-") + "x", "neutral")}
            {badge(strategy.get("margin_mode") or "-", "neutral")}
            {badge((strategy.get("instrument") or {}).get("type") or "-", "neutral")}
            {badge((strategy.get("instrument") or {}).get("exchange") or "-", "good")}
          </div>
        </div>
        <div class="live-price">
          <span class="metric-label">Position</span>
          <b>{badge(position, "muted" if position == "FLAT" else side_kind(position))}</b>
          <span class="metric-sub">entry {num(live.get("avg_px"), 4)}</span>
        </div>
      </div>
      {metric_cards_colored({
        "Budget": (money(budget, 0), None),
        "Equity now": (money(budget_now, 2), "good" if pnl_now > 0 else ("bad" if pnl_now < 0 else None)),
        "UPnL": (money(pnl_now, 2), "good" if pnl_now > 0 else ("bad" if pnl_now < 0 else None)),
        "Mark price": (num(live.get("last"), 4), None),
        "Alerts": (str(len(rows)), None),
      })}
    </section>
    """


def strategy_alert_table(rows):
    if not rows:
        return '<table><tbody><tr><td>No Duo Base Dev strategy alerts yet.</td></tr></tbody></table>'
    body = []
    for row in reversed(rows[-120:]):
        decision = "DUPLICATE" if row.get("duplicate") else (row.get("decision") or "ACCEPTED")
        body.append(f"""
        <tr>
          <td>{esc(row.get('tv_time_colombia') or row.get('received_colombia'))}</td>
          <td>{esc(row.get('received_colombia'))}</td>
          <td>{esc(row.get('strategy_name'))}</td>
          <td><b>{esc(row.get('asset'))}</b></td>
          <td>{esc(row.get('timeframe'))}</td>
          <td>{badge(row.get('side'), side_kind(row.get('side')))}</td>
          <td>{num(row.get('price'), 4)}</td>
          <td>{badge(decision, "muted" if row.get("duplicate") else "good")}</td>
          <td>{fmt_seconds(row.get('latency'))}</td>
          <td>{esc(row.get('okx_mode') or '-')}</td>
          <td>{esc(row.get('block_reason') or '-')}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>TV time</th><th>Received</th><th>Strategy</th><th>Asset</th><th>TF</th><th>Signal</th><th>TV price</th><th>Trial decision</th><th>Latency</th><th>OKX mode</th><th>Safety reason</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def strategy_execution_rows(strategy, okx_executions):
    strategy_id = strategy.get("strategy_id")
    expected_policy = f"strategy_file:{strategy_id}"
    return [
        row for row in (okx_executions or [])
        if row.get("policy") == expected_policy or str(row.get("policy") or "").endswith(str(strategy_id or ""))
    ]


# ---------------------------------------------------------------------------
# Order / reconcile / operator observability panels (read-only). These fold the
# bounded tail of the receiver's order-journal + alert ledgers via the same bounded
# reader the rest of the dashboard uses (read_jsonl_stats), so a huge ledger can never
# OOM the dashboard and corrupt/truncated lines are surfaced, not hidden. The dashboard
# stays a pure consumer -- no submit/execute/cancel controls.
# ---------------------------------------------------------------------------

def _read_order_checkpoint_index():
    """Read the receiver's verified order-journal checkpoint and return its
    ``index_records`` (latest record per cl_ord_id for every sealed/folded order).

    The checkpoint is a single JSON object (NOT JSONL), so it is read directly rather
    than via the bounded JSONL reader. Missing/unreadable/malformed checkpoint => empty
    list, so the panel degrades to a live-segment-only read exactly as before rotation."""
    path = ORDER_JOURNAL_CHECKPOINT_FILE
    if not path.exists():
        return []
    try:
        ckpt = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(ckpt, dict):
        return []
    records = ckpt.get("index_records")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def order_journal_open_orders(limit=300):
    """Latest record per cl_ord_id across the verified checkpoint AND the live-segment
    tail, filtered to non-terminal (PLANNED/SUBMITTED/UNKNOWN) open orders. Merging the
    checkpoint is what keeps the panel correct after the journal rotates -- an order
    whose latest record sealed into a segment lives only in the checkpoint's index.
    Returns ``(open_orders, stats)``; stats carries the live-segment read/skipped/
    truncated counts plus ``checkpoint_records`` for the count merged from the checkpoint."""
    rows, stats = read_jsonl_stats(ORDER_JOURNAL_FILE, limit)
    checkpoint_records = _read_order_checkpoint_index()
    stats = dict(stats)
    stats["checkpoint_records"] = len(checkpoint_records)
    latest = {}
    # Seed with the checkpoint first; live-tail records (newer seq) then win the merge.
    for rec in [*checkpoint_records, *rows]:
        if not isinstance(rec, dict):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int):
            continue
        cl = rec.get("cl_ord_id")
        cur = latest.get(cl)
        if cur is None or seq > int(cur.get("seq") or -1):
            latest[cl] = rec
    open_orders = []
    for cl, rec in latest.items():
        state = str(rec.get("state") or "").upper()
        if state in ORDER_TERMINAL_STATES_DASH:
            continue
        intent = rec.get("intent") or {}
        open_orders.append({
            "cl_ord_id": cl,
            "state": state,
            "symbol": intent.get("symbol"),
            "inst_id": intent.get("inst_id"),
            "ts": rec.get("ts"),
            "prev_state": rec.get("prev_state"),
        })
    open_orders.sort(
        key=lambda r: parse_dt(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return open_orders, stats


def reconcile_alert_records(limit=60):
    rows, stats = _alerts_rows("reconcile", limit)
    return list(reversed(rows)), stats


def operator_alert_records(limit=60):
    rows, stats = _alerts_rows("operator", limit)
    return list(reversed(rows)), stats


def _ledger_stat_note(stats):
    bits = [f"{int(stats.get('read') or 0)} rows"]
    if stats.get("skipped"):
        bits.append(f"{int(stats['skipped'])} corrupt")
    if stats.get("truncated_tail"):
        bits.append("truncated tail")
    if stats.get("more"):
        bits.append("older rows beyond window")
    kind = "warn" if (stats.get("skipped") or stats.get("truncated_tail")) else "neutral"
    return badge(" · ".join(bits), kind)


def _alert_detail_str(detail):
    if not isinstance(detail, dict):
        return esc(detail)
    return esc(", ".join(f"{k}={detail[k]}" for k in list(detail)[:8]))


def _state_kind(state):
    s = str(state or "").upper()
    if s == "SUBMITTED":
        return "good"
    if s == "UNKNOWN":
        return "warn"
    if s == "PLANNED":
        return "neutral"
    return "neutral"


def open_orders_table(open_orders):
    if not open_orders:
        return '<table><tbody><tr><td>No open orders (all PLANNED/SUBMITTED/UNKNOWN orders resolved).</td></tr></tbody></table>'
    body = []
    for row in open_orders:
        body.append(f"""
        <tr>
          <td>{esc(display_time(row.get('ts')))}</td>
          <td><code>{esc(row.get('cl_ord_id'))}</code></td>
          <td><b>{esc(row.get('symbol') or '-')}</b></td>
          <td>{esc(row.get('inst_id') or '-')}</td>
          <td>{badge(row.get('state'), _state_kind(row.get('state')))}</td>
          <td>{esc(row.get('prev_state') or '-')}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Updated</th><th>clOrdId</th><th>Symbol</th><th>Instrument</th><th>State</th><th>Prev</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def reconcile_alert_table(rows):
    if not rows:
        return '<table><tbody><tr><td>No reconcile alerts.</td></tr></tbody></table>'
    body = []
    for row in rows:
        detail = row.get("detail") or {}
        body.append(f"""
        <tr>
          <td>{esc(display_time(row.get('ts')))}</td>
          <td>{badge(row.get('alert'), 'warn')}</td>
          <td>{esc(detail.get('stage') or '-')}</td>
          <td><code>{esc(detail.get('cl_ord_id') or '-')}</code></td>
          <td>{esc(detail.get('symbol') or '-')}</td>
          <td>{esc(detail.get('reason') or detail.get('reconciled_state') or '-')}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Time</th><th>Alert</th><th>Stage</th><th>clOrdId</th><th>Symbol</th><th>Reason</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def operator_alert_table(rows):
    if not rows:
        return '<table><tbody><tr><td>No operator alerts.</td></tr></tbody></table>'
    body = []
    for row in rows:
        sev = str(row.get("severity") or "warning").lower()
        sev_kind = "bad" if sev == "error" else "warn"
        body.append(f"""
        <tr>
          <td>{esc(display_time(row.get('ts')))}</td>
          <td>{badge(sev, sev_kind)}</td>
          <td>{badge(row.get('alert'), 'neutral')}</td>
          <td>{_alert_detail_str(row.get('detail'))}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Time</th><th>Severity</th><th>Alert</th><th>Detail</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
    """


def order_state_section():
    """The read-only Order / Reconcile / Operator observability section."""
    open_orders, oo_stats = order_journal_open_orders()
    recon, rc_stats = reconcile_alert_records()
    ops, op_stats = operator_alert_records()
    return f"""
    <section class="subsection">
      <div class="log-head">
        <div>
          <h3>Order &amp; Reconcile State</h3>
          <p>Read-only view of the submission state machine and reconciliation alerts. The dashboard never submits or cancels.</p>
        </div>
      </div>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Open Orders <span class="sub">{_ledger_stat_note(oo_stats)}</span></h3>
          <p>Non-terminal orders (PLANNED / SUBMITTED / UNKNOWN) from order-journal.jsonl, latest state per clOrdId.</p>
        </div>
        <div class="table-wrap unified-log">{open_orders_table(open_orders)}</div>
      </section>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Reconcile Alerts <span class="sub">{_ledger_stat_note(rc_stats)}</span></h3>
          <p>Latest entries from alerts.jsonl (kind=reconcile: mismatches, resolver timeouts).</p>
        </div>
        <div class="table-wrap unified-log">{reconcile_alert_table(recon)}</div>
      </section>
      <section class="trade-log-card nested">
        <div class="log-head">
          <h3>Operator Alerts <span class="sub">{_ledger_stat_note(op_stats)}</span></h3>
          <p>Latest entries from alerts.jsonl (kind=operator: auth, queue, resolver, never_submitted).</p>
        </div>
        <div class="table-wrap unified-log">{operator_alert_table(ops)}</div>
      </section>
    </section>
    """


def strategy_trial_tab(strategies, alerts, okx_live, okx_executions):
    cards = ''.join(strategy_card(strategy, okx_live, alerts) for strategy in strategies)
    strategy_rows = []
    for strategy in strategies:
        strategy_rows.extend(strategy_execution_rows(strategy, okx_executions))
    strategy_rows.sort(key=lambda row: parse_dt(row.get("received_at")) or datetime.min.replace(tzinfo=timezone.utc))
    live_info = okx_live_entry_state(okx_live, strategy_rows)
    return f"""
    <section class="tab-panel" id="{TRIAL_TAB_ID}">
      <div class="section-head">
        <div>
          <h2>Duo Base Dev Trial</h2>
          <p>Strategy-file-driven trial. Alerts must include strategy_id. This view is demo-only until explicit promotion.</p>
        </div>
        <div>{badge(str(len(strategies)) + " strategy files", "good")} {badge("founder package pending", "warn")}</div>
      </div>
      <div class="asset-grid">{cards}</div>
      <section class="subsection okx-section">
        <div class="log-head">
          <h3>Strategy Demo Ledger</h3>
          <p>Actual sandbox submissions for the strategy-file trial. Open rows can show live PnL while the position remains active.</p>
        </div>
        <div class="table-wrap unified-log">{okx_execution_table(strategy_rows, live_info)}</div>
      </section>
      <section class="subsection">
        <div class="log-head">
          <h3>Strategy Alert Log</h3>
          <p>Only valid Duo Base Dev alerts appear here. Invalid strategy alerts are quarantined and never routed to OKX.</p>
        </div>
        <div class="table-wrap unified-log">{strategy_alert_table(alerts)}</div>
      </section>
      {order_state_section()}
    </section>
    """


def banner(text, kind="warn"):
    return f'<div class="banner {kind}">{esc(text)}</div>'


def status_banners(model):
    """Explicit banners for executor failure / stale data / corrupt ledgers."""
    out = []
    execu = model.get("executor") or {}
    fresh = model.get("freshness") or {}
    ledger = model.get("ledger_health") or {}
    if execu.get("error"):
        out.append(banner(f"EXECUTOR ERROR — exchange data unavailable / stale ({execu.get('error')})", "bad"))
    elif execu.get("stale"):
        out.append(banner(f"EXECUTOR DATA STALE — last exchange read {human_age(execu.get('age_seconds'))} ago", "warn"))
    if ledger.get("total_skipped"):
        out.append(banner(f"{ledger['total_skipped']} ledger lines skipped (corrupt) — see /dashboard/api ledger_health", "warn"))
    if fresh.get("stale"):
        if fresh.get("no_data"):
            out.append(banner("No recent data — dashboard has not received any alerts yet", "warn"))
        else:
            out.append(banner(f"DATA MAY BE STALE — newest data is {human_age(fresh.get('age_seconds'))} old (refresh {fresh.get('refresh_interval_seconds')}s)", "warn"))
    return "".join(out)


def strategy_indicator_label(strategies):
    indicators = {(s.get("indicator") or "").strip() for s in (strategies or []) if s.get("indicator")}
    if len(indicators) == 1:
        return indicators.pop()
    return f"{len(indicators)} indicators" if indicators else "—"


def summary_cards(model):
    okx_live = model.get("okx_live") or {}
    strategies = model.get("active_strategies") or []
    executor = model.get("executor") or {}
    fresh = model.get("freshness") or {}

    # Card 1: System status
    live_enabled, _ = live_trading_enabled()
    demo_count = sum(1 for s in strategies if (s.get("execution_mode") or "demo") != "live")
    live_count_s = sum(1 for s in strategies if (s.get("execution_mode") or "demo") == "live")
    if live_enabled and live_count_s > 0:
        sys_label, sys_kind = "ARMED", "good"
    elif strategies:
        sys_label, sys_kind = "DEMO", "warn"
    else:
        sys_label, sys_kind = "DISARMED", "bad"

    # Card 2: Active strategies
    strat_label = str(len(strategies))
    strat_sub = f"{demo_count} demo / {live_count_s} live"
    strat_kind = "good" if strategies else "muted"

    # Card 3: Open positions
    positions = (okx_live.get("positions") or {})
    open_pos = {sym: p for sym, p in positions.items() if (p.get("side") or "FLAT") != "FLAT"}
    longs = sum(1 for p in open_pos.values() if p.get("side") == "LONG")
    shorts = sum(1 for p in open_pos.values() if p.get("side") == "SHORT")
    if open_pos:
        pos_label = f"{len(open_pos)} OPEN"
        pos_sub = f"{longs}L / {shorts}S"
        pos_kind = "good"
    else:
        pos_label = "ALL FLAT"
        pos_sub = "no open positions"
        pos_kind = "muted"

    # Card 4: Executor health
    exec_ok = executor.get("ok", False)
    exec_err = executor.get("error")
    stale = fresh.get("stale", False)
    if stale:
        exec_label, exec_kind = "STALE", "bad"
    elif not exec_ok:
        exec_label, exec_kind = "ERROR", "bad"
    else:
        exec_label, exec_kind = "OK", "good"
    exec_sub = esc(str(exec_err)[:40]) if exec_err else "executor healthy"

    def card(icon_label, value, sub, kind):
        bar_color = "var(--positive)" if kind == "good" else ("var(--negative)" if kind == "bad" else ("var(--warning)" if kind == "warn" else "var(--text-muted)"))
        return f"""<div class="metric-card" style="border-top:3px solid {bar_color}">
  <span class="metric-label">{esc(icon_label)}</span>
  <b class="metric-value" style="color:{bar_color}">{esc(value)}</b>
  <span class="metric-sub">{sub}</span>
</div>"""

    return f"""<div class="summary-metrics">
  {card("SYSTEM STATUS", sys_label, f"{len(strategies)} strategies active", sys_kind)}
  {card("ACTIVE STRATEGIES", strat_label, strat_sub, strat_kind)}
  {card("OPEN POSITIONS", pos_label, pos_sub, pos_kind)}
  {card("EXECUTION ENGINE", exec_label, exec_sub, exec_kind)}
</div>"""


def render():
    model = dashboard_model()
    cfg = model["config"]
    okx_live = model.get("okx_live") or {}
    okx_executions = model.get("okx_executions") or []
    # Annotate each strategy with its effective UI mode (pause/demo/live) so the card
    # badge reflects overrides + the file's submit_orders, mirroring api_payload.
    _ctrl = _load_control_state()
    _overrides = _ctrl.get("strategy_overrides") if isinstance(_ctrl.get("strategy_overrides"), dict) else {}
    strategies = []
    for _s in model.get("active_strategies") or []:
        _s = dict(_s)
        _s["effective_mode"] = _effective_strategy_mode(_s, _overrides)
        strategies.append(_s)
    strategy_alerts = model.get("strategy_alerts") or []
    source_line = (
        f"{len(strategies)} active strategies | "
        f"{len(strategy_alerts)} strategy alerts received"
    )
    warn = status_banners(model)
    execution_cfg = cfg.get("execution") or {}
    risk_cfg = cfg.get("risk") or {}
    okx_enabled = bool(execution_cfg.get("enabled"))
    okx_badge = badge("Execution enabled", "good") if okx_enabled else badge("Orders disabled", "warn")
    fresh = model.get("freshness") or {}
    updated_text = "Updated " + (display_time(model["generated_at"]) or model["generated_at"])
    if fresh.get("age_seconds") is not None:
        updated_text += f" · data age {human_age(fresh.get('age_seconds'))}"
    updated_badge = badge(updated_text, "warn" if fresh.get("stale") else "neutral")
    stale_badge = badge("STALE", "bad") if fresh.get("stale") else ""
    tabs = f'<button class="tab-btn" data-target="{TRIAL_TAB_ID}">Duo Base Dev Trial</button>'
    panels = strategy_trial_tab(strategies, strategy_alerts, okx_live, okx_executions)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kinetic Flow Execution Dashboard</title>
  <style>{CSS}</style>
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow">KINETIC FLOW</p>
        <h1>Execution Dashboard</h1>
        <p class="subtitle">{esc(source_line)}</p>
      </div>
      <div class="header-metrics">
        <span>{badge("Strategy files", "good")}</span>
        <span>{badge(strategy_indicator_label(strategies), "neutral")}</span>
        <span>{okx_badge}</span>
        <span>{updated_badge}</span>
        {("<span>" + stale_badge + "</span>") if stale_badge else ""}
      </div>
    </header>
    {warn}
    {summary_cards(model)}
    <nav class="tabs">{tabs}</nav>
    {panels}
  </main>
  <script>
    let buttons = [];
    let panels = [];
    let validTabs = new Set();
    let subButtons = [];
    let subPanels = [];
    function show(id) {{
      if (!validTabs.has(id)) id = '{TRIAL_TAB_ID}';
      panels.forEach(p => p.classList.toggle('active', p.id === id));
      buttons.forEach(b => b.classList.toggle('active', b.dataset.target === id));
      localStorage.setItem('clean_dashboard_tab', id);
      ensureSubtab(id);
    }}
    function showSubtab(parent, id) {{
      const parentPanels = subPanels.filter(p => p.dataset.parent === parent);
      const valid = new Set(parentPanels.map(p => p.id));
      if (!valid.has(id)) id = parent + '-paper';
      parentPanels.forEach(p => p.classList.toggle('active', p.id === id));
      subButtons
        .filter(b => b.dataset.parent === parent)
        .forEach(b => b.classList.toggle('active', b.dataset.target === id));
      localStorage.setItem('clean_dashboard_subtab_' + parent, id);
    }}
    function ensureSubtab(parent) {{
      const saved = localStorage.getItem('clean_dashboard_subtab_' + parent);
      showSubtab(parent, saved || parent + '-paper');
    }}
    function bindDashboard() {{
      buttons = [...document.querySelectorAll('.tab-btn')];
      panels = [...document.querySelectorAll('.tab-panel')];
      subButtons = [...document.querySelectorAll('.subtab-btn')];
      subPanels = [...document.querySelectorAll('.subtab-panel')];
      validTabs = new Set(buttons.map(b => b.dataset.target));
      const savedTab = localStorage.getItem('clean_dashboard_tab');
      const active = validTabs.has(savedTab) ? savedTab : '{TRIAL_TAB_ID}';
      buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.target)));
      subButtons.forEach(b => b.addEventListener('click', () => showSubtab(b.dataset.parent, b.dataset.target)));
      show(active);
    }}
    async function refreshSilently() {{
      const activeTab = localStorage.getItem('clean_dashboard_tab') || '{TRIAL_TAB_ID}';
      const scrollY = window.scrollY;
      try {{
        const response = await fetch(location.pathname + '?_=' + Date.now(), {{cache: 'no-store'}});
        if (!response.ok) return;
        const html = await response.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const nextMain = doc.querySelector('main');
        const currentMain = document.querySelector('main');
        if (!nextMain || !currentMain) return;
        currentMain.replaceWith(nextMain);
        bindDashboard();
        show(activeTab);
        window.scrollTo(0, scrollY);
      }} catch (err) {{
        console.debug('silent refresh failed', err);
      }}
    }}
    bindDashboard();
    setInterval(refreshSilently, 20000);
  </script>
</body>
</html>"""


CSS = """
:root {
  /* Kinetic palette */
  --bg-base:#0a0f14; --bg-panel:#0f1519; --bg-panel-raised:#151c22;
  --border-dim:#1e293b; --border-focus:#05AD98;
  --text-primary:#e2e8f0; --text-secondary:#94a3b8; --text-muted:#475569;
  --positive:#05AD98; --negative:#E85D6C; --warning:#F5A623; --info:#8B5CF6;
  /* aliases for legacy selectors */
  --bg:var(--bg-base); --panel:var(--bg-panel); --panel2:var(--bg-panel-raised);
  --line:var(--border-dim); --text:var(--text-primary); --muted:var(--text-secondary);
  --green:var(--positive); --red:var(--negative); --yellow:var(--warning);
  --blue:#78b7ff;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:Inter, Segoe UI, Arial, sans-serif; font-size:13px; }
main { width:min(1880px, calc(100vw - 32px)); margin:0 auto; padding:22px 0 40px; }
header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:14px; }
h1,h2,h3,p { margin:0; }
h1 { font-size:25px; letter-spacing:0; }
h2 { font-size:17px; }
h3 { font-size:15px; }
.eyebrow { color:var(--blue); font-size:11px; font-weight:800; letter-spacing:.12em; margin-bottom:5px; }
.subtitle, .sub, .section-head p { color:var(--muted); line-height:1.45; }
.header-metrics { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.notice { border:1px solid var(--line); background:var(--panel); padding:12px 14px; border-radius:8px; margin:12px 0; color:var(--muted); }
.notice.warn { border-color:rgba(242,201,76,.45); background:rgba(242,201,76,.08); color:#f4df91; }
.tabs { display:flex; gap:8px; margin:16px 0; }
.tab-btn { cursor:pointer; border:1px solid var(--line); background:var(--panel); color:var(--muted); padding:9px 13px; border-radius:7px; font-weight:800; }
.tab-btn.active { color:var(--text); border-color:#4c8ed9; background:#132235; }
.legacy-tab { opacity:.72; }
.tf-chip { color:#9fc7ef; font-size:13px; font-weight:850; margin-left:4px; }
.tab-panel { display:none; }
.tab-panel.active { display:block; }
.subtabs { display:flex; gap:7px; margin:10px 0 12px; padding:5px; width:max-content; max-width:100%; overflow:auto; border:1px solid var(--line); background:#0b1119; border-radius:8px; }
.subtab-btn { cursor:pointer; border:0; background:transparent; color:var(--muted); padding:8px 11px; border-radius:6px; font-weight:850; white-space:nowrap; }
.subtab-btn.active { color:var(--text); background:#1a2a3d; box-shadow:inset 0 0 0 1px #385678; }
.subtab-panel { display:none; }
.subtab-panel.active { display:block; }
.section-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin:10px 0 12px; }
.subsection { border:1px solid var(--line); background:rgba(16,23,32,.66); border-radius:8px; padding:12px; margin:12px 0; }
.subsection .metrics { margin-top:8px; }
.okx-section { border-color:rgba(64,217,123,.28); background:rgba(64,217,123,.04); }
.comparison-section { border-color:rgba(120,183,255,.28); background:rgba(120,183,255,.04); }
.nested { margin-top:12px; background:#0b1119; }
.metrics { display:grid; grid-template-columns:repeat(9,minmax(110px,1fr)); gap:8px; margin:10px 0 12px; }
.metric { border:1px solid var(--line); background:var(--panel2); border-radius:7px; padding:10px; min-height:58px; }
.metric span { display:block; color:var(--muted); font-size:11px; margin-bottom:7px; }
.metric b { font-size:15px; }
.asset-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; align-items:start; }
#duo_base_dev_trial > .asset-grid { grid-template-columns:repeat(4,minmax(0,1fr)); }
.asset-card { border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:12px; min-width:0; }
.strategy-card { padding:10px; }
.okx-card { background:#0c1513; border-color:rgba(64,217,123,.22); }
.asset-head { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; margin-bottom:10px; }
.asset-title { display:flex; align-items:center; gap:10px; min-width:0; }
.strategy-card .asset-head { gap:8px; margin-bottom:8px; }
.strategy-card .asset-title { gap:8px; }
.strategy-card h3 { font-size:14px; }
.strategy-card .asset-title p { display:none; }
.asset-title p { color:var(--muted); margin-top:3px; font-size:12px; }
.asset-logo { width:32px; height:32px; border-radius:50%; object-fit:contain; background:#0b1018; border:1px solid var(--line); padding:3px; flex:0 0 auto; }
.strategy-card .asset-logo { width:28px; height:28px; }
.card-status { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; padding:10px; border:1px solid var(--line); border-radius:8px; background:#0b1119; margin-bottom:10px; }
.strategy-card .card-status { gap:8px; padding:8px; margin-bottom:8px; }
.label { display:block; color:var(--muted); font-size:11px; margin-bottom:6px; }
.position-line { min-height:28px; display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.strategy-card .position-line { min-height:22px; gap:5px; margin-bottom:0; flex-wrap:wrap; }
.position-pill { display:inline-flex; align-items:center; justify-content:center; min-height:28px; padding:5px 9px; border-radius:7px; font-size:12px; font-weight:900; text-decoration:none; border:1px solid var(--line); }
.position-pill.long { color:#b9f9cc; background:rgba(64,217,123,.16); border-color:rgba(64,217,123,.45); }
.position-pill.short { color:#ffd0d5; background:rgba(255,92,105,.15); border-color:rgba(255,92,105,.48); }
.position-pill.flat { color:#c3ccd8; background:#2a3440; border-color:#3a4654; }
.live-price { min-width:118px; text-align:right; }
.live-price b { font-size:16px; }
.asset-card .metrics { grid-template-columns:repeat(4,minmax(0,1fr)); }
.strategy-card .metrics { grid-template-columns:repeat(5,minmax(0,1fr)); gap:6px; margin:8px 0 0; }
.strategy-card .metric { min-height:52px; padding:8px; }
.strategy-card .metric span { margin-bottom:5px; }
.strategy-card .metric b { font-size:14px; }
.trade-log-card { border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:12px; margin-top:12px; }
.log-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-end; margin-bottom:8px; }
.log-head p { color:var(--muted); font-size:12px; max-width:720px; line-height:1.4; text-align:right; }
.table-wrap { overflow:auto; border:1px solid var(--line); border-radius:7px; margin-top:10px; max-height:520px; }
.compact-table { max-height:300px; }
table { width:100%; border-collapse:collapse; min-width:1120px; }
th, td { padding:8px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }
th { position:sticky; top:0; background:#0d141d; z-index:1; color:#bcd0e8; font-size:11px; }
td { color:#dce8f7; font-size:12px; }
tr:target { outline:2px solid var(--yellow); outline-offset:-2px; background:rgba(242,201,76,.10); }
tr:target td { background:rgba(242,201,76,.08); }
.live-row td { background:rgba(64,217,123,.055); }
.live-dot { display:inline-block; width:9px; height:9px; margin-right:6px; border-radius:999px; background:var(--green); box-shadow:0 0 0 3px rgba(64,217,123,.12); vertical-align:middle; }
.badge { display:inline-flex; align-items:center; justify-content:center; min-height:22px; padding:3px 7px; border-radius:6px; border:1px solid var(--line); font-size:11px; font-weight:900; white-space:nowrap; }
.badge.good { color:#b9f9cc; background:rgba(64,217,123,.16); border-color:rgba(64,217,123,.45); }
.badge.bad { color:#ffd0d5; background:rgba(255,92,105,.15); border-color:rgba(255,92,105,.48); }
.badge.warn { color:#ffe59b; background:rgba(242,201,76,.15); border-color:rgba(242,201,76,.42); }
.badge.muted { color:#c3ccd8; background:#2a3440; border-color:#3a4654; }
.badge.neutral { color:#dbe9ff; background:#1d2938; border-color:#34465c; }
.banner { margin:10px 0; padding:10px 14px; border-radius:8px; font-size:13px; font-weight:800; border:1px solid var(--line); }
.banner.bad { color:#ffd0d5; background:rgba(255,92,105,.14); border-color:rgba(255,92,105,.55); }
.banner.warn { color:#ffe59b; background:rgba(242,201,76,.12); border-color:rgba(242,201,76,.5); }
.why summary { list-style:none; cursor:pointer; }
.why summary::-webkit-details-marker { display:none; }
.why p { color:var(--blue); margin:8px 0 4px; white-space:normal; max-width:520px; }
.why ul { margin:0; padding-left:18px; color:var(--muted); white-space:normal; min-width:340px; max-width:560px; }
.why li { margin:4px 0; line-height:1.35; }
.row-details summary { cursor:pointer; list-style:none; width:24px; height:24px; display:inline-flex; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:999px; color:#bcd0e8; background:#111b27; font-weight:900; }
.row-details summary::-webkit-details-marker { display:none; }
.row-details p { min-width:280px; max-width:520px; color:var(--muted); white-space:normal; line-height:1.35; margin:8px 0; }
.row-details pre { min-width:340px; max-width:620px; max-height:280px; overflow:auto; margin:8px 0 0; padding:10px; border:1px solid var(--line); border-radius:7px; background:#08111b; color:#dce8f7; white-space:pre-wrap; line-height:1.35; }
@media (max-width: 1180px) {
  .asset-grid, #duo_base_dev_trial > .asset-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .metrics { grid-template-columns:repeat(3,minmax(0,1fr)); }
}
@media (max-width: 720px) {
  main { width:calc(100vw - 18px); padding-top:12px; }
  header { flex-direction:column; }
  .asset-grid, #duo_base_dev_trial > .asset-grid { grid-template-columns:1fr; }
  .metrics, .asset-card .metrics, .strategy-card .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
/* ── Kinetic component classes ─────────────────────────── */
.metric-card {
  display:flex; flex-direction:column; gap:4px;
  padding:12px 16px;
  background:var(--bg-panel-raised);
  border:1px solid var(--border-dim);
  border-radius:4px; min-width:0;
}
.metric-label {
  font-size:10px; font-weight:600;
  font-family:var(--font-mono,monospace);
  letter-spacing:.1em; color:var(--text-muted);
  text-transform:uppercase;
}
.metric-value {
  font-size:20px; font-weight:600;
  font-family:var(--font-mono,monospace);
  color:var(--text-primary); line-height:1.2;
}
.metric-sub {
  font-size:11px;
  font-family:var(--font-mono,monospace);
  color:var(--text-secondary);
}
.section-header {
  font-size:11px; font-weight:600;
  font-family:var(--font-mono,monospace);
  letter-spacing:.12em; color:var(--text-muted);
  text-transform:uppercase;
  padding-bottom:8px;
  border-bottom:1px solid var(--border-dim);
  margin-bottom:16px;
}
.level-badge {
  display:inline-flex; align-items:center;
  padding:2px 10px; border-radius:3px;
  font-size:11px; font-weight:600;
  font-family:var(--font-mono,monospace);
  letter-spacing:.08em; text-transform:uppercase;
}
.signal-badge {
  display:inline-flex; align-items:center; gap:4px;
  padding:2px 8px; border-radius:3px;
  font-size:10px; font-weight:700;
  font-family:var(--font-mono,monospace);
  letter-spacing:.1em; text-transform:uppercase;
  border:1px solid;
}
.bar-track {
  height:8px; background:var(--border-dim);
  border-radius:4px; overflow:hidden;
}
.bar-fill {
  height:100%; border-radius:4px;
  transition:width .6s cubic-bezier(.25,1,.5,1);
}
.status-indicator {
  display:inline-flex; align-items:center; gap:6px;
  font-size:11px;
  font-family:var(--font-mono,monospace);
  letter-spacing:.06em;
}
.status-dot { width:8px; height:8px; border-radius:50%; }
.live-dot.active {
  background:var(--positive);
  box-shadow:0 0 6px var(--positive);
  animation:pulse-glow 2s ease-in-out infinite;
}
.live-dot.inactive { background:var(--text-muted); }
@keyframes pulse-glow {
  0%,100% { opacity:1; }
  50% { opacity:.4; }
}
/* Summary cards row */
.summary-metrics {
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:12px; margin:16px 0;
}
@media(max-width:900px) {
  .summary-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
@media(max-width:500px) {
  .summary-metrics { grid-template-columns:1fr; }
}
"""


# --- Built Next.js SPA (dashboard-ui/out) + dev CORS --------------------------
# When dashboard-ui/out/ exists (after `npm run build`), the static export takes
# over "/" and all asset paths; until then the legacy server-rendered HTML is the
# fallback. HERMX_DEV_CORS lets the Next dev server (localhost:3001) call /api and
# /health cross-origin during development.
STATIC_DIR = REPO_ROOT / "dashboard-ui" / "out"
DEV_CORS_ENABLED = bool((os.environ.get("HERMX_DEV_CORS") or "").strip())
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


class Handler(BaseHTTPRequestHandler):
    def _dashboard_auth_ok(self) -> bool:
        if not DASH_AUTH_ENABLED:
            return True
        if not DASH_AUTH_TOKEN:
            return False
        provided = (self.headers.get("X-Dashboard-Token") or "").strip()
        if provided and hmac.compare_digest(provided, DASH_AUTH_TOKEN):
            return True
        auth_header = (self.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            return bool(token) and hmac.compare_digest(token, DASH_AUTH_TOKEN)
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                _user, pwd = decoded.split(":", 1)
            except Exception:
                return False
            return bool(pwd) and hmac.compare_digest(pwd, DASH_AUTH_TOKEN)
        return False

    def _maybe_cors(self):
        # Dev-only: allow the Next dev server (localhost:3001) to read /api,/health.
        if not DEV_CORS_ENABLED:
            return
        self.send_header("Access-Control-Allow-Origin", DEV_CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _auth_challenge(self):
        body = {"ok": False, "error": "unauthorized"}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self._maybe_cors()
        # Offer both Basic (browser native prompt) and Bearer (tools/APIs).
        self.send_header("WWW-Authenticate", 'Basic realm="hermx-dashboard"')
        self.send_header("WWW-Authenticate", 'Bearer realm="hermx-dashboard"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self._maybe_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight (dev only). 204 No Content with the allow-* headers.
        self.send_response(204)
        self._maybe_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, path):
        # Serve the built Next.js export from dashboard-ui/out, never escaping it.
        # Strip the /dashboard basePath prefix (Next builds with basePath=/dashboard).
        if path.startswith("/dashboard"):
            path = path[len("/dashboard"):] or "/"
        out_root = STATIC_DIR.resolve()
        rel = path.lstrip("/") or "index.html"
        candidate = (out_root / rel).resolve()
        # trailingSlash export emits dir/index.html (e.g. /health/ -> health/index.html).
        if candidate.is_dir():
            candidate = (candidate / "index.html").resolve()
        if candidate != out_root and out_root not in candidate.parents:
            self.send_bytes(404, b"not found", "text/plain")
            return
        if not candidate.is_file():
            self.send_bytes(404, b"not found", "text/plain")
            return
        ctype = STATIC_MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        body = candidate.read_bytes()
        # Inject the auth token into index.html so the SPA can read it from a meta
        # tag instead of baking the secret into the JS bundle at build time.
        if candidate.name == "index.html" and DASH_AUTH_TOKEN:
            meta = f'<meta name="hermx-token" content="{DASH_AUTH_TOKEN}">'
            body = body.replace(b"</head>", meta.encode("utf-8") + b"</head>", 1)
        self.send_bytes(200, body, ctype)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/dashboard", "/dashboard/", "/dashboard/api", "/dashboard/api/", "/", "/shadow/dashboard", "/api", "/shadow/dashboard/api"} and not self._dashboard_auth_ok():
            self._auth_challenge()
            return
        if path in {"/api/signals", "/shadow/api/signals", "/dashboard/api/signals"}:
            if not self._dashboard_auth_ok():
                self._auth_challenge()
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                n = int((qs.get("n") or [SIGNALS_DEFAULT_N])[0])
            except (TypeError, ValueError):
                n = SIGNALS_DEFAULT_N
            symbol = (qs.get("symbol") or [None])[0]
            try:
                payload = signals_payload(n, symbol)
            except Exception as exc:  # unexpected read/projection failure only
                self.send_bytes(500, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            self.send_bytes(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        static_ready = STATIC_DIR.is_dir()
        if path in {"/dashboard/api", "/dashboard/api/", "/api", "/shadow/dashboard/api"}:
            payload = api_payload()
            self.send_bytes(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path in {"/health", "/shadow/health"}:
            self.send_bytes(200, json.dumps(health_payload(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif static_ready and (path.startswith("/dashboard") or path == "/"):
            # React SPA (built with basePath=/dashboard) handles /dashboard/* and /.
            self._serve_static(path)
        elif path in {"/dashboard", "/dashboard/", "/shadow/dashboard"}:
            # Fallback: legacy Python HTML when React build not present.
            self.send_bytes(200, render().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/":
            self.send_bytes(200, render().encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_bytes(404, b"not found", "text/plain")

    # ---- Strategy-mode control (write) -------------------------------------
    # POST   /api/control/strategy/{id}  body {"mode": "demo"|"live"|"clear"}
    # DELETE /api/control/strategy/{id}  -> clear override (restore file default)
    _CONTROL_PREFIXES = (
        "/dashboard/api/control/strategy/",
        "/shadow/dashboard/api/control/strategy/",
        "/api/control/strategy/",
    )

    def _strategy_control_id(self, path):
        """Return the {id} from a strategy-control path, or None if not such a route."""
        for prefix in self._CONTROL_PREFIXES:
            if path.startswith(prefix):
                return unquote(path[len(prefix):]).strip("/").strip()
        return None

    def _send_control_error(self, status, message):
        body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def _apply_strategy_control(self, sid, mode):
        sid = (sid or "").strip()
        if not sid:
            self._send_control_error(400, "missing strategy_id")
            return
        known = {s.get("strategy_id") for s in active_strategies()}
        if sid not in known:
            self._send_control_error(404, f"unknown strategy_id: {sid}")
            return
        mode = _LEGACY_STRATEGY_MODE_ALIASES.get(mode, mode)  # accept legacy shadow->pause, paper->demo
        if mode not in {"pause", "demo", "live", "clear"}:
            self._send_control_error(400, "mode must be one of: pause, demo, live, clear")
            return
        if mode == "clear":
            _clear_strategy_override(sid)  # idempotent: no-op if no override existed
            resp = {"ok": True, "strategy_id": sid, "mode": "clear"}
        else:
            if not _set_strategy_override(sid, mode):
                self._send_control_error(400, "failed to set override")
                return
            resp = {"ok": True, "strategy_id": sid, "mode": mode}
        self.send_bytes(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        sid = self._strategy_control_id(path)
        if sid is None:
            self.send_bytes(404, b"not found", "text/plain")
            return
        if not self._dashboard_auth_ok():
            self._auth_challenge()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        mode = ""
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_control_error(400, "invalid JSON body")
                return
            mode = str((body or {}).get("mode") or "").strip().lower()
        self._apply_strategy_control(sid, mode)

    def do_DELETE(self):
        path = urlparse(self.path).path
        sid = self._strategy_control_id(path)
        if sid is None:
            self.send_bytes(404, b"not found", "text/plain")
            return
        if not self._dashboard_auth_ok():
            self._auth_challenge()
            return
        self._apply_strategy_control(sid, "clear")

    def log_message(self, _fmt, *_args):
        return


if __name__ == "__main__":
    if DASH_AUTH_ENABLED and not DASH_AUTH_TOKEN:
        print("[dashboard] HERMX_DASH_AUTH enabled but HERMX_SECRET is blank; failing closed with 401 for protected routes.", file=sys.stderr)
    ThreadingHTTPServer((HERMX_BIND_HOST, PORT), Handler).serve_forever()
