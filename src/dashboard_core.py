#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os


ROOT = Path(os.environ.get("SHADOW_ROOT", Path(__file__).resolve().parents[1]))
LOGS = ROOT / "logs"
SYMBOLS = ["XRPUSDT", "SOLUSDT", "ETHUSDT", "BTCUSDT"]
OKX_TICKER_CACHE = {"ts": 0.0, "data": {}}

# Per-path stats for the most recent read of each ledger (Phase 4 / D1+D2). The
# dashboard model snapshots this after building so corrupt/skipped lines are
# *surfaced* rather than silently dropped.
LEDGER_READ_STATS: dict[str, dict] = {}

# Hard ceiling on how far back we scan from EOF, so a pathological huge or
# single-line ledger can never OOM the dashboard (D1).
_TAIL_SCAN_CAP_BYTES = 16 * 1024 * 1024
_TAIL_BLOCK_BYTES = 65536


def _read_tail_lines(path: Path, limit: int):
    """Return ``(lines, more)`` for roughly the last ``limit`` lines of ``path``.

    Reads backwards from EOF in blocks so memory stays bounded regardless of
    file size (D1). ``more`` is True when older content exists beyond what was
    returned (file larger than the returned window or the scan cap was hit). A
    partial leading line produced by stopping mid-line is dropped by the final
    ``[-limit:]`` slice.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        data = b""
        newlines = 0
        capped = False
        while pos > 0 and newlines <= limit:
            if len(data) >= _TAIL_SCAN_CAP_BYTES:
                capped = True
                break
            read_size = min(_TAIL_BLOCK_BYTES, pos)
            pos -= read_size
            handle.seek(pos)
            data = handle.read(read_size) + data
            newlines = data.count(b"\n")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    more = capped or pos > 0 or len(lines) > limit
    return lines[-limit:], more


def read_jsonl_stats(path: Path, limit: int = 200):
    """Bounded tail read returning ``(rows, stats)``.

    Corrupt lines are *counted* (``stats["skipped"]``) instead of being hidden.
    A single unparseable FINAL line is tolerated as a truncated in-flight append
    (``stats["truncated_tail"]``) rather than counted as corruption, since the
    writer fsyncs whole lines (Phase 1) and a torn tail is expected mid-write.
    """
    path = Path(path)
    stats = {
        "path": str(path),
        "exists": False,
        "read": 0,
        "skipped": 0,
        "truncated_tail": False,
        "more": False,
    }
    if not path.exists():
        LEDGER_READ_STATS[str(path)] = stats
        return [], stats
    stats["exists"] = True
    try:
        lines, more = _read_tail_lines(path, limit)
    except Exception:
        LEDGER_READ_STATS[str(path)] = stats
        return [], stats
    stats["more"] = more
    rows: list[dict] = []
    last = len(lines) - 1
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            if idx == last:
                stats["truncated_tail"] = True
            else:
                stats["skipped"] += 1
            continue
    stats["read"] = len(rows)
    LEDGER_READ_STATS[str(path)] = stats
    return rows, stats


def read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    rows, _stats = read_jsonl_stats(path, limit)
    return rows


def shadow_config() -> dict:
    # shadow-config.json was removed entirely. Execution config now comes from
    # engine-config.json + strategy files + CCXT; the dashboard reads ledgers, not
    # a config blob. Retained as a no-op so existing callers keep working.
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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    # Tolerate naive timestamps by assuming UTC (D6) — previously these returned
    # an aware/naive mix that rendered as "-" and broke age/freshness math.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def display_tz():
    """Resolve the dashboard display timezone (configurable, default UTC — D6).

    ``HERMX_DASH_TZ`` (IANA name, e.g. ``America/Bogota``) takes precedence; then
    ``HERMX_DASH_TZ_OFFSET_HOURS`` (signed float). Anything unset/invalid → UTC.
    Read on each call so the timezone is configurable without reimport.
    """
    name = (os.environ.get("HERMX_DASH_TZ") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    offset = (os.environ.get("HERMX_DASH_TZ_OFFSET_HOURS") or "").strip()
    if offset:
        try:
            return timezone(timedelta(hours=float(offset)))
        except Exception:
            pass
    return timezone.utc


def display_time(iso):
    dt = parse_dt(iso)
    if not dt:
        return "-"
    return dt.astimezone(display_tz()).strftime("%Y-%m-%d %H:%M")


# Backward-compatible shim: callers/imports still reference ``colombia_time`` but
# the timezone is now configurable (default UTC) rather than hardcoded -05:00.
def colombia_time(iso):
    return display_time(iso)


def asset_inst_id(config: dict, symbol: str) -> str:
    asset_cfg = ((config.get("assets") or {}).get(symbol) or {})
    configured = (asset_cfg.get("inst_id") or "").strip()
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
