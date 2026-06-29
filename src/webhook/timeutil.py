"""Pure time/timestamp primitives (Option A leaf extraction).

This module holds the byte-for-byte time helpers that used to live in
webhook_receiver.py: the UTC now-stamp ``now_iso``, the tolerant TradingView
timestamp parser ``parse_tv_time`` (epoch seconds/millis or ISO-8601), the
``latency_info`` receipt-vs-signal latency calculator, and the ``_epoch_from_iso``
ISO-to-epoch coercion used by the signal dedupe index.

It is a TRUE leaf: it imports only ``datetime``/``timezone`` from the stdlib and
reads NO mutable global state. webhook_receiver re-exports every name here for
backward compatibility.
"""
from __future__ import annotations

from datetime import datetime, timezone


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


def _epoch_from_iso(ts: str | None) -> float | None:
    dt = parse_tv_time(ts)
    if dt is None:
        return None
    return dt.timestamp()
