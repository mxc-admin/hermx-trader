#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os


ROOT = Path(os.environ.get("SHADOW_ROOT", Path(__file__).resolve().parents[1]))
LOGS = ROOT / "logs"
CONFIG_FILE = ROOT / "shadow-config.json"
SYMBOLS = ["XRPUSDT", "SOLUSDT", "ETHUSDT", "BTCUSDT"]
OKX_TICKER_CACHE = {"ts": 0.0, "data": {}}


def read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def shadow_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    fallback = ROOT / "config" / "runtime.demo.json"
    if fallback.exists():
        try:
            return json.loads(fallback.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def as_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def colombia_time(iso):
    dt = parse_dt(iso)
    if not dt:
        return "-"
    return dt.astimezone(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d %H:%M")


def asset_inst_id(config: dict, symbol: str) -> str:
    configured = (((config.get("assets") or {}).get(symbol) or {}).get("okx_inst_id") or "").strip()
    if configured:
        return configured
    return {
        "XRPUSDT": "XRP-USDT-SWAP",
        "SOLUSDT": "SOL-USDT-SWAP",
        "ETHUSDT": "ETH-USDT-SWAP",
        "BTCUSDT": "BTC-USDT-SWAP",
    }.get(symbol, "")


def okx_swap_tickers() -> dict:
    import time

    now = time.time()
    if now - float(OKX_TICKER_CACHE.get("ts") or 0) < 10 and OKX_TICKER_CACHE.get("data"):
        return OKX_TICKER_CACHE["data"]
    try:
        req = urllib.request.Request(
            "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
            headers={"User-Agent": "kinetic-flow-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = {row.get("instId"): row for row in payload.get("data") or [] if row.get("instId")}
        OKX_TICKER_CACHE["ts"] = now
        OKX_TICKER_CACHE["data"] = data
        return data
    except Exception:
        return OKX_TICKER_CACHE.get("data") or {}


def merged_replay_state() -> dict:
    return {}


def build_combined_events(merged: dict, live_rows: list[dict]):
    live = []
    for record in live_rows:
        n = record.get("normalized") or {}
        live.append(
            {
                "time": n.get("tv_time") or record.get("received_at"),
                "received_at": record.get("received_at"),
                "time_colombia": colombia_time(n.get("tv_time") or record.get("received_at")),
                "symbol": n.get("symbol"),
                "side": str(n.get("side") or "").lower(),
                "ha_close": as_float(n.get("tv_signal_price") or n.get("price")),
                "source": "live",
                "payload": record.get("payload") or {},
                "ctx30": {},
                "ctx1h": {},
                "health_gate": record.get("health_gate") or {},
                "policies": record.get("policies") or {},
            }
        )
    return live, [], live


def normalize_policy_decision(event: dict, key: str) -> dict:
    policies = event.get("policies") or {}
    bundle = policies.get(key) or {}
    if isinstance(bundle, dict):
        decision = bundle.get("decision")
        if isinstance(decision, dict):
            bundle = decision
        return {
            "decision": bundle.get("decision") or "-",
            "risk_weight": as_float(bundle.get("risk_weight")) or 0.0,
            "action": bundle.get("action") or "-",
            "score": bundle.get("score"),
            "reasons": bundle.get("reasons") or [],
            "actions": bundle.get("actions") or [],
            "ctx30": event.get("ctx30") or {},
            "ctx1h": event.get("ctx1h") or {},
            "health": bundle.get("health_status") or "live",
        }
    return {
        "decision": "UNAVAILABLE",
        "risk_weight": 0.0,
        "action": "UNAVAILABLE",
        "score": None,
        "reasons": [],
        "actions": [],
        "ctx30": event.get("ctx30") or {},
        "ctx1h": event.get("ctx1h") or {},
        "health": "unavailable",
    }
