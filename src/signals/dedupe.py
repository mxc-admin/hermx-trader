"""Signal dedupe / idempotency (Phase 1 extraction, REFACTOR_PLAN.md).

Houses the business-idempotency cluster that used to live in
webhook_receiver.py: ``dedupe_key``, ``_signal_identity``,
``stable_client_order_id``, ``_dedupe_window_seconds``,
``_load_signal_dedupe_index`` and ``check_and_mark_signal``.

The root-bound / monkeypatchable module state these functions read STAYS
defined in webhook_receiver.py (``_SIGNAL_DEDUPE_INDEX``,
``_SIGNAL_DEDUPE_LOCK``, ``SIGNALS_LEDGER``,
``HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS``): tests monkeypatch/rebind them on
``wr`` (test_replay_startup rebinds the index and ledger path;
test_intake_hardening rebinds the window), and the per-test
``importlib.reload(webhook_receiver)`` in conftest must reset the in-memory
index to fresh ``{"loaded": False}`` state -- state defined here would survive
the reload and leak across tests. Each function therefore reads that state
lazily via ``import webhook_receiver as _wr`` -- the same pattern as
src/alerts.py (Phase 0). webhook_receiver re-exports these functions so
``wr.<fn>`` call sites and monkeypatch seams keep working.

The leaf-pure primitives are imported directly from their extracted homes.
"""
from __future__ import annotations

import hashlib
import time

from webhook.timeutil import _epoch_from_iso
from webhook.ledger_io import append_jsonl, read_jsonl_tolerant


def dedupe_key(normalized: dict) -> str:
    return "|".join(str(normalized.get(k, "")) for k in ("strategy_id", "symbol", "side", "timeframe", "tv_time"))


def _signal_identity(normalized: dict) -> str:
    return "|".join(
        str(normalized.get(k, ""))
        for k in ("strategy_id", "symbol", "side", "timeframe", "tv_time", "signal_id")
    )


def stable_client_order_id(identity: str, role: str = "base") -> str:
    digest = hashlib.sha256(f"{identity}|{role}".encode("utf-8")).hexdigest()
    return f"mxc{digest}"[:32]


def _dedupe_window_seconds() -> float:
    # BUSINESS idempotency window ONLY. Deliberately INDEPENDENT of the SECURITY replay
    # window (HERMX_REPLAY_WINDOW_SECONDS), which is enforced separately in HMAC
    # verification. Conflating them (the old max(..., replay)) let a long replay window
    # silently widen idempotency retention -- two unrelated concerns. Neither widens the
    # other now: freshness is the HMAC's job, idempotency is this ledger's job.
    import webhook_receiver as _wr
    return max(1.0, _wr.HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS)


def _load_signal_dedupe_index(now_seconds: float | None = None) -> None:
    import webhook_receiver as _wr
    if _wr._SIGNAL_DEDUPE_INDEX.get("loaded"):
        return
    now = time.time() if now_seconds is None else float(now_seconds)
    cutoff = now - _dedupe_window_seconds()
    signals: dict[str, dict] = {}
    keys: dict[str, dict] = {}
    for rec in read_jsonl_tolerant(_wr.SIGNALS_LEDGER):
        if not isinstance(rec, dict):
            continue
        first_seen_at = str(rec.get("first_seen_at") or rec.get("ts") or "")
        first_seen_epoch = rec.get("first_seen_epoch")
        if not isinstance(first_seen_epoch, (int, float)):
            first_seen_epoch = _epoch_from_iso(first_seen_at)
        if first_seen_epoch is None or first_seen_epoch < cutoff:
            continue
        entry = {
            "first_seen_at": first_seen_at,
            "first_seen_epoch": float(first_seen_epoch),
            "signal_id": str(rec.get("signal_id") or ""),
            "dedupe_key": str(rec.get("dedupe_key") or ""),
            "symbol": rec.get("symbol"),
            "side": rec.get("side"),
            "timeframe": rec.get("timeframe"),
            "tv_time": rec.get("tv_time"),
        }
        if entry["signal_id"]:
            signals[entry["signal_id"]] = entry
        if entry["dedupe_key"]:
            keys[entry["dedupe_key"]] = entry
    _wr._SIGNAL_DEDUPE_INDEX["signals"] = signals
    _wr._SIGNAL_DEDUPE_INDEX["keys"] = keys
    _wr._SIGNAL_DEDUPE_INDEX["loaded"] = True


def check_and_mark_signal(normalized: dict, received_at: str) -> tuple[bool, dict]:
    import webhook_receiver as _wr
    sid = str(normalized.get("signal_id") or "")
    key = dedupe_key(normalized)
    received_epoch = _epoch_from_iso(received_at) or time.time()
    cutoff = received_epoch - _dedupe_window_seconds()
    with _wr._SIGNAL_DEDUPE_LOCK:
        _load_signal_dedupe_index(now_seconds=received_epoch)
        signals = _wr._SIGNAL_DEDUPE_INDEX.setdefault("signals", {})
        keys = _wr._SIGNAL_DEDUPE_INDEX.setdefault("keys", {})
        for bucket in (signals, keys):
            stale_keys = [k for k, v in bucket.items() if float(v.get("first_seen_epoch") or 0.0) < cutoff]
            for stale in stale_keys:
                bucket.pop(stale, None)

        existing = None
        duplicate_by = None
        if sid and sid in signals:
            existing = signals[sid]
            duplicate_by = "signal_id"
        elif key in keys:
            existing = keys[key]
            duplicate_by = "symbol_side_timeframe_tv_time"

        meta = {
            "signal_id": sid,
            "dedupe_key": key,
            "duplicate_by": duplicate_by,
            "first_seen_at": (existing or {}).get("first_seen_at"),
            "window_seconds": _dedupe_window_seconds(),
        }
        if existing:
            return True, meta

        entry = {
            "first_seen_at": received_at,
            "first_seen_epoch": received_epoch,
            "signal_id": sid,
            "dedupe_key": key,
            "symbol": normalized.get("symbol"),
            "side": normalized.get("side"),
            "timeframe": normalized.get("timeframe"),
            "tv_time": normalized.get("tv_time"),
        }
        if sid:
            signals[sid] = entry
        keys[key] = entry
        append_jsonl(
            _wr.SIGNALS_LEDGER,
            {
                "ts": received_at,
                "kind": "signal_dedupe",
                **entry,
            },
        )
        meta["first_seen_at"] = received_at
        return False, meta
