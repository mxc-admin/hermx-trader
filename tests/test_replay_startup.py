"""Option A startup replay — re-queue intake webhooks accepted (HTTP 200) before a
restart but never dequeued from the in-memory PROCESS_QUEUE.

`replay_intake_webhooks()` is a COLD-PATH, best-effort recovery run once at boot
(before worker threads start). It:
  * reads raw-webhooks.jsonl, pairs ``intake`` rows against ``webhook`` rows by
    received_at (already-processed → skip);
  * drops rows older than the lookback window, or with no time field (Option A:
    no time field => normalize() would mint a non-deterministic signal_id), or
    whose tv_time is staler than the max-age window;
  * consults the dedupe index (signals.jsonl) — already-seen → skip;
  * re-queues the survivors and returns (replayed, skipped, dropped).

Deterministic and offline: a pinned clock is passed via ``now_seconds`` and all
ledger paths + the queue + the dedupe index are redirected to per-test state, so
nothing touches real runtime files and no exchange path can ever arm.
"""
from __future__ import annotations

import json
import queue
from datetime import datetime, timezone

import pytest

import webhook_receiver as wr

# Pinned wall clock for every test (epoch seconds). Nov 2023; value is arbitrary.
FIXED_NOW = 1_700_000_000.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _write_raw_webhooks(raw_path, rows) -> None:
    _write_jsonl(raw_path, rows)


def _write_signals_ledger(signals_path, rows) -> None:
    _write_jsonl(signals_path, rows)


def _eligible_payload(now: float, *, tv_offset: float = 10.0, **overrides) -> dict:
    """A payload normalize() accepts and that carries a fresh time field."""
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "timeframe": "30m",
        "strategy_id": "S1",
        "tv_time": _iso(now - tv_offset),
    }
    payload.update(overrides)
    return payload


def _intake_row(now: float, payload, *, rcv_offset: float = 10.0) -> dict:
    return {
        "phase": "intake",
        "received_at": _iso(now - rcv_offset),
        "payload": payload,
        "path": "/webhook",
    }


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    """Redirect ledgers + queue + dedupe index to isolated per-test state."""
    raw_path = tmp_path / "raw-webhooks.jsonl"
    signals_path = tmp_path / "signals.jsonl"
    monkeypatch.setattr(wr, "RAW_WEBHOOK_LEDGER", raw_path)
    monkeypatch.setattr(wr, "SIGNALS_LEDGER", signals_path)
    monkeypatch.setattr(wr, "PROCESS_QUEUE", queue.Queue(maxsize=200))
    monkeypatch.setattr(
        wr, "_SIGNAL_DEDUPE_INDEX", {"signals": {}, "keys": {}, "loaded": False}
    )
    # Defaults (in case a prior test or the environment shifted them).
    monkeypatch.setattr(wr, "HERMX_REPLAY_ENABLED", True)
    monkeypatch.setattr(wr, "REPLAY_LOOKBACK_SECONDS", 300.0)
    monkeypatch.setattr(wr, "REPLAY_MAX_TV_AGE_SECONDS", 120.0)

    class Env:
        now = FIXED_NOW
        raw = raw_path
        signals = signals_path

    return Env()


# ---------------------------------------------------------------------------
# 1. Empty journals.
# ---------------------------------------------------------------------------

def test_empty_journals(replay_env):
    assert wr.replay_intake_webhooks(now_seconds=replay_env.now) == (0, 0, 0)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 2. intake + matching webhook row (already processed) => skipped.
# ---------------------------------------------------------------------------

def test_intake_with_webhook_pair_skipped(replay_env):
    now = replay_env.now
    payload = _eligible_payload(now)
    intake = _intake_row(now, payload)
    webhook = {"phase": "webhook", "received_at": intake["received_at"], "payload": payload}
    _write_raw_webhooks(replay_env.raw, [intake, webhook])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 1, 0)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 3. lone fresh intake row, no dupe => replayed.
# ---------------------------------------------------------------------------

def test_intake_only_replayed(replay_env):
    now = replay_env.now
    _write_raw_webhooks(replay_env.raw, [_intake_row(now, _eligible_payload(now))])

    assert wr.replay_intake_webhooks(now_seconds=now) == (1, 0, 0)
    assert wr.PROCESS_QUEUE.qsize() == 1


# ---------------------------------------------------------------------------
# 4. signal already in signals.jsonl (within window) => skipped.
# ---------------------------------------------------------------------------

def test_intake_in_signals_skipped(replay_env):
    now = replay_env.now
    payload = _eligible_payload(now)
    norm = wr.normalize(payload)
    sid = norm["signal_id"]
    _write_raw_webhooks(replay_env.raw, [_intake_row(now, payload)])
    _write_signals_ledger(replay_env.signals, [{
        "ts": _iso(now - 10),
        "kind": "signal_dedupe",
        "signal_id": sid,
        "dedupe_key": wr.dedupe_key(norm),
        "first_seen_at": _iso(now - 10),
        "first_seen_epoch": now - 10,
        "symbol": norm["symbol"],
        "side": norm["side"],
        "timeframe": norm["timeframe"],
        "tv_time": norm["tv_time"],
    }])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 1, 0)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 5. stale tv_time (older than max-age) => dropped.
# ---------------------------------------------------------------------------

def test_stale_tv_time_dropped(replay_env):
    now = replay_env.now
    # received recently, but the bar time is well past the freshness window.
    payload = _eligible_payload(now, tv_offset=wr.REPLAY_MAX_TV_AGE_SECONDS + 60)
    _write_raw_webhooks(replay_env.raw, [_intake_row(now, payload)])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 0, 1)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 6. received_at older than lookback => skipped.
# ---------------------------------------------------------------------------

def test_old_received_at_skipped(replay_env):
    now = replay_env.now
    payload = _eligible_payload(now)  # tv_time fresh
    intake = _intake_row(now, payload, rcv_offset=wr.REPLAY_LOOKBACK_SECONDS + 60)
    _write_raw_webhooks(replay_env.raw, [intake])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 1, 0)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 7. queue full => dropped, no raise.
# ---------------------------------------------------------------------------

def test_queue_full_dropped(replay_env, monkeypatch):
    now = replay_env.now
    full_q = queue.Queue(maxsize=1)
    full_q.put_nowait(("dummy",))  # saturate
    monkeypatch.setattr(wr, "PROCESS_QUEUE", full_q)
    _write_raw_webhooks(replay_env.raw, [_intake_row(now, _eligible_payload(now))])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 0, 1)
    assert wr.PROCESS_QUEUE.qsize() == 1  # unchanged; nothing new enqueued


# ---------------------------------------------------------------------------
# 8. truncated trailing line tolerated; valid row still replays.
# ---------------------------------------------------------------------------

def test_corrupt_trailing_line_tolerated(replay_env):
    now = replay_env.now
    valid = _intake_row(now, _eligible_payload(now))
    replay_env.raw.parent.mkdir(parents=True, exist_ok=True)
    with replay_env.raw.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(valid) + "\n")
        fh.write('{"phase": "intake", "received')  # truncated, no newline

    replayed, skipped, dropped = wr.replay_intake_webhooks(now_seconds=now)
    assert replayed == 1
    assert dropped == 0
    assert wr.PROCESS_QUEUE.qsize() == 1


# ---------------------------------------------------------------------------
# 9. duplicate intake rows (same received_at) replay once.
# ---------------------------------------------------------------------------

def test_duplicate_intake_rows_once(replay_env):
    now = replay_env.now
    payload = _eligible_payload(now)
    row = _intake_row(now, payload)
    _write_raw_webhooks(replay_env.raw, [dict(row), dict(row)])

    assert wr.replay_intake_webhooks(now_seconds=now) == (1, 1, 0)
    assert wr.PROCESS_QUEUE.qsize() == 1


# ---------------------------------------------------------------------------
# 10. replay disabled => short-circuit (0, 0, 0).
# ---------------------------------------------------------------------------

def test_disabled_short_circuits(replay_env, monkeypatch):
    now = replay_env.now
    monkeypatch.setattr(wr, "HERMX_REPLAY_ENABLED", False)
    _write_raw_webhooks(replay_env.raw, [_intake_row(now, _eligible_payload(now))])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 0, 0)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 11. Option A: payload with no time field => dropped.
# ---------------------------------------------------------------------------

def test_missing_tv_time_dropped(replay_env):
    now = replay_env.now
    payload = _eligible_payload(now)
    payload.pop("tv_time")  # no tv_time/time/timestamp/bar_time/candle_time
    _write_raw_webhooks(replay_env.raw, [_intake_row(now, payload)])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 0, 1)
    assert wr.PROCESS_QUEUE.qsize() == 0


# ---------------------------------------------------------------------------
# 12. non-dict payload => skipped (defensive).
# ---------------------------------------------------------------------------

def test_non_dict_payload_skipped(replay_env):
    now = replay_env.now
    row = {"phase": "intake", "received_at": _iso(now - 10), "payload": "not-a-dict", "path": "/webhook"}
    _write_raw_webhooks(replay_env.raw, [row])

    assert wr.replay_intake_webhooks(now_seconds=now) == (0, 1, 0)
    assert wr.PROCESS_QUEUE.qsize() == 0
