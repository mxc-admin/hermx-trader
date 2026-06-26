"""Phase 1 task 7: journal lifecycle = verified checkpoint + segment rotation.

Covers REFACTOR_PLAN.md:218-221 and acceptance:
  * :239  checkpoint + journal-segment rotation keeps replay BOUNDED (startup
          replay no longer grows with full history) AND preserves exact state
          equivalence with a full from-empty replay.
  * :240  all existing tests still pass unchanged; legacy mode is untouched here
          (no checkpoint/segment files are ever created in legacy mode).
  * verify-before-trust: a corrupt (hash-mismatch) or forward-version checkpoint
          is DISCARDED and load falls back to full replay, still correct.
  * disk-full / write failure FAILS CLOSED: a journal append or checkpoint write
          that raises OSError surfaces an operator alert and re-raises so the
          money path is blocked; in-memory state is never silently advanced.

Determinism: each alert carries a UNIQUE tv_time (idx-keyed) so the dedupe layer
does not suppress it, and a fixed received_at, so signal_id / *_at fields are
stable. The same (n, start) drive sequence is byte-for-byte reproducible, which is
what lets a journal run (with checkpoints) be compared against a legacy run.
"""
from __future__ import annotations

import json

import pytest

from conftest import load_alert

BUY = "shadow/btcusdt_shadow_buy.json"
SELL = "shadow/btcusdt_shadow_sell.json"


def _payload(side: str, idx: int) -> dict:
    """A shadow buy/sell payload with a UNIQUE tv_time so it is not a duplicate.
    idx must stay in [0, 30] (one day per alert)."""
    p = dict(load_alert(BUY if side == "buy" else SELL))
    p["tv_time"] = f"2026-07-{idx + 1:02d}T00:00:00Z"
    return p


def _drive(module, n: int, start: int = 0) -> None:
    """Drive n alternating buy/sell alerts, each unique. Deterministic in
    (n, start) so two backends can be driven identically and compared."""
    for k in range(n):
        idx = start + k
        side = "buy" if idx % 2 == 0 else "sell"
        status, _ = module.build_record(_payload(side, idx), f"2026-07-{idx + 1:02d}T00:00:01Z")
        assert status == 200


def _txn_count(module, path) -> int:
    return sum(1 for r in module.read_jsonl_tolerant(path) if r.get("kind") == "transition")


def _duo(state, account="realistic_policies"):
    return state[account]["duo_raw"]


# ---------------------------------------------------------------------------
# :239 -- the bounded-replay proof: after checkpoint+rotation a load replays
# ONLY post-checkpoint records, AND equals a full from-empty replay.
# ---------------------------------------------------------------------------

def test_checkpoint_rotation_bounds_replay_and_preserves_state(reload_wr, tmp_path, monkeypatch):
    wr = reload_wr(tmp_path / "bound", backend="journal")

    # Build a non-trivial history (open/close/reverse cycles).
    _drive(wr, 4)
    total_before = _txn_count(wr, wr.POSITION_JOURNAL_LEDGER)
    assert total_before > 0

    # Force a verified checkpoint + rotation covering all of it.
    wr._checkpoint_and_rotate(wr.load_paper_state())
    assert wr.POSITION_JOURNAL_CHECKPOINT_FILE.exists()
    assert wr._sealed_segment_paths(), "live segment should have been sealed"
    # Live segment is fresh/empty; the history now lives in a sealed segment.
    assert wr.read_jsonl_tolerant(wr.POSITION_JOURNAL_LEDGER) == []

    # A small live tail accrues after the checkpoint.
    _drive(wr, 2, start=4)
    live_txn = _txn_count(wr, wr.POSITION_JOURNAL_LEDGER)
    assert 0 < live_txn < total_before

    # Spy on apply_effect to count replay work.
    orig = wr.apply_effect
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(wr, "apply_effect", spy)

    calls["n"] = 0
    bounded = wr.load_paper_state()         # checkpoint fast-path
    bounded_calls = calls["n"]

    calls["n"] = 0
    full = wr.replay_position_journal()     # full from-empty replay
    full_calls = calls["n"]

    # PROOF (bounded): the checkpoint load replayed ONLY the post-checkpoint live
    # tail -- not the full history -- so replay work no longer scales with history.
    assert bounded_calls == live_txn
    assert bounded_calls < full_calls
    assert full_calls >= total_before + live_txn

    # PROOF (equivalence): the bounded load is byte-for-byte the full replay.
    assert bounded == full
    assert _duo(bounded)["stats"]["closed_trades"] >= 1


# ---------------------------------------------------------------------------
# :239 -- auto-rotation when the live segment crosses the threshold, plus
# end-to-end equivalence against a legacy run of the identical sequence.
# ---------------------------------------------------------------------------

def test_auto_rotation_threshold_and_legacy_equivalence(reload_wr, tmp_path, monkeypatch):
    j = reload_wr(tmp_path / "auto-j", backend="journal")
    monkeypatch.setattr(j, "HERMX_JOURNAL_SEGMENT_MAX_RECORDS", 8)  # force early rotation
    _drive(j, 8)
    # The threshold tripped at least one checkpoint+rotation automatically.
    assert j.POSITION_JOURNAL_CHECKPOINT_FILE.exists()
    assert j._sealed_segment_paths()
    # Bounded: the live tail is smaller than the full history.
    live = _txn_count(j, j.POSITION_JOURNAL_LEDGER)
    sealed_total = sum(_txn_count(j, p) for _s, p in j._sealed_segment_paths())
    assert live < sealed_total + live
    journal_state = j.load_paper_state()

    # Legacy reference: identical alert sequence, no checkpoints/segments.
    legacy = reload_wr(tmp_path / "auto-l", backend="legacy")
    _drive(legacy, 8)
    legacy_state = legacy.load_paper_state()

    assert journal_state == legacy_state
    assert _duo(journal_state)["stats"]["closed_trades"] >= 1


# ---------------------------------------------------------------------------
# verify-before-trust -- corrupt checkpoint (hash mismatch) is discarded.
# ---------------------------------------------------------------------------

def test_corrupt_checkpoint_discarded_falls_back_to_full_replay(reload_wr, tmp_path):
    wr = reload_wr(tmp_path / "corrupt-ckpt", backend="journal")
    _drive(wr, 4)
    good = wr.load_paper_state()
    wr._checkpoint_and_rotate(good)
    assert wr.POSITION_JOURNAL_CHECKPOINT_FILE.exists()

    # Tamper the stored state so its recomputed hash no longer matches state_hash.
    ck = json.loads(wr.POSITION_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    ck["state"]["realistic_policies"]["duo_raw"]["stats"]["closed_trades"] = 999
    wr.POSITION_JOURNAL_CHECKPOINT_FILE.write_text(json.dumps(ck), encoding="utf-8")

    assert wr._read_checkpoint() is None  # discarded by verify-before-trust
    recovered = wr.load_paper_state()      # falls back to full replay
    assert recovered == good               # corrupt checkpoint never trusted


# ---------------------------------------------------------------------------
# verify-before-trust -- a checkpoint from a NEWER writer is discarded, not crashed.
# ---------------------------------------------------------------------------

def test_forward_version_checkpoint_discarded(reload_wr, tmp_path):
    wr = reload_wr(tmp_path / "fwd-ckpt", backend="journal")
    _drive(wr, 4)
    good = wr.load_paper_state()
    wr._checkpoint_and_rotate(good)

    ck = json.loads(wr.POSITION_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    ck["checkpoint_version"] = wr.POSITION_JOURNAL_CHECKPOINT_VERSION + 1  # hash stays valid
    wr.POSITION_JOURNAL_CHECKPOINT_FILE.write_text(json.dumps(ck), encoding="utf-8")

    assert wr._read_checkpoint() is None   # discarded loudly, not a crash
    recovered = wr.load_paper_state()
    assert recovered == good


# ---------------------------------------------------------------------------
# fail-closed -- a journal append OSError blocks the money path.
# ---------------------------------------------------------------------------

def test_disk_full_journal_append_fails_closed(reload_wr, tmp_path, monkeypatch):
    root = tmp_path / "diskfull-append"
    wr = reload_wr(root, backend="journal")

    def enospc(path, obj):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wr, "append_jsonl_durable", enospc)

    with pytest.raises(OSError):
        wr.build_record(_payload("buy", 0), "2026-07-01T00:00:01Z")

    # Operator-visible alert emitted...
    alerts = wr.read_jsonl_tolerant(wr.STATE_ALERT_LEDGER)
    assert any(a.get("alert") == "STATE_WRITE_FAILED" and a.get("operation") == "journal-append" for a in alerts)
    # ...and submission never ran: build_record aborted before execute / LATEST_FILE.
    assert not (root / "latest.json").exists()


# ---------------------------------------------------------------------------
# fail-closed -- a checkpoint write OSError blocks and does not lose state.
# ---------------------------------------------------------------------------

def test_disk_full_checkpoint_fails_closed(reload_wr, tmp_path, monkeypatch):
    wr = reload_wr(tmp_path / "diskfull-ckpt", backend="journal")
    _drive(wr, 4)
    state = wr.load_paper_state()

    def enospc(path, obj):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(wr, "_atomic_json_dump", enospc)

    with pytest.raises(OSError):
        wr._checkpoint_and_rotate(state)

    # No checkpoint, no rotation -> the live journal is intact and state recovers.
    assert not wr.POSITION_JOURNAL_CHECKPOINT_FILE.exists()
    assert wr._sealed_segment_paths() == []
    assert wr.replay_position_journal() == state
    alerts = wr.read_jsonl_tolerant(wr.STATE_ALERT_LEDGER)
    assert any(a.get("alert") == "STATE_WRITE_FAILED" and a.get("operation") == "checkpoint-rotate" for a in alerts)


# ---------------------------------------------------------------------------
# seq stays monotonic across rotation AND a restart (the rotation footgun).
# ---------------------------------------------------------------------------

def test_seq_monotonic_after_rotation_and_restart(reload_wr, tmp_path):
    root = tmp_path / "seq"
    wr = reload_wr(root, backend="journal")
    _drive(wr, 4)
    wr._checkpoint_and_rotate(wr.load_paper_state())
    last_seq = wr._read_checkpoint()["last_seq"]
    assert wr.read_jsonl_tolerant(wr.POSITION_JOURNAL_LEDGER) == []  # fresh live

    # Restart: fresh module (seq cache reset), same root, live segment empty.
    wr2 = reload_wr(root, backend="journal")
    _drive(wr2, 1, start=20)  # one more unique alert
    live = wr2.read_jsonl_tolerant(wr2.POSITION_JOURNAL_LEDGER)
    assert live, "new records must land in the fresh live segment"
    # Seq continues from the checkpoint floor -- no reset-to-0 collision.
    assert all(r["seq"] > last_seq for r in live)
    assert min(r["seq"] for r in live) == last_seq + 1


# ---------------------------------------------------------------------------
# retention -- old sealed segments are pruned; checkpoint still reconstructs.
# ---------------------------------------------------------------------------

def test_retention_prunes_sealed_segments_and_state_survives(reload_wr, tmp_path, monkeypatch):
    j = reload_wr(tmp_path / "retain-j", backend="journal")
    monkeypatch.setattr(j, "HERMX_JOURNAL_SEGMENT_RETENTION", 2)

    for batch in range(5):
        _drive(j, 2, start=batch * 4)
        j._checkpoint_and_rotate(j.load_paper_state())

    # Only the last K sealed segments are kept (checkpoint subsumes the rest).
    assert len(j._sealed_segment_paths()) == 2
    journal_state = j.load_paper_state()  # checkpoint path -- correct despite pruning

    # Same sequence under legacy authority for an end-to-end equivalence check.
    legacy = reload_wr(tmp_path / "retain-l", backend="legacy")
    for batch in range(5):
        _drive(legacy, 2, start=batch * 4)
    legacy_state = legacy.load_paper_state()

    assert journal_state == legacy_state
    assert _duo(journal_state)["stats"]["closed_trades"] >= 1
