#!/usr/bin/env python3
"""Parallel MXC shadow webhook receiver.

Safe by design: receives TradingView alerts, answers quickly, enriches with
available MXC context, writes append-only ledgers, and only calls OKX when the
active config explicitly enables sandbox/demo execution.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("SHADOW_PORT", "8891"))
SECRET = os.environ.get("SHADOW_WEBHOOK_SECRET", "")
ROOT = Path(os.environ.get("SHADOW_ROOT", Path(__file__).resolve().parents[1]))
LOG_DIR = ROOT / "logs"
LATEST_FILE = ROOT / "latest.json"
WEBHOOK_LEDGER = LOG_DIR / "shadow-webhooks.jsonl"
RAW_INTAKE_LEDGER = LOG_DIR / "shadow-intake.jsonl"
DECISION_LEDGER = LOG_DIR / "shadow-decisions.jsonl"
PAPER_STATE_FILE = ROOT / "paper-state.json"
CONTROL_STATE_FILE = ROOT / "control-state.json"
PAPER_TRADES_LEDGER = LOG_DIR / "paper-trades.jsonl"
# Generic, exchange-agnostic execution ledgers. The legacy OKX-named files are
# kept as compatibility mirrors so older dashboards/tools keep reading history.
EXECUTION_PLAN_LEDGER = LOG_DIR / "execution-plan.jsonl"
EXECUTION_LEDGER = LOG_DIR / "executions.jsonl"
LEGACY_EXECUTION_PLAN_LEDGER = LOG_DIR / "okx-execution-plan.jsonl"
LEGACY_EXECUTION_LEDGER = LOG_DIR / "okx-executions.jsonl"
SIGNAL_STATE_FILE = ROOT / "seen-signals.json"
DUPLICATE_LEDGER = LOG_DIR / "shadow-duplicates.jsonl"
TAB_HEALTH_LEDGER = LOG_DIR / "tab-health.jsonl"
CONFIG_FILE = ROOT / "shadow-config.json"
POLICY_LABELS = {
    "duo_raw": "Duo Full",
    "v52_fast_1h": "30m+1H Candidate",
    "v6_regime_duo": "Regime Duo",
    "duo_conviction_sized": "Duo Conviction Sized",
    "conviction_v2_candidate": "Conviction V2 Candidate",
    "duo_regime_rsi_sized": "Duo Regime RSI Sized",
    "duo_regime_rsi_30m": "Duo Regime RSI 30m",
    "v3_r75": "Legacy V3 R75",
    "v5_mtf": "Legacy V5 MTF",
    "v51_balanced": "Legacy V5.1",
}


def load_shadow_config() -> dict:
    default = {
        "mode": os.environ.get("SHADOW_MODE", "paper_shadow"),
        "primary_policy": os.environ.get("PRIMARY_POLICY", "not_selected"),
        "execution_timeframe": "30m",
        "chart_type": os.environ.get("DEFAULT_CHART_TYPE", "heikin_ashi"),
        "base_notional_usd": float(os.environ.get("PAPER_BASE_NOTIONAL_USD", "10000")),
        "fees": {
            "maker_rate": float(os.environ.get("OKX_PERP_MAKER_FEE_RATE", "0.0002")),
            "taker_rate": float(os.environ.get("OKX_PERP_TAKER_FEE_RATE", "0.0005")),
            "default_liquidity": os.environ.get("PAPER_DEFAULT_LIQUIDITY", "taker").lower(),
        },
        "funding": {
            "enabled": os.environ.get("PAPER_FUNDING_ENABLED", "false").lower() == "true",
            "default_rate": float(os.environ.get("PAPER_DEFAULT_FUNDING_RATE", "0")),
        },
        "policies": {"enabled": ["duo_raw", "duo_regime_rsi_30m"]},
        "execution": {
            "enabled": False,
            "mode": "dry_run",
            # Exchange key understood by ExecutorFactory. "okx" is accepted as a
            # backward-compat alias for "okx_demo".
            "exchange": "okx_demo",
            "execution_policy": "duo_raw",
            "shadow_policy": "duo_regime_rsi_30m",
            "route": "okx_api",
            "account": "sandbox",
        },
        # Assets use the generic "inst_id"; "okx_inst_id" remains readable as a
        # fallback for older configs (see asset_inst_id()).
        "assets": {
            "XRPUSDT": {"enabled": True, "budget_usd": 1500, "leverage": 2, "inst_id": "XRP-USDT-SWAP", "timeframe": "30m"},
            "SOLUSDT": {"enabled": True, "budget_usd": 1500, "leverage": 2, "inst_id": "SOL-USDT-SWAP", "timeframe": "30m"},
            "ETHUSDT": {"enabled": True, "budget_usd": 2000, "leverage": 2, "inst_id": "ETH-USDT-SWAP", "timeframe": "30m"},
        },
        "risk": {"allow_live_execution": False, "duplicate_protection": True, "max_slippage_pct": 0.25, "max_daily_loss_usd": 150.0},
    }
    if not CONFIG_FILE.exists():
        return default
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        merged = default | loaded
        merged["fees"] = default["fees"] | loaded.get("fees", {})
        merged["funding"] = default["funding"] | loaded.get("funding", {})
        merged["policies"] = default["policies"] | loaded.get("policies", {})
        merged["execution"] = default["execution"] | loaded.get("execution", {})
        merged["assets"] = default["assets"] | loaded.get("assets", {})
        merged["risk"] = default["risk"] | loaded.get("risk", {})
        return merged
    except Exception as exc:
        logging.warning("Failed to load shadow config: %s", exc)
        return default


CONFIG = load_shadow_config()
STRATEGY_ENGINE = CONFIG.get("strategy_engine", {}) or {}
STRATEGIES_DIR = ROOT / str(STRATEGY_ENGINE.get("strategies_dir") or "strategies")
STRATEGY_ALERT_LEDGER = LOG_DIR / "strategy-alerts.jsonl"
STRATEGY_QUARANTINE_LEDGER = LOG_DIR / "strategy-alert-quarantine.jsonl"
POLICY_KEYS = tuple(CONFIG.get("policies", {}).get("enabled") or ["duo_raw", "duo_regime_rsi_30m"])
MTF_POLICY_KEYS = {"v52_fast_1h", "v6_regime_duo", "duo_conviction_sized", "conviction_v2_candidate", "duo_regime_rsi_sized"}
MTF_REQUIRED = any(key in set(POLICY_KEYS) for key in MTF_POLICY_KEYS)
PRIMARY_POLICY = str(CONFIG.get("primary_policy") or "not_selected")
PRIMARY_POLICY_SELECTED = PRIMARY_POLICY not in ("", "none", "not_selected", "observation_only")
PAPER_BASE_NOTIONAL_USD = float(CONFIG.get("base_notional_usd") or 10000)
OKX_PERP_MAKER_FEE_RATE = float(CONFIG.get("fees", {}).get("maker_rate", 0.0002))
OKX_PERP_TAKER_FEE_RATE = float(CONFIG.get("fees", {}).get("taker_rate", 0.0005))
PAPER_DEFAULT_LIQUIDITY = str(CONFIG.get("fees", {}).get("default_liquidity", "taker")).lower()
DEFAULT_CHART_TYPE = str(CONFIG.get("chart_type") or "heikin_ashi")
PAPER_FUNDING_ENABLED = bool(CONFIG.get("funding", {}).get("enabled", False))
PAPER_DEFAULT_FUNDING_RATE = float(CONFIG.get("funding", {}).get("default_rate", 0.0))


def canonical_timeframe(value) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    aliases = {
        "30": "30m",
        "30min": "30m",
        "30mins": "30m",
        "30minute": "30m",
        "30minutes": "30m",
        "60": "1h",
        "1hr": "1h",
        "1hour": "1h",
        "120": "2h",
        "2hr": "2h",
        "2hour": "2h",
        "180": "3h",
        "3hr": "3h",
        "3hour": "3h",
        "240": "4h",
        "4hr": "4h",
        "4hour": "4h",
    }
    return aliases.get(text, text)


def load_strategy_files() -> dict:
    strategies = {}
    if not STRATEGIES_DIR.exists():
        return strategies
    for path in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            sid = str(row.get("strategy_id") or "").strip()
            if not sid:
                continue
            row["_path"] = str(path)
            row["timeframe"] = canonical_timeframe(row.get("timeframe"))
            row["asset"] = str(row.get("asset") or "").upper()
            strategies[sid] = row
        except Exception as exc:
            logging.warning("Failed to load strategy file %s: %s", path, exc)
    return strategies


STRATEGIES = load_strategy_files()

LOG_DIR.mkdir(parents=True, exist_ok=True)
PROCESS_QUEUE: queue.Queue[tuple[dict, str]] = queue.Queue()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.FileHandler(LOG_DIR / "receiver.log"), logging.StreamHandler(sys.stdout)],
)
logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()

sys.path.insert(0, "/root/trading-system")
# Ensure this src/ directory is importable so the executors package resolves
# regardless of the working directory the receiver is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from cdp_indicator_reader import read_indicator_values
except Exception as exc:  # fail closed
    read_indicator_values = None
    logging.warning("CDP reader unavailable: %s", exc)

# Exchange-agnostic execution layer. The factory selects the right adapter from
# config["execution"]["exchange"] (okx_demo, kucoin_paper, bybit_testnet, ...).
try:
    from executors import ExecutorFactory
except Exception as exc:  # fail closed: execution simply stays disabled
    ExecutorFactory = None
    logging.warning("Executor factory unavailable: %s", exc)

ALLOWED_SYMBOLS = {symbol for symbol, cfg in CONFIG.get("assets", {}).items() if cfg.get("enabled", True)}
ALLOWED_SIDES = {"buy", "sell"}
MTF_TIMEFRAMES = ("1h",)
ACTIVE_MTF_TIMEFRAMES = MTF_TIMEFRAMES if MTF_REQUIRED else ()
CORE_MXC_FIELDS = ("pp_acc", "pp_vel")
LIVE_READ_ATTEMPTS = int(os.environ.get("MXC_LIVE_READ_ATTEMPTS", "1"))
LIVE_READ_SLEEP_SECONDS = float(os.environ.get("MXC_LIVE_READ_SLEEP_SECONDS", "1.5"))
HEALTH_CACHE_MAX_AGE_SECONDS = float(os.environ.get("MXC_HEALTH_CACHE_MAX_AGE_SECONDS", "420"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_tv_time(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit():
            raw = int(text)
            if raw > 10_000_000_000:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def latency_info(tv_time, received_at: str) -> dict:
    received_dt = parse_tv_time(received_at) or datetime.now(timezone.utc)
    tv_dt = parse_tv_time(tv_time)
    if not tv_dt:
        return {"tv_time_parse_ok": False, "latency_seconds": None, "latency_minutes": None}
    seconds = (received_dt - tv_dt).total_seconds()
    return {
        "tv_time_parse_ok": True,
        "latency_seconds": round(seconds, 3),
        "latency_minutes": round(seconds / 60.0, 3),
    }


def dedupe_key(normalized: dict) -> str:
    return "|".join(str(normalized.get(k, "")) for k in ("strategy_id", "symbol", "side", "timeframe", "tv_time"))


def load_signal_state() -> dict:
    if not SIGNAL_STATE_FILE.exists():
        return {"version": 1, "signals": {}, "keys": {}}
    try:
        state = json.loads(SIGNAL_STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("signals", {})
        state.setdefault("keys", {})
        return state
    except Exception:
        return {"version": 1, "signals": {}, "keys": {}}


def save_signal_state(state: dict) -> None:
    # Keep the file bounded; 5000 recent signals is plenty for shadow alerts.
    for bucket in ("signals", "keys"):
        items = list((state.get(bucket) or {}).items())[-5000:]
        state[bucket] = dict(items)
    tmp = SIGNAL_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SIGNAL_STATE_FILE)


def check_and_mark_signal(normalized: dict, received_at: str) -> tuple[bool, dict]:
    state = load_signal_state()
    sid = str(normalized.get("signal_id") or "")
    key = dedupe_key(normalized)
    existing = None
    duplicate_by = None
    if sid and sid in state.get("signals", {}):
        existing = state["signals"][sid]
        duplicate_by = "signal_id"
    elif key in state.get("keys", {}):
        existing = state["keys"][key]
        duplicate_by = "symbol_side_timeframe_tv_time"
    meta = {
        "signal_id": sid,
        "dedupe_key": key,
        "duplicate_by": duplicate_by,
        "first_seen_at": (existing or {}).get("first_seen_at"),
    }
    if existing:
        return True, meta
    entry = {"first_seen_at": received_at, "signal_id": sid, "dedupe_key": key, "symbol": normalized.get("symbol"), "side": normalized.get("side"), "timeframe": normalized.get("timeframe"), "tv_time": normalized.get("tv_time")}
    if sid:
        state.setdefault("signals", {})[sid] = entry
    state.setdefault("keys", {})[key] = entry
    state["updated_at"] = received_at
    save_signal_state(state)
    meta["first_seen_at"] = received_at
    return False, meta


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mxc_core_ok(values: dict | None) -> bool:
    return bool(values and all(values.get(field) is not None for field in CORE_MXC_FIELDS))


def read_indicator_values_stable(symbol: str, timeframe: str) -> tuple[dict | None, str | None]:
    """Read CDP values, retrying when TradingView has not hydrated Data Window yet."""
    if read_indicator_values is None:
        return None, "cdp_reader_unavailable"
    last_values = None
    last_error = None
    attempts = max(1, LIVE_READ_ATTEMPTS)
    for attempt in range(attempts):
        try:
            last_values = read_indicator_values(symbol, timeframe)
            last_error = None
            if mxc_core_ok(last_values):
                if attempt:
                    last_values = dict(last_values)
                    last_values["_live_read_attempts"] = attempt + 1
                return last_values, None
            last_error = f"missing_core_fields:{sorted((last_values or {}).keys())}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < attempts - 1:
            time.sleep(LIVE_READ_SLEEP_SECONDS)
    if last_values is not None:
        last_values = dict(last_values)
        last_values["_live_read_attempts"] = attempts
        last_values["_live_read_warning"] = last_error
    return last_values, last_error


def health_age_seconds(health: dict | None) -> float | None:
    if not health:
        return None
    checked = parse_tv_time(health.get("checked_at"))
    if not checked:
        return None
    return (datetime.now(timezone.utc) - checked).total_seconds()


def cached_health_values(health: dict | None, symbol: str, timeframe: str) -> dict | None:
    age = health_age_seconds(health)
    if age is None or age > HEALTH_CACHE_MAX_AGE_SECONDS:
        return None
    symbol = str(symbol or "").upper()
    for row in (health or {}).get("results") or []:
        if row.get("symbol") == symbol and row.get("timeframe") == timeframe and row.get("ok"):
            fields = dict(row.get("fields") or {})
            if mxc_core_ok(fields):
                fields["_fallback_source"] = "tab_health_cache"
                fields["_fallback_age_seconds"] = round(age, 3)
                return fields
    return None


def trigger_health_repair(reason: str, symbol: str, timeframe: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "start", "--no-block", "mxc-tab-health.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        logging.warning("Triggered immediate tab health repair reason=%s symbol=%s timeframe=%s", reason, symbol, timeframe)
    except Exception as exc:
        logging.warning("Failed to trigger tab health repair reason=%s symbol=%s timeframe=%s error=%s", reason, symbol, timeframe, exc)


def first(payload: dict, *names: str, default=""):
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return default


def normalize(payload: dict) -> dict:
    strategy_id = str(first(payload, "strategy_id", "strategyId", default="")).strip()
    strategy_name = str(first(payload, "strategy_name", "strategyName", default="")).strip()
    indicator = str(first(payload, "indicator", "indicator_name", "indicatorName", default="")).strip()
    symbol = str(first(payload, "symbol", "ticker", default="")).upper()
    symbol = symbol.replace("OKX:", "").replace("/", "").replace("-", "")
    side = str(first(payload, "side", "action", default="")).lower()
    timeframe = canonical_timeframe(first(payload, "timeframe", "interval", default="30m"))
    tv_time = str(first(payload, "tv_time", "time", "timestamp", "bar_time", "candle_time", default=now_iso()))
    signal_id = str(first(payload, "signal_id", default=""))
    if not signal_id:
        raw = f"{strategy_id}|{symbol}|{side}|{timeframe}|{tv_time}"
        signal_id = hashlib.sha256(raw.encode()).hexdigest()
    return {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "indicator": indicator,
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "tv_signal_price": as_float(first(payload, "tv_signal_price", "tv_close", "signal_price", "price", "close", default=None)),
        "chart_type": str(first(payload, "chart_type", default=DEFAULT_CHART_TYPE)).lower(),
        "okx_mark_price": as_float(first(payload, "okx_mark_price", "mark_price", default=None)),
        "okx_last_price": as_float(first(payload, "okx_last_price", "last_price", default=None)),
        "tv_time": tv_time,
        "exchange": str(first(payload, "exchange", default="okx")).lower(),
        "strategy": str(first(payload, "strategy", default="")),
        "source": str(first(payload, "source", default="tradingview")),
        "signal_id": signal_id,
    }


def validate_strategy_alert(normalized: dict) -> tuple[bool, dict | None, str | None]:
    strategy_id = str(normalized.get("strategy_id") or "").strip()
    if not strategy_id:
        if bool(STRATEGY_ENGINE.get("require_strategy_id", False)):
            return False, None, "missing_strategy_id_required"
        indicator = str(normalized.get("indicator") or "").lower()
        strategy_name = str(normalized.get("strategy_name") or normalized.get("strategy") or "").lower()
        if "duo-base" in indicator or "duo base" in indicator or "duo-base" in strategy_name or "duo base" in strategy_name:
            return False, None, "missing_strategy_id"
        return True, None, None
    if not bool(STRATEGY_ENGINE.get("allow_strategy_alerts", True)):
        return False, None, "strategy_alerts_disabled"
    strategy = STRATEGIES.get(strategy_id)
    if not strategy:
        return False, None, "unknown_strategy_id"
    if str(strategy.get("asset") or "").upper() != str(normalized.get("symbol") or "").upper():
        return False, strategy, "strategy_symbol_mismatch"
    if canonical_timeframe(strategy.get("timeframe")) != canonical_timeframe(normalized.get("timeframe")):
        return False, strategy, "strategy_timeframe_mismatch"
    if str(strategy.get("status") or "") not in {"trial_candidate", "active_demo"}:
        return False, strategy, "strategy_not_active"
    return True, strategy, None


def regime_from_acc(acc):
    if acc is None:
        return "unknown"
    if acc >= 10:
        return "BULL_STRONG"
    if acc > 2:
        return "BULL"
    if acc <= -10:
        return "BEAR_STRONG"
    if acc < -2:
        return "BEAR"
    return "TRANSITION"


def phase_from_acc_vel(acc, vel):
    if acc is None or vel is None:
        return "unknown"
    if vel >= 0 and acc >= 0:
        return "Q1_RISE"
    if vel >= 0 and acc < 0:
        return "Q2_TOP"
    if vel < 0 and acc < 0:
        return "Q3_DROP"
    return "Q4_BASE"


def pulse_label(values, direction: str):
    bkt = as_float(values.get("pulse_bkt"))
    rs = as_float(values.get("pulse_rs_mkt"))
    score = 0.0
    if bkt is not None:
        score += 25 if bkt >= 65 else (-20 if bkt < 50 else 0)
    if rs is not None:
        if direction == "long":
            score += 10 if rs > -20 else -15
        else:
            score += 10 if rs < 30 else -15
    if score >= 35:
        return "PULSE_STRONG"
    if score >= 15:
        return "PULSE_OK"
    if score <= -20:
        return "PULSE_WEAK"
    return "PULSE_MIXED"


def risk_on(values, direction: str):
    acc = as_float(values.get("pp_acc"))
    vel = as_float(values.get("pp_vel"))
    if acc is None or vel is None:
        return None
    bkt = as_float(values.get("pulse_bkt"))
    rs = as_float(values.get("pulse_rs_mkt"))
    sign = 1 if direction == "long" else -1
    score = max(-25, min(25, acc * sign)) * 1.4
    score += max(-20, min(20, vel * sign))
    if bkt is not None:
        score += (bkt - 50) * 0.35
    if rs is not None:
        score += max(-40, min(40, rs * sign)) * 0.35
    return round(max(0.0, min(100.0, 50.0 + score)), 2)


def no_pulse_score(values, direction: str):
    acc = as_float((values or {}).get("pp_acc"))
    vel = as_float((values or {}).get("pp_vel"))
    if acc is None or vel is None:
        return None
    sign = 1 if direction == "long" else -1
    score = 50.0
    score += max(-25, min(25, acc * sign)) * 1.55
    score += max(-20, min(20, vel * sign)) * 0.85
    return round(max(0.0, min(100.0, score)), 2)


def extract_jrsx(values: dict | None):
    for study in (values or {}).get("raw_studies") or []:
        name = str(study.get("name") or "").lower()
        if "jrsx" in name or "rsi" in name:
            last = study.get("last") or {}
            raw_values = last.get("values") or {}
            for key in ("plot_0", "plot_1"):
                val = as_float(raw_values.get(key))
                if val is not None and 0 <= val <= 100:
                    return val
    return None


def rsi_caution(direction: str, rsi):
    rsi = as_float(rsi)
    if rsi is None:
        return "RSI_UNKNOWN"
    if direction == "long":
        if rsi >= 80:
            return "RSI_OVERHEATED"
        if rsi <= 25:
            return "RSI_RECOVERY_ZONE"
    else:
        if rsi <= 20:
            return "RSI_OVERSOLD_SHORT_CAUTION"
        if rsi >= 70:
            return "RSI_DISTRIBUTION_ZONE"
    return "RSI_NEUTRAL"



def base_context(normalized: dict, values: dict | None) -> dict:
    if not values:
        return {
            "available": False,
            "direction": "long" if normalized["side"] == "buy" else "short",
            "regime": "unknown",
            "phase": "unknown",
            "pulse": "unknown",
            "risk_on": None,
            "no_pulse_score": None,
            "jrsx": None,
            "rsi_caution": "RSI_UNKNOWN",
        }
    direction = "long" if normalized["side"] == "buy" else "short"
    acc = as_float(values.get("pp_acc"))
    vel = as_float(values.get("pp_vel"))
    jrsx = extract_jrsx(values)
    return {
        "available": acc is not None and vel is not None,
        "direction": direction,
        "regime": regime_from_acc(acc),
        "phase": phase_from_acc_vel(acc, vel),
        "pulse": pulse_label(values, direction),
        "risk_on": risk_on(values, direction),
        "no_pulse_score": no_pulse_score(values, direction),
        "jrsx": jrsx,
        "rsi_caution": rsi_caution(direction, jrsx),
        "pp_acc": acc,
        "pp_vel": vel,
        "pulse_bkt": as_float(values.get("pulse_bkt")),
        "pulse_rs_mkt": as_float(values.get("pulse_rs_mkt")),
    }


def policy_result(name, decision, risk_weight, leverage, reasons, **extra):
    out = {
        "name": name,
        "decision": decision,
        "risk_weight": risk_weight,
        "leverage": leverage,
        "reasons": reasons,
    }
    out.update(extra)
    return out


def decide_duo_raw(normalized: dict, ctx: dict) -> dict:
    direction = "LONG" if normalized["side"] == "buy" else "SHORT"
    return policy_result(
        "duo_raw",
        "TRADE",
        1.0,
        1.0,
        [f"Duo Crypto {normalized['side'].upper()} opens/reverses {direction}", "Baseline only; no MXC intelligence filter"],
        action="FULL_ENTRY",
        target_direction=direction,
    )


def direction_confirms(ctx: dict) -> bool:
    if not ctx.get("available"):
        return False
    if ctx["direction"] == "long":
        return str(ctx["regime"]).startswith("BULL") and ctx["phase"] in {"Q1_RISE", "Q4_BASE"}
    return str(ctx["regime"]).startswith("BEAR") and ctx["phase"] in {"Q2_TOP", "Q3_DROP"}


def decide_v3_r75(ctx: dict) -> dict:
    if not ctx.get("available"):
        return policy_result("v3_r75", "SKIP", 0, 0, ["No live MXC CDP values available"])
    confirms = direction_confirms(ctx)
    ro = ctx.get("risk_on")
    pulse = ctx.get("pulse")
    reasons = [f"30m context: regime={ctx['regime']}, phase={ctx['phase']}, pulse={pulse}, risk_on={ro}"]
    if confirms and ro is not None and ro >= 75 and pulse in {"PULSE_OK", "PULSE_STRONG"}:
        reasons.append("R75-style recovery: direction confirmed and risk_on >= 75")
        return policy_result("v3_r75", "REDUCE", 0.5, 1.0, reasons, action="PARTIAL_ENTRY", confidence="medium", approximation="vps_30m_only")
    if confirms and ro is not None and ro >= 60:
        reasons.append("Directional context exists but below R75 conviction")
        return policy_result("v3_r75", "REDUCE_SMALL", 0.25, 1.0, reasons, action="SMALL_ENTRY", confidence="low", approximation="vps_30m_only")
    reasons.append("No base 30m edge")
    return policy_result("v3_r75", "SKIP", 0, 0, reasons, action="SKIP", confidence="none", approximation="vps_30m_only")


def step_weight(weight: float, delta: float) -> float:
    levels = [0.0, 0.25, 0.5, 0.75, 1.0]
    target = max(0.0, min(1.0, weight + delta))
    return min(levels, key=lambda x: abs(x - target))


def convert_weight_policy(name: str, weight: float, notes: list[str], *, mtf_status: str, mtf_summary: dict | None = None) -> dict:
    if weight >= 0.9:
        decision, leverage, action = "TRADE", 1.0, "FULL_ENTRY"
    elif weight >= 0.2:
        decision, leverage, action = "REDUCE", 1.0, "PARTIAL_ENTRY"
    else:
        decision, leverage, action = "SKIP", 0.0, "SKIP"
    extra = {"action": action, "mtf_status": mtf_status}
    if mtf_summary is not None:
        extra["mtf_summary"] = mtf_summary
    return policy_result(name, decision, weight, leverage, notes, **extra)


def read_mtf_values(symbol: str) -> tuple[dict, dict]:
    values_by_tf = {}
    errors = {}
    if not ACTIVE_MTF_TIMEFRAMES:
        return values_by_tf, errors
    if read_indicator_values is None:
        return values_by_tf, {tf: "cdp_reader_unavailable" for tf in ACTIVE_MTF_TIMEFRAMES}
    for tf in ACTIVE_MTF_TIMEFRAMES:
        values, error = read_indicator_values_stable(symbol, tf)
        values_by_tf[tf] = values or {}
        if error and not mxc_core_ok(values):
            errors[tf] = error
    return values_by_tf, errors


def mtf_contexts(normalized: dict, mtf_values: dict | None) -> dict:
    out = {}
    for tf in ACTIVE_MTF_TIMEFRAMES:
        out[tf] = base_context(normalized, (mtf_values or {}).get(tf))
    return out


def mtf_summary(ctxs: dict, direction: str) -> dict:
    available = {tf: ctx for tf, ctx in ctxs.items() if ctx.get("available") and ctx.get("risk_on") is not None}
    confirms = [tf for tf, ctx in available.items() if direction_confirms(ctx)]
    opposes = []
    for tf, ctx in available.items():
        regime = str(ctx.get("regime", ""))
        phase = ctx.get("phase")
        if direction == "long" and regime.startswith("BEAR") and phase in {"Q2_TOP", "Q3_DROP"}:
            opposes.append(tf)
        if direction == "short" and regime.startswith("BULL") and phase in {"Q1_RISE", "Q4_BASE"}:
            opposes.append(tf)
    risks = [float(ctx.get("risk_on")) for ctx in available.values() if ctx.get("risk_on") is not None]
    avg_risk = round(sum(risks) / len(risks), 2) if risks else None
    detail = {
        tf: {
            "available": ctx.get("available"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "risk_on": ctx.get("risk_on"),
            "pulse": ctx.get("pulse"),
            "confirms": direction_confirms(ctx),
        }
        for tf, ctx in ctxs.items()
    }
    return {
        "available_count": len(available),
        "confirm_count": len(confirms),
        "oppose_count": len(opposes),
        "confirm_timeframes": confirms,
        "oppose_timeframes": opposes,
        "avg_risk_on": avg_risk,
        "detail": detail,
    }


def context_opposes(ctx: dict) -> bool:
    if not ctx.get("available"):
        return False
    direction = ctx.get("direction", "long")
    regime = str(ctx.get("regime", ""))
    phase = ctx.get("phase")
    if direction == "long":
        return regime.startswith("BEAR") and phase in {"Q2_TOP", "Q3_DROP"}
    return regime.startswith("BULL") and phase in {"Q1_RISE", "Q4_BASE"}


def single_tf_summary(tf: str, ctx: dict) -> dict:
    available = bool(ctx.get("available") and ctx.get("risk_on") is not None)
    confirms = available and direction_confirms(ctx)
    opposes = available and context_opposes(ctx)
    risk = ctx.get("risk_on") if available else None
    return {
        "available_count": 1 if available else 0,
        "confirm_count": 1 if confirms else 0,
        "oppose_count": 1 if opposes else 0,
        "confirm_timeframes": [tf] if confirms else [],
        "oppose_timeframes": [tf] if opposes else [],
        "avg_risk_on": risk,
        "detail": {
            tf: {
                "available": ctx.get("available"),
                "regime": ctx.get("regime"),
                "phase": ctx.get("phase"),
                "risk_on": ctx.get("risk_on"),
                "pulse": ctx.get("pulse"),
                "confirms": bool(confirms),
                "opposes": bool(opposes),
            }
        },
    }


def decide_v52_fast_1h(ctx: dict, v3: dict, mtf_ctxs: dict) -> dict:
    name = "v52_fast_1h"
    base_weight = float(v3.get("risk_weight") or 0)
    one_h = (mtf_ctxs or {}).get("1h") or {}
    summary = single_tf_summary("1h", one_h)
    available = summary["available_count"] == 1
    confirms = summary["confirm_count"] == 1
    opposes = summary["oppose_count"] == 1
    one_h_risk = summary["avg_risk_on"]
    risk_30m = ctx.get("risk_on") or 0
    notes = [
        f"base 30m conviction weight={base_weight:.2f}",
        f"Fast MTF 30m+1H: available={available}, confirms={confirms}, opposes={opposes}, 1H_risk={one_h_risk}",
    ]
    weight = base_weight
    status = "ready" if available else "pending"

    if not available:
        weight = step_weight(base_weight, -0.25) if base_weight > 0 else 0.0
        notes.append("1H context unavailable; V5.2 reduces because fast confirmation is missing")
        return convert_weight_policy(name, weight, notes, mtf_status=status, mtf_summary=summary)

    if base_weight > 0:
        if opposes:
            weight = step_weight(base_weight, -0.25)
            if base_weight <= 0.25 and risk_30m < 65:
                weight = 0.0
            notes.append("1H opposes the 30m signal; V5.2 cuts risk")
        elif confirms and (one_h_risk or 0) >= 65 and risk_30m >= 60:
            weight = step_weight(base_weight, 0.25)
            notes.append("1H confirms and risk is healthy; V5.2 upgrades one step")
        elif confirms:
            notes.append("1H confirms but not enough to upgrade; 30m+1H keeps base size")
        else:
            notes.append("1H is neutral; 30m+1H keeps base size without upgrade")
    else:
        if direction_confirms(ctx) and confirms and risk_30m >= 75 and (one_h_risk or 0) >= 65 and ctx.get("pulse") in {"PULSE_OK", "PULSE_STRONG"}:
            weight = 0.25
            notes.append("Fast recovery: 30m and 1H align strongly enough for a small probe")
        else:
            weight = 0.0
            notes.append("No base 30m edge and fast 1H filter does not justify recovery")
    return convert_weight_policy(name, weight, notes, mtf_status=status, mtf_summary=summary)


def decide_v6_regime_duo(ctx: dict, mtf_ctxs: dict) -> dict:
    name = "v6_regime_duo"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    score_30m = ctx.get("no_pulse_score")
    score_1h = one_h.get("no_pulse_score")
    confirms_30m = direction_confirms(ctx)
    confirms_1h = direction_confirms(one_h)
    opposes_1h = context_opposes(one_h)
    rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    notes = [
        f"Regime Alignment V6: 30m regime={ctx.get('regime')}, phase={ctx.get('phase')}, score={score_30m}, RSI={fmt_float(ctx.get('jrsx'))} {rsi_state}",
        f"1H Regime check: available={bool(one_h.get('available'))}, confirms={confirms_1h}, opposes={opposes_1h}, score={score_1h}",
        "Pulse and VTM/BTM ignored by design; Duo signal is still the trigger",
    ]
    if not ctx.get("available") or score_30m is None:
        notes.append("30m Regime data unavailable; V6 cannot judge the Duo signal")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP", confidence="none", approximation="regime_duo_no_pulse")

    weight = 0.0
    if confirms_30m and score_30m >= 72:
        weight = 0.5
        notes.append("30m Regime strongly confirms Duo direction")
    elif confirms_30m and score_30m >= 60:
        weight = 0.25
        notes.append("30m Regime confirms Duo direction but conviction is modest")
    elif score_30m >= 66 and not context_opposes(ctx):
        weight = 0.25
        notes.append("30m Regime is constructive but phase is not ideal; small probe only")
    else:
        notes.append("30m Regime does not provide enough conviction without Pulse")

    if weight > 0:
        if one_h.get("available"):
            if opposes_1h:
                weight = step_weight(weight, -0.25)
                notes.append("1H Regime opposes the Duo direction; V6 reduces one step")
            elif confirms_1h and (score_1h or 0) >= 65:
                weight = step_weight(weight, 0.25)
                notes.append("1H Regime confirms; V6 upgrades one step")
            else:
                notes.append("1H Regime is neutral; V6 keeps 30m size")
        else:
            weight = step_weight(weight, -0.25)
            notes.append("1H Regime unavailable; V6 reduces one step")

    if weight > 0 and rsi_state in {"RSI_OVERHEATED", "RSI_OVERSOLD_SHORT_CAUTION"}:
        weight = step_weight(weight, -0.25)
        notes.append("RSI is stretched against fresh entry; V6 reduces softly, not a hard skip")
    elif weight > 0 and rsi_state in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
        notes.append("RSI supports a possible turn; no extra size added until DuoBase update arrives")

    summary = single_tf_summary("1h", one_h)
    summary["no_pulse"] = True
    summary["score_30m"] = score_30m
    summary["score_1h"] = score_1h
    summary["rsi"] = ctx.get("jrsx")
    return convert_weight_policy(name, weight, notes, mtf_status="ready" if one_h.get("available") else "partial", mtf_summary=summary)


def regime_rsi_points(ctx: dict, direction: str, label: str, *, primary: bool) -> tuple[int, list[str]]:
    notes = []
    if not ctx.get("available"):
        return 0, [f"{label} unavailable"]
    score = 0
    confirms = direction_confirms(ctx)
    opposes = context_opposes(ctx)
    regime = str(ctx.get("regime") or "unknown")
    phase = str(ctx.get("phase") or "unknown")
    no_pulse = as_float(ctx.get("no_pulse_score"))
    if confirms:
        score += 3 if primary else 1
        notes.append(f"{label} Regime confirms Duo ({regime} {phase})")
    elif opposes:
        score -= 3 if primary else 1
        notes.append(f"{label} Regime opposes Duo ({regime} {phase})")
    else:
        score += 1 if primary and regime == "TRANSITION" else 0
        notes.append(f"{label} Regime is neutral/mixed ({regime} {phase})")
    if no_pulse is not None:
        if no_pulse >= 70:
            score += 2 if primary else 1
            notes.append(f"{label} Regime Alignment Score is strong ({fmt_float(no_pulse)})")
        elif no_pulse >= 58:
            score += 1 if primary else 0
            notes.append(f"{label} Regime Alignment Score is constructive ({fmt_float(no_pulse)})")
        elif no_pulse <= 42:
            score -= 2 if primary else 1
            notes.append(f"{label} Regime Alignment Score is weak ({fmt_float(no_pulse)})")
        else:
            notes.append(f"{label} Regime Alignment Score is middle/chop ({fmt_float(no_pulse)})")
    for value, strong, weak, metric in (
        (ctx.get("pp_acc"), 10, 2, "acceleration"),
        (ctx.get("pp_vel"), 5, 1, "velocity"),
    ):
        signed = as_float(value)
        if signed is None:
            notes.append(f"{label} {metric} unavailable")
            continue
        signed = signed if direction == "long" else -signed
        if signed >= strong:
            score += 2 if primary else 1
            notes.append(f"{label} {metric} strongly supports Duo ({fmt_float(value)})")
        elif signed >= weak:
            score += 1 if primary else 0
            notes.append(f"{label} {metric} supports Duo ({fmt_float(value)})")
        elif signed <= -strong:
            score -= 2 if primary else 1
            notes.append(f"{label} {metric} strongly opposes Duo ({fmt_float(value)})")
        elif signed <= -weak:
            score -= 1 if primary else 0
            notes.append(f"{label} {metric} opposes Duo ({fmt_float(value)})")
        else:
            notes.append(f"{label} {metric} neutral ({fmt_float(value)})")
    return score, notes


def rsi_quality_points(ctx: dict, direction: str, label: str, *, primary: bool) -> tuple[int, list[str]]:
    rsi = as_float(ctx.get("jrsx"))
    state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    if rsi is None:
        return 0, [f"{label} RSI unavailable"]
    score = 0
    notes = []
    if direction == "long":
        if rsi <= 25:
            score += 2 if primary else 1
            notes.append(f"{label} RSI recovery zone supports BUY ({fmt_float(rsi)})")
        elif 25 < rsi <= 62:
            score += 1 if primary else 0
            notes.append(f"{label} RSI is usable for BUY ({fmt_float(rsi)})")
        elif rsi >= 80:
            score -= 2 if primary else 1
            notes.append(f"{label} RSI is overheated for fresh BUY ({fmt_float(rsi)})")
        else:
            score -= 1 if primary else 0
            notes.append(f"{label} RSI is late/chasing for BUY ({fmt_float(rsi)})")
    else:
        if rsi >= 70:
            score += 2 if primary else 1
            notes.append(f"{label} RSI distribution zone supports SELL ({fmt_float(rsi)})")
        elif 38 <= rsi < 70:
            score += 1 if primary else 0
            notes.append(f"{label} RSI is usable for SELL ({fmt_float(rsi)})")
        elif rsi <= 20:
            score -= 2 if primary else 1
            notes.append(f"{label} RSI is oversold for fresh SELL ({fmt_float(rsi)})")
        else:
            score -= 1 if primary else 0
            notes.append(f"{label} RSI is late/chasing for SELL ({fmt_float(rsi)})")
    notes.append(f"{label} RSI state={state}")
    return score, notes


def decide_duo_regime_rsi_sized(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "duo_regime_rsi_sized"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "no_pulse_score": ctx.get("no_pulse_score"),
            "jrsx": ctx.get("jrsx"),
            "rsi_caution": ctx.get("rsi_caution"),
        },
        "1h": {
            "available": one_h.get("available"),
            "pp_acc": one_h.get("pp_acc"),
            "pp_vel": one_h.get("pp_vel"),
            "regime": one_h.get("regime"),
            "phase": one_h.get("phase"),
            "no_pulse_score": one_h.get("no_pulse_score"),
            "jrsx": one_h.get("jrsx"),
            "rsi_caution": one_h.get("rsi_caution"),
        },
        "no_pulse": True,
    }
    notes = [
        f"Duo {normalized['side'].upper()} is the trigger; Regime + RSI only size the new reverse entry",
        "Pulse, VTM/BTM and higher MTFs are ignored to reduce latency and indicator noise",
        "State machine rule: opposite Duo signal closes first; this score only decides the new entry size",
    ]
    if not ctx.get("available"):
        notes.append("30m Regime unavailable; close is allowed but no new position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)

    if context_opposes(ctx) and one_h.get("available") and context_opposes(one_h):
        rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
        if rsi_state not in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
            notes.append("Hard protection: 30m and 1H Regime both oppose Duo and RSI does not show a reversal zone")
            return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_REGIME_RSI_HARD_AGAINST", target_direction=target_direction, score=-5, mtf_summary=summary)

    score = 0
    delta, subnotes = regime_rsi_points(ctx, direction, "30m", primary=True)
    score += delta
    notes.extend(subnotes)
    delta, subnotes = rsi_quality_points(ctx, direction, "30m", primary=True)
    score += delta
    notes.extend(subnotes)
    if one_h.get("available"):
        delta, subnotes = regime_rsi_points(one_h, direction, "1H", primary=False)
        score += delta
        notes.extend(subnotes)
        delta, subnotes = rsi_quality_points(one_h, direction, "1H", primary=False)
        score += delta
        notes.extend(subnotes)
    else:
        notes.append("1H unavailable; policy continues from 30m only without penalty")

    if score >= 7:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 4:
        return policy_result(name, "REDUCE", 0.75, 1.0, notes, action="THREE_QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 2:
        return policy_result(name, "REDUCE", 0.5, 1.0, notes, action="HALF_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 0:
        return policy_result(name, "REDUCE_SMALL", 0.25, 1.0, notes, action="QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score below zero; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_REGIME_RSI", target_direction=target_direction, score=score, mtf_summary=summary)


def decide_duo_regime_rsi_30m(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "duo_regime_rsi_30m"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "no_pulse_score": ctx.get("no_pulse_score"),
            "jrsx": ctx.get("jrsx"),
            "rsi_caution": ctx.get("rsi_caution"),
        },
        "timeframes_used": ["30m"],
        "no_pulse": True,
    }
    notes = [
        f"Duo {normalized['side'].upper()} trigger; 30m Regime + RSI size only the new reverse entry",
        "Strict 30m-only policy: no Pulse, no VTM/BTM, no AVWAP, no 1H/2H/3H/4H",
        "Duo Risk-On is intentionally ignored because this local reader can derive it from Pulse inputs",
        "State machine rule: opposite Duo signal closes first; this score only decides the new entry size",
    ]
    if not ctx.get("available"):
        notes.append("30m Regime unavailable; close is allowed but no new position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)

    score = 0
    regime = str(ctx.get("regime") or "unknown")
    phase = str(ctx.get("phase") or "unknown")
    if direction_confirms(ctx):
        score += 1
        notes.append(f"30m Regime confirms Duo ({regime} {phase})")
    elif context_opposes(ctx):
        score -= 1
        notes.append(f"30m Regime opposes Duo ({regime} {phase})")
    else:
        notes.append(f"30m Regime is mixed/chop ({regime} {phase})")

    nps = as_float(ctx.get("no_pulse_score"))
    if nps is not None:
        if nps >= 70:
            score += 2
            notes.append(f"30m Regime Alignment Score strong ({fmt_float(nps)})")
        elif nps >= 52:
            score += 1
            notes.append(f"30m Regime Alignment Score constructive ({fmt_float(nps)})")
        elif nps <= 45:
            score -= 2
            notes.append(f"30m Regime Alignment Score weak/chop ({fmt_float(nps)})")
        else:
            notes.append(f"30m Regime Alignment Score middle ({fmt_float(nps)})")

    for value, strong, weak, label in (
        (ctx.get("pp_acc"), 10, 2, "30m acceleration"),
        (ctx.get("pp_vel"), 5, 1, "30m velocity"),
    ):
        raw = as_float(value)
        if raw is None:
            notes.append(f"{label} unavailable")
            continue
        signed = raw if direction == "long" else -raw
        if signed >= strong:
            score += 2
            notes.append(f"{label} strongly supports Duo ({fmt_float(raw)})")
        elif signed >= weak:
            score += 1
            notes.append(f"{label} supports Duo ({fmt_float(raw)})")
        elif signed <= -strong:
            score -= 2
            notes.append(f"{label} strongly opposes Duo ({fmt_float(raw)})")
        elif signed <= -weak:
            score -= 1
            notes.append(f"{label} opposes Duo ({fmt_float(raw)})")
        else:
            notes.append(f"{label} neutral ({fmt_float(raw)})")

    rsi = as_float(ctx.get("jrsx"))
    if rsi is None:
        notes.append("30m RSI unavailable")
    elif direction == "long":
        if rsi <= 25:
            score += 2
            notes.append(f"30m RSI recovery zone supports BUY ({fmt_float(rsi)})")
        elif rsi <= 68:
            score += 1
            notes.append(f"30m RSI usable for BUY ({fmt_float(rsi)})")
        elif rsi >= 80:
            score -= 2
            notes.append(f"30m RSI overheated for fresh BUY ({fmt_float(rsi)})")
        else:
            score -= 1
            notes.append(f"30m RSI late/chasing for BUY ({fmt_float(rsi)})")
    else:
        if rsi >= 70:
            score += 2
            notes.append(f"30m RSI distribution zone supports SELL ({fmt_float(rsi)})")
        elif rsi >= 32:
            score += 1
            notes.append(f"30m RSI usable for SELL ({fmt_float(rsi)})")
        elif rsi <= 20:
            score -= 2
            notes.append(f"30m RSI oversold for fresh SELL ({fmt_float(rsi)})")
        else:
            score -= 1
            notes.append(f"30m RSI late/chasing for SELL ({fmt_float(rsi)})")

    if score >= 4:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 0:
        return policy_result(name, "REDUCE_SMALL", 0.25, 1.0, notes, action="QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score below zero; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_30M_REGIME_RSI", target_direction=target_direction, score=score, mtf_summary=summary)


def metric_align_points(value, direction: str, *, strong: float, weak: float, label: str, points: int = 1) -> tuple[int, str]:
    value = as_float(value)
    if value is None:
        return 0, f"{label} unavailable"
    sign = 1 if direction == "long" else -1
    aligned = value * sign
    if aligned >= strong:
        return points, f"{label} strongly aligned ({fmt_float(value)})"
    if aligned >= weak:
        return max(1, points - 1), f"{label} aligned ({fmt_float(value)})"
    if aligned <= -strong:
        return -points, f"{label} strongly against ({fmt_float(value)})"
    if aligned <= -weak:
        return -max(1, points - 1), f"{label} against ({fmt_float(value)})"
    return 0, f"{label} neutral ({fmt_float(value)})"


def both_acc_vel_against(ctx: dict, direction: str) -> bool:
    acc = as_float(ctx.get("pp_acc"))
    vel = as_float(ctx.get("pp_vel"))
    if acc is None or vel is None:
        return False
    sign = 1 if direction == "long" else -1
    return (acc * sign) < -2 and (vel * sign) < -1


def decide_duo_conviction_sized(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "duo_conviction_sized"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    notes = [
        f"Duo {normalized['side'].upper()} is the trigger; this policy only sizes the reversal",
        "State machine rule: opposite signal closes first; size controls only the new reverse entry",
    ]
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "jrsx": ctx.get("jrsx"),
            "pulse": ctx.get("pulse"),
            "pulse_bkt": ctx.get("pulse_bkt"),
            "pulse_rs_mkt": ctx.get("pulse_rs_mkt"),
        },
        "1h": {
            "available": one_h.get("available"),
            "pp_acc": one_h.get("pp_acc"),
            "pp_vel": one_h.get("pp_vel"),
            "regime": one_h.get("regime"),
            "phase": one_h.get("phase"),
        },
    }

    if not ctx.get("available"):
        notes.append("30m MXC unavailable; closes are allowed by paper state, but no new position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)

    if both_acc_vel_against(ctx, direction) and both_acc_vel_against(one_h, direction):
        notes.append("Hard override: 30m and 1H acceleration plus velocity both oppose Duo direction")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_HARD_AGAINST", target_direction=target_direction, score=-3, mtf_summary=summary)

    score = 0
    for value, strong, weak, label, points in (
        (ctx.get("pp_acc"), 10, 2, "30m pp_acc", 2),
        (ctx.get("pp_vel"), 5, 1, "30m pp_vel", 2),
        (one_h.get("pp_acc"), 10, 2, "1H pp_acc", 1),
        (one_h.get("pp_vel"), 5, 1, "1H pp_vel", 1),
    ):
        delta, reason = metric_align_points(value, direction, strong=strong, weak=weak, label=label, points=points)
        score += delta
        notes.append(reason)

    rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    jrsx = as_float(ctx.get("jrsx"))
    if rsi_state in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
        score += 1
        notes.append(f"RSI quality supports a turn ({rsi_state}, JRSX={fmt_float(jrsx)})")
    elif rsi_state in {"RSI_OVERHEATED", "RSI_OVERSOLD_SHORT_CAUTION"}:
        score -= 1
        notes.append(f"RSI quality warns against fresh entry ({rsi_state}, JRSX={fmt_float(jrsx)})")
    else:
        notes.append(f"RSI quality neutral/unknown ({rsi_state}, JRSX={fmt_float(jrsx)})")

    pulse_bkt = as_float(ctx.get("pulse_bkt"))
    pulse_rs = as_float(ctx.get("pulse_rs_mkt"))
    pulse = ctx.get("pulse")
    if pulse in {"PULSE_OK", "PULSE_STRONG"}:
        score += 1
        notes.append(f"Low-weight Pulse confirmation ({pulse}, breakout={fmt_float(pulse_bkt)}, RS:Mkt={fmt_float(pulse_rs)})")
    elif pulse == "PULSE_WEAK":
        score -= 1
        notes.append(f"Low-weight Pulse late/chop caution ({pulse}, breakout={fmt_float(pulse_bkt)}, RS:Mkt={fmt_float(pulse_rs)})")
    else:
        notes.append(f"Low-weight Pulse neutral ({pulse}, breakout={fmt_float(pulse_bkt)}, RS:Mkt={fmt_float(pulse_rs)})")

    if score >= 5:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 3:
        return policy_result(name, "REDUCE", 0.5, 1.0, notes, action="HALF_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 1:
        return policy_result(name, "REDUCE_SMALL", 0.25, 1.0, notes, action="QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score <= 0; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_CONVICTION", target_direction=target_direction, score=score, mtf_summary=summary)


def decide_conviction_v2_candidate(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "conviction_v2_candidate"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    notes = [
        f"Duo {normalized['side'].upper()} trigger",
        "State machine rule: opposite signal closes first; score sizes only the new reverse entry",
        "V2 sizing: full >=3, 0.75x >=2, 0.40x >=0, skip <0",
    ]
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "jrsx": ctx.get("jrsx"),
            "rsi_caution": ctx.get("rsi_caution"),
            "pulse": ctx.get("pulse"),
            "pulse_bkt": ctx.get("pulse_bkt"),
            "pulse_rs_mkt": ctx.get("pulse_rs_mkt"),
        },
        "1h": {
            "available": one_h.get("available"),
            "pp_acc": one_h.get("pp_acc"),
            "pp_vel": one_h.get("pp_vel"),
            "regime": one_h.get("regime"),
            "phase": one_h.get("phase"),
        },
    }
    if not ctx.get("available"):
        notes.append("30m MXC unavailable; no new reverse position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)
    if both_acc_vel_against(ctx, direction) and both_acc_vel_against(one_h, direction):
        notes.append("Hard override: 30m and 1H acceleration plus velocity both oppose Duo direction")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_HARD_AGAINST", target_direction=target_direction, score=-3, mtf_summary=summary)

    score = 0
    for value, strong, weak, label, points in (
        (ctx.get("pp_acc"), 10, 2, "30m pp_acc", 2),
        (ctx.get("pp_vel"), 5, 1, "30m pp_vel", 2),
        (one_h.get("pp_acc"), 10, 2, "1H pp_acc", 1),
        (one_h.get("pp_vel"), 5, 1, "1H pp_vel", 1),
    ):
        delta, reason = metric_align_points(value, direction, strong=strong, weak=weak, label=label, points=points)
        score += delta
        notes.append(reason)

    rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    jrsx = as_float(ctx.get("jrsx"))
    if rsi_state in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
        score += 1
        notes.append(f"RSI supports turn ({rsi_state}, JRSX={fmt_float(jrsx)})")
    elif rsi_state in {"RSI_OVERHEATED", "RSI_OVERSOLD_SHORT_CAUTION"}:
        score -= 1
        notes.append(f"RSI warns against entry ({rsi_state}, JRSX={fmt_float(jrsx)})")
    else:
        notes.append(f"RSI neutral/unknown ({rsi_state}, JRSX={fmt_float(jrsx)})")

    pulse = ctx.get("pulse")
    if pulse in {"PULSE_OK", "PULSE_STRONG"}:
        score += 1
        notes.append(f"Low-weight Pulse confirms ({pulse})")
    elif pulse == "PULSE_WEAK":
        score -= 1
        notes.append(f"Low-weight Pulse caution ({pulse})")
    else:
        notes.append(f"Low-weight Pulse neutral ({pulse})")

    if score >= 3:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 2:
        return policy_result(name, "REDUCE", 0.75, 1.0, notes, action="THREE_QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 0:
        return policy_result(name, "REDUCE_SMALL", 0.40, 1.0, notes, action="FORTY_PERCENT_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score < 0; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_CONVICTION", target_direction=target_direction, score=score, mtf_summary=summary)


def fmt_float(value):
    try:
        if value is None:
            return "-"
        return f"{float(value):.2f}"
    except Exception:
        return str(value)


def decide_v5_family(ctx: dict, v3: dict, mtf_ctxs: dict, balanced: bool) -> dict:
    name = "v51_balanced" if balanced else "v5_mtf"
    base_weight = float(v3.get("risk_weight") or 0)
    summary = mtf_summary(mtf_ctxs, ctx.get("direction", "long"))
    available = summary["available_count"]
    confirms = summary["confirm_count"]
    opposes = summary["oppose_count"]
    avg_risk = summary["avg_risk_on"]
    status = "ready" if available >= 3 else ("partial" if available else "pending")
    notes = [
        f"base 30m conviction weight={base_weight:.2f}",
        f"MTF {status}: {confirms}/{available} confirm, {opposes} oppose, avg_risk={avg_risk}",
    ]
    if available == 0:
        notes.append("No MTF context available on VPS")
        return convert_weight_policy(name, base_weight if balanced else step_weight(base_weight, -0.25), notes, mtf_status="pending", mtf_summary=summary)

    weight = base_weight
    if balanced:
        if base_weight == 0 and confirms >= 3 and (avg_risk or 0) >= 65 and opposes == 0:
            weight = 0.25
            notes.append("V5.1 recovery: strong higher-timeframe agreement allows small probe")
        elif base_weight > 0 and confirms >= 2 and opposes <= 1:
            if (avg_risk or 0) >= 70 and confirms >= 3:
                weight = step_weight(base_weight, 0.25)
                notes.append("V5.1 upgrade: broad MTF support")
            else:
                notes.append("V5.1 keeps base weight: enough MTF support")
        elif opposes >= 2:
            weight = step_weight(base_weight, -0.25)
            notes.append("V5.1 caution: multiple higher timeframes oppose")
        else:
            notes.append("V5.1 neutral: MTF does not add edge")
    else:
        if base_weight > 0 and confirms >= 3 and opposes == 0 and (avg_risk or 0) >= 60:
            weight = step_weight(base_weight, 0.25)
            notes.append("Strict V5 upgrade: 3+ higher timeframes confirm")
        elif base_weight > 0 and confirms >= 2 and opposes <= 1:
            notes.append("Strict V5 keeps base weight: partial MTF confirmation")
        elif base_weight == 0 and confirms >= 4 and (avg_risk or 0) >= 70 and opposes == 0:
            weight = 0.25
            notes.append("Strict V5 rare recovery: all MTF confirms")
        else:
            weight = step_weight(base_weight, -0.25)
            notes.append("Strict V5 reduces/skips: insufficient MTF confirmation")
    return convert_weight_policy(name, weight, notes, mtf_status=status, mtf_summary=summary)


def latest_tab_health() -> dict | None:
    if not TAB_HEALTH_LEDGER.exists():
        return None
    try:
        lines = [line for line in TAB_HEALTH_LEDGER.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def health_gate_for(symbol: str, health: dict | None, mxc_values: dict | None, mtf_values: dict | None) -> dict:
    symbol = str(symbol or "").upper()
    result = {
        "status": "unknown",
        "primary_30m_ok": bool(mxc_values and mxc_values.get("pp_acc") is not None and mxc_values.get("pp_vel") is not None),
        "mtf_ok": False,
        "mtf_available_count": 0,
        "failed_timeframes": [],
        "source": "live_read",
        "message": "Health derived from current read",
    }
    mtf_values = mtf_values or {}
    for tf in ACTIVE_MTF_TIMEFRAMES:
        vals = mtf_values.get(tf) or {}
        ok = vals.get("pp_acc") is not None and vals.get("pp_vel") is not None
        if ok:
            result["mtf_available_count"] += 1
        else:
            result["failed_timeframes"].append(tf)
    result["mtf_ok"] = result["mtf_available_count"] == len(ACTIVE_MTF_TIMEFRAMES)
    if health:
        summary = (health.get("summary") or {}).get(symbol)
        if summary:
            result["source"] = "tab_health_and_live_read"
            # Never allow stale health to override a failed current 30m read.
            result["primary_30m_ok"] = result["primary_30m_ok"] and bool(summary.get("primary_30m_ok", True))
            summary_failed = {tf for tf in (summary.get("failed_timeframes") or []) if tf in ACTIVE_MTF_TIMEFRAMES}
            result["failed_timeframes"] = sorted(set(result["failed_timeframes"]) | summary_failed)
            result["mtf_ok"] = result["mtf_ok"] and not summary_failed
            if summary_failed:
                result["mtf_available_count"] = max(0, len(ACTIVE_MTF_TIMEFRAMES) - len(summary_failed))
    if isinstance(mxc_values, dict) and mxc_values.get("_fallback_source") == "tab_health_cache":
        result["source"] = "tab_health_cache_after_live_retry"
        result["cache_fallback"] = True
        result["cache_age_seconds"] = mxc_values.get("_fallback_age_seconds")
    if not result["primary_30m_ok"]:
        result["status"] = "primary_30m_blocked"
        result["message"] = "30m MXC is unavailable; all policies blocked for this alert"
    elif not result["mtf_ok"]:
        result["status"] = "mtf_degraded"
        result["message"] = "30m MXC is available but higher timeframe context is partial"
    else:
        result["status"] = "healthy"
        if result.get("cache_fallback"):
            result["message"] = "30m live read was incomplete; used recent tab-health snapshot"
        elif ACTIVE_MTF_TIMEFRAMES:
            result["message"] = "30m and MTF MXC context available"
        else:
            result["message"] = "30m MXC context available; MTF disabled by selected policies"
    return result


def apply_health_gate(policies: dict, gate: dict) -> dict:
    status = (gate or {}).get("status")
    if status == "primary_30m_blocked":
        for key, policy in policies.items():
            if key == "duo_raw":
                policy["health_status"] = "signal_only_no_cdp"
                policy.setdefault("reasons", []).insert(0, "Health gate: 30m MXC unavailable, but Duo raw uses the TradingView alert itself")
                continue
            policy["decision"] = "SKIP"
            policy["risk_weight"] = 0
            policy["leverage"] = 0
            policy["action"] = "BLOCKED_BY_HEALTH"
            policy["health_status"] = status
            policy.setdefault("reasons", []).insert(0, "Health gate: 30m Regime unavailable, decision blocked")
        return policies
    if status == "mtf_degraded":
        failed = set((gate or {}).get("failed_timeframes") or [])
        for key in ("v5_mtf", "v51_balanced"):
            policy = policies.get(key)
            if not policy:
                continue
            current = float(policy.get("risk_weight") or 0)
            degraded = step_weight(current, -0.25)
            policy["risk_weight"] = degraded
            if degraded <= 0:
                policy["decision"] = "SKIP"
                policy["leverage"] = 0
                policy["action"] = "SKIP_MTF_DEGRADED"
            else:
                policy["decision"] = "REDUCE"
                policy["leverage"] = 1.0
                policy["action"] = "PARTIAL_ENTRY_MTF_DEGRADED"
            policy["mtf_status"] = "degraded"
            policy["health_status"] = status
            policy.setdefault("reasons", []).insert(0, "Health gate: full MTF degraded, V5/V5.1 reduced")
        fast = policies.get("v52_fast_1h")
        if fast:
            if "1h" in failed:
                current = float(fast.get("risk_weight") or 0)
                degraded = step_weight(current, -0.25)
                fast["risk_weight"] = degraded
                if degraded <= 0:
                    fast["decision"] = "SKIP"
                    fast["leverage"] = 0
                    fast["action"] = "SKIP_1H_DEGRADED"
                else:
                    fast["decision"] = "REDUCE"
                    fast["leverage"] = 1.0
                    fast["action"] = "PARTIAL_ENTRY_1H_DEGRADED"
                fast["mtf_status"] = "degraded"
                fast["health_status"] = status
                fast.setdefault("reasons", []).insert(0, "Health gate: 1H unavailable, V5.2 reduced")
            else:
                fast["health_status"] = "healthy_fast_1h"
                fast.setdefault("reasons", []).insert(0, "Health gate: full MTF partial, but 1H is available for V5.2")
    else:
        for policy in policies.values():
            policy["health_status"] = status or "unknown"
    return policies


def build_policies(normalized: dict, values: dict | None, mtf_values: dict | None = None) -> dict:
    ctx = base_context(normalized, values)
    mtf_ctxs = mtf_contexts(normalized, mtf_values)
    out = {}
    if "duo_raw" in POLICY_KEYS:
        out["duo_raw"] = decide_duo_raw(normalized, ctx)
    needs_v3 = any(key in POLICY_KEYS for key in ("v52_fast_1h", "v5_mtf", "v51_balanced"))
    v3 = decide_v3_r75(ctx) if needs_v3 else None
    if "v52_fast_1h" in POLICY_KEYS:
        out["v52_fast_1h"] = decide_v52_fast_1h(ctx, v3, mtf_ctxs)
    if "v6_regime_duo" in POLICY_KEYS:
        out["v6_regime_duo"] = decide_v6_regime_duo(ctx, mtf_ctxs)
    if "duo_conviction_sized" in POLICY_KEYS:
        out["duo_conviction_sized"] = decide_duo_conviction_sized(normalized, ctx, mtf_ctxs)
    if "conviction_v2_candidate" in POLICY_KEYS:
        out["conviction_v2_candidate"] = decide_conviction_v2_candidate(normalized, ctx, mtf_ctxs)
    if "duo_regime_rsi_sized" in POLICY_KEYS:
        out["duo_regime_rsi_sized"] = decide_duo_regime_rsi_sized(normalized, ctx, mtf_ctxs)
    if "duo_regime_rsi_30m" in POLICY_KEYS:
        out["duo_regime_rsi_30m"] = decide_duo_regime_rsi_30m(normalized, ctx, mtf_ctxs)
    return out


def load_paper_state() -> dict:
    empty = {"version": 3, "updated_at": None, "policies": {}, "realistic_policies": {}, "compound_policies": {}}
    if not PAPER_STATE_FILE.exists():
        return empty
    try:
        state = json.loads(PAPER_STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("version", 3)
        state.setdefault("policies", {})
        state.setdefault("realistic_policies", {})
        state.setdefault("compound_policies", {})
        return state
    except Exception:
        return empty


def save_paper_state(state: dict) -> None:
    tmp = PAPER_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PAPER_STATE_FILE)

def default_control_state() -> dict:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "mode": "shadow_only",
        "live_trading": "paused",
        "manual_pause": False,
        "pause_reason": "",
        "allowed_assets": list(ALLOWED_SYMBOLS),
        "allowed_policies": list(POLICY_KEYS),
        "risk_limits": {
            "max_daily_loss_usd": float(CONFIG.get("risk", {}).get("max_daily_loss_usd", 150.0)),
            "max_slippage_pct": float(CONFIG.get("risk", {}).get("max_slippage_pct", 0.25)),
        },
        "notes": "Shadow control file. Dashboard/Hermes may read this. Live execution remains disabled here.",
    }


def save_control_state(state: dict) -> None:
    tmp = CONTROL_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONTROL_STATE_FILE)


def load_control_state() -> dict:
    if not CONTROL_STATE_FILE.exists():
        state = default_control_state()
        save_control_state(state)
        return state
    try:
        state = json.loads(CONTROL_STATE_FILE.read_text(encoding="utf-8"))
        default = default_control_state()
        merged = default | state
        merged["risk_limits"] = default.get("risk_limits", {}) | state.get("risk_limits", {})
        return merged
    except Exception:
        return default_control_state()


def realistic_base_notional_usd(symbol: str) -> float:
    asset = (CONFIG.get("assets") or {}).get(symbol) or {}
    budget = float(asset.get("budget_usd") or PAPER_BASE_NOTIONAL_USD)
    leverage = float(asset.get("leverage") or 1.0)
    return budget * leverage


def asset_budget_usd(symbol: str) -> float:
    asset = (CONFIG.get("assets") or {}).get(symbol) or {}
    return float(asset.get("budget_usd") or PAPER_BASE_NOTIONAL_USD)


def asset_leverage(symbol: str) -> float:
    asset = (CONFIG.get("assets") or {}).get(symbol) or {}
    return float(asset.get("leverage") or 1.0)


def refresh_compound_stats(ps: dict) -> None:
    equity = ps.get("equity") or {}
    initial = ps.get("initial_equity") or {}
    total_equity = sum(float(v or 0.0) for v in equity.values())
    total_initial = sum(float(v or 0.0) for v in initial.values())
    stats = ps.setdefault("stats", empty_policy_stats())
    stats["current_equity_usd"] = round(total_equity, 4)
    stats["initial_equity_usd"] = round(total_initial, 4)
    stats["equity_change_usd"] = round(total_equity - total_initial, 4)
    stats["equity_change_pct"] = round(((total_equity / total_initial) - 1.0) * 100.0, 6) if total_initial else 0.0


def empty_policy_stats() -> dict:
    return {
        "realized_pnl_usd": 0.0,
        "realized_gross_pnl_usd": 0.0,
        "realized_net_pnl_usd": 0.0,
        "realized_pnl_pct_weighted": 0.0,
        "realized_gross_pnl_pct_weighted": 0.0,
        "realized_net_pnl_pct_weighted": 0.0,
        "total_fees_usd": 0.0,
        "total_funding_usd": 0.0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "skips": 0,
        "entries": 0,
    }


def side_to_position(side: str) -> str:
    return "long" if side == "buy" else "short"


def pnl_pct(position_side: str, entry_price: float, exit_price: float) -> float:
    if not entry_price or not exit_price:
        return 0.0
    if position_side == "long":
        return (exit_price / entry_price - 1.0) * 100.0
    return (entry_price / exit_price - 1.0) * 100.0


def fee_rate_for(liquidity: str | None = None) -> float:
    liq = (liquidity or PAPER_DEFAULT_LIQUIDITY or "taker").lower()
    return OKX_PERP_MAKER_FEE_RATE if liq == "maker" else OKX_PERP_TAKER_FEE_RATE


def fee_usd(notional_usd: float, liquidity: str | None = None) -> float:
    return float(notional_usd or 0.0) * fee_rate_for(liquidity)


def funding_rate_from_payload(payload: dict | None = None) -> float:
    payload = payload or {}
    rate = as_float(first(payload, "funding_rate", "okx_funding_rate", default=None))
    if rate is None:
        rate = PAPER_DEFAULT_FUNDING_RATE
    return float(rate or 0.0)


def estimate_funding_usd(position: dict, payload: dict | None = None) -> float:
    # Placeholder until OKX funding timestamps are wired. Default is 0.
    if not PAPER_FUNDING_ENABLED:
        return 0.0
    rate = funding_rate_from_payload(payload)
    notional = float((position or {}).get("notional_usd") or 0.0)
    side = (position or {}).get("side")
    # Positive funding rate usually means longs pay shorts.
    sign = -1.0 if side == "long" else 1.0
    return notional * rate * sign


def executed_price_from_signal(signal_price: float, payload: dict | None = None) -> tuple[float, float]:
    payload = payload or {}
    execution_price = as_float(first(payload, "okx_execution_price", "execution_price", "fill_price", "avg_fill_price", default=None))
    if execution_price is None:
        execution_price = float(signal_price)
    diff_pct = 0.0 if not signal_price else (float(execution_price) / float(signal_price) - 1.0) * 100.0
    return float(execution_price), round(diff_pct, 6)


def paper_apply_policy(state: dict, normalized: dict, payload: dict, policy_key: str, policy: dict, price: float, received_at: str, base_notional_usd: float, account_key: str, account_label: str, *, compound: bool = False) -> dict:
    policies_state = state.setdefault(account_key, {})
    ps = policies_state.setdefault(policy_key, {"label": POLICY_LABELS.get(policy_key, policy_key), "symbols": {}, "stats": empty_policy_stats()})
    ps.setdefault("stats", empty_policy_stats())
    sym = normalized["symbol"]
    if compound:
        initial = ps.setdefault("initial_equity", {})
        equity = ps.setdefault("equity", {})
        initial.setdefault(sym, asset_budget_usd(sym))
        equity.setdefault(sym, initial[sym])
        base_notional_usd = max(0.0, float(equity.get(sym) or 0.0)) * asset_leverage(sym)
    symbols = ps.setdefault("symbols", {})
    pos = symbols.get(sym)
    target_side = side_to_position(normalized["side"])
    weight = float(policy.get("risk_weight") or 0.0)
    decision = str(policy.get("decision") or "SKIP").upper()
    event = {
        "received_at": received_at,
        "paper_account": account_key,
        "paper_account_label": account_label,
        "base_notional_usd": base_notional_usd,
        "policy": policy_key,
        "policy_label": POLICY_LABELS.get(policy_key, policy_key),
        "symbol": sym,
        "signal_side": normalized["side"],
        "signal_price": price,
        "tv_time": normalized.get("tv_time"),
        "chart_type": normalized.get("chart_type"),
        "okx_mark_price": normalized.get("okx_mark_price"),
        "okx_last_price": normalized.get("okx_last_price"),
        "okx_execution_price": None,
        "alert_execution_diff_pct": None,
        "decision": decision,
        "risk_weight": weight,
        "actions": [],
        "realized_pnl_pct": 0.0,
        "realized_pnl_usd": 0.0,
    }
    stats = ps["stats"]
    exec_price, alert_diff_pct = executed_price_from_signal(price, payload)
    event["okx_execution_price"] = exec_price
    event["alert_execution_diff_pct"] = alert_diff_pct
    liquidity = str(first(payload or {}, "liquidity", "execution_liquidity", default=PAPER_DEFAULT_LIQUIDITY)).lower()
    entry_fee_rate = fee_rate_for(liquidity)
    if price is None:
        stats["skips"] = int(stats.get("skips", 0)) + 1
        event["actions"].append("SKIP_NO_NEW_ENTRY")
        return event

    if (decision == "SKIP" or weight <= 0) and not (pos and pos.get("side") != target_side):
        stats["skips"] = int(stats.get("skips", 0)) + 1
        event["actions"].append("SKIP_NO_NEW_ENTRY")
        return event

    if pos and pos.get("side") == target_side:
        # Duo should not duplicate often, but keep the state stable if it happens.
        old_weight = float(pos.get("weight") or 0)
        pos["weight"] = weight
        pos["last_signal_at"] = received_at
        pos["last_signal_price"] = price
        pos["last_execution_price"] = exec_price
        event["actions"].append(f"UPDATE_SAME_DIRECTION {old_weight:.2f}->{weight:.2f}")
        return event

    if pos:
        entry_price = float(pos.get("entry_execution_price") or pos.get("entry_price") or 0)
        qty_units = float(pos.get("qty_units") or 0)
        exit_notional = qty_units * exec_price
        pct = pnl_pct(pos.get("side"), entry_price, exec_price)
        weighted_pct = pct * float(pos.get("weight") or 0)
        gross_usd = float(pos.get("notional_usd") or 0) * pct / 100.0
        entry_fee = float(pos.get("entry_fee_usd") or 0)
        exit_fee = fee_usd(exit_notional, liquidity)
        total_trade_fees = entry_fee + exit_fee
        funding_usd = estimate_funding_usd(pos, payload)
        net_usd = gross_usd - total_trade_fees + funding_usd
        net_weighted_pct = (net_usd / base_notional_usd) * 100.0 if base_notional_usd else 0.0
        trade = {
            "closed_at": received_at,
            "paper_account": account_key,
            "paper_account_label": account_label,
            "base_notional_usd": base_notional_usd,
            "policy": policy_key,
            "policy_label": POLICY_LABELS.get(policy_key, policy_key),
            "symbol": sym,
            "side": pos.get("side"),
            "entry_price": pos.get("entry_price"),
            "entry_execution_price": pos.get("entry_execution_price"),
            "exit_price": price,
            "exit_execution_price": exec_price,
            "alert_execution_diff_pct": alert_diff_pct,
            "entry_at": pos.get("entry_at"),
            "exit_tv_time": normalized.get("tv_time"),
            "weight": pos.get("weight"),
            "notional_usd": pos.get("notional_usd"),
            "pnl_pct": round(pct, 6),
            "weighted_pnl_pct": round(weighted_pct, 6),
            "net_weighted_pnl_pct": round(net_weighted_pct, 6),
            "gross_pnl_usd": round(gross_usd, 4),
            "entry_fee_usd": round(entry_fee, 4),
            "exit_fee_usd": round(exit_fee, 4),
            "total_fees_usd": round(total_trade_fees, 4),
            "funding_usd": round(funding_usd, 4),
            "chart_type": normalized.get("chart_type"),
            "okx_mark_price": normalized.get("okx_mark_price"),
            "okx_last_price": normalized.get("okx_last_price"),
            "pnl_usd": round(net_usd, 4),
            "net_pnl_usd": round(net_usd, 4),
            "fee_rate": fee_rate_for(liquidity),
            "liquidity": liquidity,
            "exit_signal_side": normalized["side"],
        }
        append_jsonl(PAPER_TRADES_LEDGER, trade)
        stats["closed_trades"] = int(stats.get("closed_trades", 0)) + 1
        stats["realized_gross_pnl_usd"] = round(float(stats.get("realized_gross_pnl_usd", 0)) + gross_usd, 4)
        stats["realized_net_pnl_usd"] = round(float(stats.get("realized_net_pnl_usd", 0)) + net_usd, 4)
        stats["realized_pnl_usd"] = stats["realized_net_pnl_usd"]
        stats["realized_gross_pnl_pct_weighted"] = round(float(stats.get("realized_gross_pnl_pct_weighted", 0)) + weighted_pct, 6)
        stats["realized_net_pnl_pct_weighted"] = round(float(stats.get("realized_net_pnl_pct_weighted", 0)) + net_weighted_pct, 6)
        stats["realized_pnl_pct_weighted"] = stats["realized_net_pnl_pct_weighted"]
        stats["total_fees_usd"] = round(float(stats.get("total_fees_usd", 0)) + exit_fee, 4)
        stats["total_funding_usd"] = round(float(stats.get("total_funding_usd", 0)) + funding_usd, 4)
        if compound:
            equity = ps.setdefault("equity", {})
            equity[sym] = round(float(equity.get(sym, asset_budget_usd(sym))) + net_usd, 4)
            refresh_compound_stats(ps)
        if net_usd > 0:
            stats["wins"] = int(stats.get("wins", 0)) + 1
        else:
            stats["losses"] = int(stats.get("losses", 0)) + 1
        event["actions"].append("CLOSE_" + str(pos.get("side", "")).upper())
        event["closed_trade"] = trade
        event["realized_pnl_pct"] = round(net_weighted_pct, 6)
        event["realized_pnl_usd"] = round(net_usd, 4)
        event["gross_pnl_usd"] = round(gross_usd, 4)
        event["fees_usd"] = round(total_trade_fees, 4)
        event["funding_usd"] = round(funding_usd, 4)
        symbols.pop(sym, None)

    if decision == "SKIP" or weight <= 0:
        stats["skips"] = int(stats.get("skips", 0)) + 1
        event["actions"].append("SKIP_NO_NEW_ENTRY")
        return event

    if compound:
        base_notional_usd = max(0.0, float(ps.setdefault("equity", {}).get(sym, asset_budget_usd(sym)))) * asset_leverage(sym)
        event["base_notional_usd"] = base_notional_usd
        event["equity_usd"] = round(float(ps.setdefault("equity", {}).get(sym, 0.0)), 4)
    notional = base_notional_usd * weight
    qty_units = notional / exec_price if exec_price else 0.0
    entry_fee = fee_usd(notional, liquidity)
    stats["total_fees_usd"] = round(float(stats.get("total_fees_usd", 0)) + entry_fee, 4)
    symbols[sym] = {
        "side": target_side,
        "entry_price": price,
        "entry_execution_price": exec_price,
        "alert_execution_diff_pct": alert_diff_pct,
        "chart_type": normalized.get("chart_type"),
        "okx_mark_price_at_entry": normalized.get("okx_mark_price"),
        "okx_last_price_at_entry": normalized.get("okx_last_price"),
        "entry_at": received_at,
        "entry_tv_time": normalized.get("tv_time"),
        "entry_signal_side": normalized.get("side"),
        "weight": weight,
        "base_notional_usd": base_notional_usd,
        "notional_usd": notional,
        "qty_units": qty_units,
        "entry_fee_usd": entry_fee,
        "entry_fee_rate": entry_fee_rate,
        "liquidity": liquidity,
        "source_decision": decision,
        "mtf_status": policy.get("mtf_status"),
        "equity_usd": round(float((ps.get("equity") or {}).get(sym, 0.0)), 4) if compound else None,
    }
    stats["entries"] = int(stats.get("entries", 0)) + 1
    if compound:
        refresh_compound_stats(ps)
    event["entry_fee_usd"] = round(entry_fee, 4)
    event["actions"].append("OPEN_" + target_side.upper())
    return event


def apply_paper_trading(record: dict) -> list[dict]:
    normalized = record.get("normalized") or {}
    price = normalized.get("tv_signal_price")
    if price is None:
        return []
    state = load_paper_state()
    events = []
    realistic_base = realistic_base_notional_usd(normalized.get("symbol"))
    for key in POLICY_KEYS:
        policy = (record.get("policies") or {}).get(key) or {}
        events.append(paper_apply_policy(state, normalized, record.get("payload") or {}, key, policy, float(price), record["received_at"], PAPER_BASE_NOTIONAL_USD, "policies", "Research $10k fixed"))
        events.append(paper_apply_policy(state, normalized, record.get("payload") or {}, key, policy, float(price), record["received_at"], realistic_base, "realistic_policies", "Fixed budget x leverage"))
        events.append(paper_apply_policy(state, normalized, record.get("payload") or {}, key, policy, float(price), record["received_at"], realistic_base, "compound_policies", "Compounding paper", compound=True))
    state["updated_at"] = record["received_at"]
    save_paper_state(state)
    return events


def find_policy_event(events: list[dict], policy_key: str, account_key: str = "realistic_policies") -> dict | None:
    for event in events or []:
        if event.get("paper_account") == account_key and event.get("policy") == policy_key:
            return event
    return None


def build_okx_execution_readiness(record: dict, paper_events: list[dict]) -> dict:
    """Prepare the future OKX order intent without sending any exchange order."""
    normalized = record.get("normalized") or {}
    execution_cfg = CONFIG.get("execution", {}) or {}
    risk_cfg = CONFIG.get("risk", {}) or {}
    execution_policy = str(execution_cfg.get("execution_policy") or "duo_raw")
    shadow_policy = str(execution_cfg.get("shadow_policy") or "duo_regime_rsi_30m")
    execution_event = find_policy_event(paper_events, execution_policy)
    shadow_event = find_policy_event(paper_events, shadow_policy)
    asset_cfg = CONFIG.get("assets", {}).get(normalized.get("symbol"), {}) or {}
    live_allowed = bool(execution_cfg.get("enabled")) and bool(risk_cfg.get("allow_live_execution"))
    signal_id = (normalized.get("signal_id") or "")[:16]
    client_order_id = f"mxc-{normalized.get('symbol','')}-{normalized.get('side','')}-{signal_id}".lower()
    weight = float((execution_event or {}).get("risk_weight") or 0.0)
    base_notional = float((execution_event or {}).get("base_notional_usd") or realistic_base_notional_usd(normalized.get("symbol")))
    planned_notional = round(base_notional * weight, 6)
    plan = {
        "mode": "live_order_enabled" if live_allowed else "dry_run_no_order",
        "live_execution_enabled": live_allowed,
        "execution_policy": execution_policy,
        "execution_policy_label": POLICY_LABELS.get(execution_policy, execution_policy),
        "shadow_policy": shadow_policy,
        "shadow_policy_label": POLICY_LABELS.get(shadow_policy, shadow_policy),
        "exchange": execution_cfg.get("exchange", "okx"),
        "route": execution_cfg.get("route", "okx_api"),
        "account": execution_cfg.get("account", "sandbox"),
        "symbol": normalized.get("symbol"),
        "okx_inst_id": asset_cfg.get("okx_inst_id"),
        "expected_leverage": asset_cfg.get("leverage"),
        "td_mode": execution_cfg.get("td_mode", "cross"),
        "timeframe": normalized.get("timeframe"),
        "tv_time": normalized.get("tv_time"),
        "signal_side": normalized.get("side"),
        "signal_price": normalized.get("tv_signal_price"),
        "execution_intent": {
            "policy": execution_policy,
            "decision": (execution_event or {}).get("decision"),
            "risk_weight": weight,
            "actions": (execution_event or {}).get("actions", []),
            "base_notional_usd": base_notional,
            "planned_notional_usd": planned_notional,
            "paper_execution_price": (execution_event or {}).get("okx_execution_price"),
            "alert_execution_diff_pct": (execution_event or {}).get("alert_execution_diff_pct"),
            "client_order_id": client_order_id,
        },
        "shadow_comparison": {
            "policy": shadow_policy,
            "decision": (shadow_event or {}).get("decision"),
            "risk_weight": (shadow_event or {}).get("risk_weight"),
            "actions": (shadow_event or {}).get("actions", []),
            "paper_execution_price": (shadow_event or {}).get("okx_execution_price"),
            "realized_pnl_usd": (shadow_event or {}).get("realized_pnl_usd"),
        },
        "okx_fill": {
            "status": "not_sent_shadow" if not live_allowed else "ready_to_send_when_executor_enabled",
            "order_id": None,
            "client_order_id": client_order_id,
            "avg_fill_price": None,
            "filled_size": None,
            "fee_usd": None,
            "slippage_pct": None,
            "position_after_order": None,
        },
        "block_reason": None if live_allowed else "OKX execution disabled: dry-run shadow only",
    }
    append_jsonl(EXECUTION_PLAN_LEDGER, {"received_at": record.get("received_at"), "execution_readiness": plan})
    return plan


def build_strategy_execution_readiness(record: dict) -> dict:
    normalized = record.get("normalized") or {}
    strategy = record.get("strategy_config") or {}
    execution_cfg = CONFIG.get("execution", {}) or {}
    risk_cfg = CONFIG.get("risk", {}) or {}
    strategy_submit = bool(STRATEGY_ENGINE.get("submit_orders")) and bool(strategy.get("okx_submit_orders"))
    live_allowed = (
        strategy_submit
        and bool(execution_cfg.get("enabled"))
        and bool(execution_cfg.get("submit_orders"))
        and bool(risk_cfg.get("allow_live_execution"))
    )
    direction = "long" if normalized.get("side") == "buy" else "short"
    signal_id = (normalized.get("signal_id") or "")[:16]
    client_order_id = f"mxc-{normalized.get('strategy_id','')}-{normalized.get('side','')}-{signal_id}".lower()[:64]
    base_notional = float(strategy.get("budget_usd") or 0.0) * float(strategy.get("leverage") or 1.0)
    plan = {
        "mode": "strategy_file_live_order_enabled" if live_allowed else "strategy_file_trial_no_order",
        "live_execution_enabled": live_allowed,
        "execution_policy": f"strategy_file:{normalized.get('strategy_id')}",
        "execution_policy_label": strategy.get("name") or normalized.get("strategy_id"),
        "exchange": execution_cfg.get("exchange", "okx"),
        "route": execution_cfg.get("route", "okx_api"),
        "account": execution_cfg.get("account", "sandbox"),
        "symbol": normalized.get("symbol"),
        "okx_inst_id": strategy.get("okx_inst_id"),
        "expected_leverage": strategy.get("leverage"),
        "td_mode": strategy.get("margin_mode", execution_cfg.get("td_mode", "isolated")),
        "timeframe": normalized.get("timeframe"),
        "tv_time": normalized.get("tv_time"),
        "signal_side": normalized.get("side"),
        "signal_price": normalized.get("tv_signal_price"),
        "execution_intent": {
            "policy": f"strategy_file:{normalized.get('strategy_id')}",
            "decision": "TRADE",
            "risk_weight": 1.0,
            "target_direction": direction,
            "actions": ["CLOSE_OPPOSITE_IF_ANY", f"OPEN_{direction.upper()}"],
            "base_notional_usd": float(strategy.get("budget_usd") or 0.0),
            "planned_notional_usd": round(base_notional, 6),
            "client_order_id": client_order_id,
        },
        "okx_fill": {
            "status": "not_sent_strategy_trial" if not live_allowed else "ready_to_send_when_strategy_promoted",
            "order_id": None,
            "client_order_id": client_order_id,
            "avg_fill_price": None,
            "filled_size": None,
            "fee_usd": None,
            "slippage_pct": None,
            "position_after_order": None,
        },
        "block_reason": None if live_allowed else "Duo Base Dev strategy trial is not approved for OKX submission",
    }
    append_jsonl(EXECUTION_PLAN_LEDGER, {"received_at": record.get("received_at"), "execution_readiness": plan})
    return plan


def submit_kill_switch_armed() -> tuple[bool, str]:
    """Global hard kill switch for OKX order submission.

    ``HERMX_SUBMIT_ENABLED`` gates ALL order submission regardless of config:
      - unset      -> inert; preserve existing config-driven behavior (armed).
      - falsey      -> hard-block submission ("false", "0", "no", "" / blank).
      - anything else -> armed (config gates still apply downstream).

    Returns ``(armed, raw_value)``. ``armed`` False means submission must be
    refused before any subprocess is spawned. The unset default is inert so the
    switch's absence cannot arm submission (Phase 0 rollback note).
    """
    raw = os.environ.get("HERMX_SUBMIT_ENABLED")
    if raw is None:
        return True, "<unset>"
    if raw.strip().lower() in {"false", "0", "no", ""}:
        return False, raw
    return True, raw


def execute_okx_if_enabled(record: dict) -> dict:
    armed, kill_switch_raw = submit_kill_switch_armed()
    if not armed:
        result = {
            "ok": True,
            "mode": "not_submitted",
            "reason": "HERMX_SUBMIT_ENABLED kill switch engaged",
            "kill_switch": kill_switch_raw,
        }
        append_jsonl(EXECUTION_LEDGER, {"received_at": record.get("received_at"), "okx_execution": result})
        return result
    readiness = record.get("execution_readiness") or {}
    execution_cfg = CONFIG.get("execution", {}) or {}
    risk_cfg = CONFIG.get("risk", {}) or {}
    should_execute = (
        bool(readiness.get("live_execution_enabled"))
        and bool(execution_cfg.get("enabled"))
        and bool(execution_cfg.get("submit_orders"))
        and bool(risk_cfg.get("allow_live_execution"))
    )
    if not should_execute:
        result = {
            "ok": True,
            "mode": "not_submitted",
            "reason": readiness.get("block_reason") or "OKX execution disabled",
        }
        append_jsonl(EXECUTION_LEDGER, {"received_at": record.get("received_at"), "okx_execution": result})
        return result
    script = ROOT / "src" / "okx_demo_executor.py"
    env = os.environ.copy()
    env["OKX_SIMULATED_TRADING"] = "1" if bool(execution_cfg.get("simulated_trading", True)) else "0"
    env["OKX_FORCE_IPV4"] = "1" if bool(execution_cfg.get("force_ipv4", True)) else "0"
    env["OKX_SUBMIT_ORDERS"] = "true"
    started = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "execute"],
            input=json.dumps(readiness, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=45,
            env=env,
        )
        elapsed_ms = round((time.time() - started) * 1000)
        if completed.returncode != 0:
            result = {
                "ok": False,
                "mode": "submit_failed",
                "elapsed_ms": elapsed_ms,
                "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
                "stdout": completed.stdout[-2000:],
            }
        else:
            payload = json.loads(completed.stdout)
            result = {
                "ok": True,
                "mode": payload.get("mode"),
                "elapsed_ms": elapsed_ms,
                "payload": payload,
            }
            if isinstance(readiness.get("okx_fill"), dict):
                readiness["okx_fill"].update(payload.get("okx_fill_summary") or {})
    except Exception as exc:
        result = {
            "ok": False,
            "mode": "submit_exception",
            "elapsed_ms": round((time.time() - started) * 1000),
            "error": str(exc),
        }
    append_jsonl(EXECUTION_LEDGER, {"received_at": record.get("received_at"), "okx_execution": result})
    return result


def primary_decision_from_policies(policies: dict) -> dict:
    if PRIMARY_POLICY_SELECTED and PRIMARY_POLICY in policies:
        return policies[PRIMARY_POLICY]
    # Compatibility fallback for the legacy top-level decision field. The actual
    # dashboard and ledger keep every policy side by side while primary is unset.
    return policies.get("duo_raw") or policies.get("v52_fast_1h") or policies.get("v6_regime_duo") or {}

def build_record(payload: dict, received_at_override: str | None = None) -> tuple[int, dict]:
    normalized = normalize(payload)
    if normalized["side"] not in ALLOWED_SIDES:
        return 400, {"ok": False, "error": "side_not_allowed", "normalized": normalized}
    if normalized.get("source") != "tradingview":
        return 202, {"ok": True, "ignored": True, "reason": "non_tradingview_source", "normalized": normalized}

    received_at = received_at_override or now_iso()
    strategy_ok, strategy_config, strategy_error = validate_strategy_alert(normalized)
    if not strategy_ok:
        record = {
            "received_at": received_at,
            "mode": "strategy_alert_quarantine",
            "ok": True,
            "quarantined": True,
            "reason": strategy_error,
            "payload": payload,
            "normalized": normalized,
            "strategy_config": strategy_config,
        }
        append_jsonl(STRATEGY_QUARANTINE_LEDGER, record)
        append_jsonl(WEBHOOK_LEDGER, {"received_at": received_at, "payload": payload, "normalized": normalized, "quarantined": True, "reason": strategy_error})
        return 202, record
    if strategy_config is None and normalized["symbol"] not in ALLOWED_SYMBOLS:
        return 400, {"ok": False, "error": "symbol_not_allowed", "normalized": normalized}

    duplicate, dedupe = check_and_mark_signal(normalized, received_at)
    latency = latency_info(normalized.get("tv_time"), received_at)
    if duplicate:
        record = {
            "received_at": received_at,
            "mode": "strategy_file_trial" if strategy_config else "vps_parallel_shadow",
            "config_snapshot": {"mode": CONFIG.get("mode"), "primary_policy": PRIMARY_POLICY, "base_notional_usd": PAPER_BASE_NOTIONAL_USD, "fees": CONFIG.get("fees"), "funding": CONFIG.get("funding"), "asset": CONFIG.get("assets", {}).get(normalized.get("symbol"))},
            "ok": True,
            "duplicate": True,
            "dedupe": dedupe,
            "latency": latency,
            "payload": payload,
            "normalized": normalized,
            "strategy_config": strategy_config,
        }
        append_jsonl(WEBHOOK_LEDGER, {"received_at": received_at, "payload": payload, "normalized": normalized, "duplicate": True})
        append_jsonl(DUPLICATE_LEDGER, record)
        if strategy_config:
            append_jsonl(STRATEGY_ALERT_LEDGER, record)
        return 200, record

    if strategy_config is not None:
        direction = "long" if normalized.get("side") == "buy" else "short"
        decision = {
            "policy": f"strategy_file:{normalized.get('strategy_id')}",
            "decision": "TRADE",
            "action": "TRADE",
            "risk_weight": 1.0,
            "target_direction": direction,
            "score": None,
            "reasons": [
                "strategy_id matched strategy file",
                "Duo Base Dev alert is in trial mode",
                "OKX order submission is disabled until explicit promotion",
            ],
        }
        record = {
            "received_at": received_at,
            "mode": "strategy_file_trial",
            "config_snapshot": {
                "mode": CONFIG.get("mode"),
                "strategy_engine": STRATEGY_ENGINE,
                "strategy": {
                    "strategy_id": strategy_config.get("strategy_id"),
                    "asset": strategy_config.get("asset"),
                    "timeframe": strategy_config.get("timeframe"),
                    "upper_band_mult": strategy_config.get("upper_band_mult"),
                    "lower_band_mult": strategy_config.get("lower_band_mult"),
                    "budget_usd": strategy_config.get("budget_usd"),
                    "leverage": strategy_config.get("leverage"),
                    "margin_mode": strategy_config.get("margin_mode"),
                    "okx_submit_orders": strategy_config.get("okx_submit_orders"),
                    "status": strategy_config.get("status"),
                },
            },
            "ok": True,
            "payload": payload,
            "normalized": normalized,
            "duplicate": False,
            "dedupe": dedupe,
            "latency": latency,
            "market_context": {
                "chart_type": strategy_config.get("chart_type") or normalized.get("chart_type"),
                "funding_enabled": PAPER_FUNDING_ENABLED,
                "funding_rate": funding_rate_from_payload(payload),
            },
            "strategy_config": strategy_config,
            "strategy_decision": decision,
            "decision": decision,
            "policies": {},
            "paper_events": [],
        }
        record["execution_readiness"] = build_strategy_execution_readiness(record)
        record["okx_execution"] = execute_okx_if_enabled(record)
        append_jsonl(WEBHOOK_LEDGER, {"received_at": record["received_at"], "payload": payload, "normalized": normalized, "strategy_id": normalized.get("strategy_id")})
        append_jsonl(STRATEGY_ALERT_LEDGER, record)
        append_jsonl(DECISION_LEDGER, record)
        LATEST_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return 200, record

    health = latest_tab_health()
    mxc_values = None
    mxc_error = None
    if read_indicator_values is not None:
        mxc_values, mxc_error = read_indicator_values_stable(normalized["symbol"], normalized["timeframe"])
        if not mxc_core_ok(mxc_values):
            cached = cached_health_values(health, normalized["symbol"], normalized["timeframe"])
            if cached:
                mxc_values = cached
                mxc_error = f"live_read_incomplete_used_health_cache:{mxc_error}"
            else:
                trigger_health_repair("primary_live_read_incomplete", normalized["symbol"], normalized["timeframe"])

    mtf_values = {}
    mtf_errors = {}
    if read_indicator_values is not None:
        mtf_values, mtf_errors = read_mtf_values(normalized["symbol"])
        for tf in ACTIVE_MTF_TIMEFRAMES:
            if not mxc_core_ok(mtf_values.get(tf)):
                cached = cached_health_values(health, normalized["symbol"], tf)
                if cached:
                    mtf_values[tf] = cached
                    mtf_errors[tf] = f"live_read_incomplete_used_health_cache:{mtf_errors.get(tf)}"

    health_gate = health_gate_for(normalized["symbol"], health, mxc_values, mtf_values)
    policies = build_policies(normalized, mxc_values, mtf_values)
    policies = apply_health_gate(policies, health_gate)
    decision = primary_decision_from_policies(policies)
    record = {
        "received_at": received_at,
        "mode": "vps_parallel_shadow",
        "config_snapshot": {"mode": CONFIG.get("mode"), "primary_policy": PRIMARY_POLICY, "base_notional_usd": PAPER_BASE_NOTIONAL_USD, "fees": CONFIG.get("fees"), "funding": CONFIG.get("funding"), "asset": CONFIG.get("assets", {}).get(normalized.get("symbol"))},
        "ok": True,
        "payload": payload,
        "normalized": normalized,
        "duplicate": False,
        "dedupe": dedupe,
        "latency": latency,
        "market_context": {"chart_type": normalized.get("chart_type"), "okx_mark_price": normalized.get("okx_mark_price"), "okx_last_price": normalized.get("okx_last_price"), "funding_enabled": PAPER_FUNDING_ENABLED, "funding_rate": funding_rate_from_payload(payload)},
        "mxc_values": mxc_values,
        "mxc_error": mxc_error,
        "mtf_values": mtf_values,
        "mtf_errors": mtf_errors,
        "health_gate": health_gate,
        "decision": decision,
        "policies": policies,
    }
    paper_events = apply_paper_trading(record)
    record["paper_events"] = paper_events
    record["execution_readiness"] = build_okx_execution_readiness(record, paper_events)
    record["okx_execution"] = execute_okx_if_enabled(record)
    append_jsonl(WEBHOOK_LEDGER, {"received_at": record["received_at"], "payload": payload, "normalized": normalized})
    append_jsonl(DECISION_LEDGER, record)
    LATEST_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return 200, record


def process_payload_async(payload: dict, intake_received_at: str) -> None:
    try:
        status, record = build_record(payload, intake_received_at)
        if status >= 400:
            append_jsonl(LOG_DIR / "shadow-processing-errors.jsonl", {"received_at": intake_received_at, "status": status, "record": record})
            logging.warning("Shadow async processing rejected status=%s error=%s", status, record.get("error"))
        else:
            normalized = record.get("normalized") or {}
            logging.info(
                "Shadow async processed symbol=%s side=%s tv_time=%s status=%s",
                normalized.get("symbol"),
                normalized.get("side"),
                normalized.get("tv_time"),
                status,
            )
    except Exception as exc:
        append_jsonl(LOG_DIR / "shadow-processing-errors.jsonl", {"received_at": intake_received_at, "error": str(exc), "payload": payload})
        logging.exception("Shadow async processing failed")


def worker_loop() -> None:
    while True:
        payload, intake_received_at = PROCESS_QUEUE.get()
        try:
            process_payload_async(payload, intake_received_at)
        finally:
            PROCESS_QUEUE.task_done()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/health", "/shadow/health"}:
            self._send(200, {"ok": True, "service": "mxc-vps-shadow-receiver", "port": PORT, "mode": "shadow_only", "latest": str(LATEST_FILE)})
        elif path in {"/latest", "/shadow/latest"}:
            if LATEST_FILE.exists():
                self._send(200, json.loads(LATEST_FILE.read_text(encoding="utf-8")))
            else:
                self._send(404, {"ok": False, "error": "no_latest_yet"})
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in {"/webhook", "/shadow/webhook"}:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        provided = self.headers.get("X-Webhook-Secret", "") or parse_qs(parsed.query).get("secret", [""])[0]
        if SECRET and provided != SECRET:
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as exc:
            self._send(400, {"ok": False, "error": "invalid_json", "detail": str(exc)})
            return
        intake_received_at = now_iso()
        append_jsonl(RAW_INTAKE_LEDGER, {"received_at": intake_received_at, "payload": payload, "path": parsed.path})
        PROCESS_QUEUE.put((payload, intake_received_at))
        self._send(200, {"ok": True, "status": "queued", "received_at": intake_received_at, "queue_depth": PROCESS_QUEUE.qsize()})

    def log_message(self, fmt, *args):
        return


def log_execution_arm_state() -> None:
    """Startup self-check: print the effective order-submission arm state.

    Surfaces every gate that controls live submission so the operator can see at
    a glance whether the bot is armed or inert. All gates must be affirmative for
    a real order to be sent (see execute_okx_if_enabled / gate-precedence).
    """
    execution_cfg = CONFIG.get("execution", {}) or {}
    risk_cfg = CONFIG.get("risk", {}) or {}
    armed, kill_switch_raw = submit_kill_switch_armed()
    logging.info(
        "EXECUTION ARM STATE: HERMX_SUBMIT_ENABLED=%s (kill_switch_armed=%s) "
        "execution.submit_orders=%s risk.allow_live_execution=%s "
        "execution.enabled=%s execution.simulated_trading=%s",
        kill_switch_raw,
        armed,
        execution_cfg.get("submit_orders"),
        risk_cfg.get("allow_live_execution"),
        execution_cfg.get("enabled"),
        execution_cfg.get("simulated_trading"),
    )


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    log_execution_arm_state()
    threading.Thread(target=worker_loop, daemon=True, name="shadow-policy-worker").start()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    logging.info("MXC VPS shadow receiver listening on 127.0.0.1:%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
