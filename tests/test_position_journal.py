"""Phase 1 task 1/2: durable append-only position journal + replay.

Covers REFACTOR_PLAN.md acceptance:
  * :233  kill-9 / crash mid-trade -> open position recovered from the journal,
          subsequent close executes correctly (crash-recovery).
  * :234  truncated trailing journal line is quarantined; reader continues; no
          unhandled exception.
  * :240  ALL P0 characterization tests still pass UNCHANGED -> covered by the
          existing suite running in the default (legacy) backend. Here we add the
          explicit equivalence proof: for the corpus alert sequence, the
          journal-mode replay-derived state == the legacy-mode snapshot state.
  * E5    a missing/corrupt snapshot REBUILDS from the journal instead of
          silently resetting to empty.

Determinism: the shadow buy/sell pair carries fixed tv_time and we pass fixed
received_at values, so signal_id / *_at fields are stable (same basis the golden
round-trip relies on). The shadow path trades only `duo_raw`; MXC-gated policies
SKIP, which the journal records too (so replay reproduces stats.skips exactly).
"""
from __future__ import annotations

import json

from conftest import load_alert

BUY = "shadow/btcusdt_shadow_buy.json"
SELL = "shadow/btcusdt_shadow_sell.json"
BUY_AT = "2026-06-21T00:00:01Z"
SELL_AT = "2026-06-21T04:00:01Z"

# The corpus alert sequence used for the equivalence proof: open (buy) then
# close+reverse (sell), exercised across all three paper accounts and both
# policies (one trades, one skips), plus the compound equity path.
SEQUENCE = [(BUY, BUY_AT), (SELL, SELL_AT)]


def _duo(state, account="realistic_policies"):
    return state[account]["duo_raw"]


def _run(module, sequence):
    for alert, at in sequence:
        status, _ = module.build_record(load_alert(alert), at)
        assert status == 200


# ---------------------------------------------------------------------------
# :240 equivalence -- replay (journal authority) == snapshot (legacy authority)
# ---------------------------------------------------------------------------

def test_replay_equivalence_journal_equals_legacy(reload_wr, tmp_path):
    legacy = reload_wr(tmp_path / "legacy", backend="legacy")
    _run(legacy, SEQUENCE)
    legacy_state = legacy.load_paper_state()  # authoritative snapshot

    journal = reload_wr(tmp_path / "journal", backend="journal")
    _run(journal, SEQUENCE)
    replayed_state = journal.load_paper_state()  # authoritative replay (from empty)

    # Deep equality across the entire state tree: positions, per-policy stats,
    # compound equity/initial_equity, and updated_at. This is the proof that the
    # single shared apply_effect routine makes replay byte-for-byte the live run.
    assert replayed_state == legacy_state
    # Sanity: the sequence actually produced non-trivial state to compare.
    assert _duo(legacy_state)["stats"]["closed_trades"] == 1
    assert _duo(legacy_state)["symbols"]["BTCUSDT"]["side"] == "short"


# ---------------------------------------------------------------------------
# :233 crash recovery -- snapshot gone, state recovered from journal
# ---------------------------------------------------------------------------

def test_crash_recovery_rebuilds_open_position_and_close_executes(reload_wr, tmp_path):
    root = tmp_path / "crash"
    wr = reload_wr(root, backend="journal")

    # Open a long, then capture what the journal says state should be.
    _run(wr, [(BUY, BUY_AT)])
    expected = wr.load_paper_state()
    assert _duo(expected)["symbols"]["BTCUSDT"]["side"] == "long"
    assert _duo(expected)["stats"]["entries"] == 1

    # Simulate kill -9 mid-trade: the snapshot cache is lost entirely.
    (root / "paper-state.json").unlink()

    # Restart: reload the module against the same root (journal still on disk).
    wr2 = reload_wr(root, backend="journal")
    recovered = wr2.load_paper_state()
    assert recovered == expected  # identical recovery from the journal alone
    assert _duo(recovered)["symbols"]["BTCUSDT"]["side"] == "long"

    # A subsequent close executes correctly against the recovered position.
    status, sell_rec = wr2.build_record(load_alert(SELL), SELL_AT)
    assert status == 200
    sell_duo = next(
        e for e in sell_rec["paper_events"]
        if e["paper_account"] == "realistic_policies" and e["policy"] == "duo_raw"
    )
    assert any(a.startswith("CLOSE_LONG") for a in sell_duo["actions"])
    assert sell_duo["realized_pnl_usd"] > 0

    final = wr2.load_paper_state()
    assert _duo(final)["stats"]["closed_trades"] == 1
    assert _duo(final)["stats"]["wins"] == 1
    # reversal short is now open
    assert _duo(final)["symbols"]["BTCUSDT"]["side"] == "short"


# ---------------------------------------------------------------------------
# E5 -- corrupt snapshot must rebuild from journal, NOT reset to empty
# ---------------------------------------------------------------------------

def test_corrupt_snapshot_rebuilds_not_empty(reload_wr, tmp_path):
    root = tmp_path / "corrupt-snap"
    wr = reload_wr(root, backend="journal")
    _run(wr, [(BUY, BUY_AT)])
    expected = wr.load_paper_state()

    # Corrupt (not delete) the snapshot the way a partial write would.
    (root / "paper-state.json").write_text("{ this is not valid json", encoding="utf-8")

    wr2 = reload_wr(root, backend="journal")
    recovered = wr2.load_paper_state()
    # The E5 footgun would have returned {} positions here. Journal mode must not.
    assert recovered == expected
    assert _duo(recovered)["symbols"]["BTCUSDT"]["side"] == "long"


# ---------------------------------------------------------------------------
# :234 truncated trailing journal line is quarantined; reader continues
# ---------------------------------------------------------------------------

def test_truncated_trailing_line_quarantined(reload_wr, tmp_path):
    root = tmp_path / "trunc"
    wr = reload_wr(root, backend="journal")
    _run(wr, [(BUY, BUY_AT)])
    good = wr.load_paper_state()
    assert _duo(good)["symbols"]["BTCUSDT"]["side"] == "long"

    journal = root / "logs" / "position-journal.jsonl"
    # Append a half-written record (simulates a crash mid-fsync of the next line).
    with journal.open("a", encoding="utf-8") as f:
        f.write('{"schema_version":1,"seq":999,"kind":"transi')

    # Reader tolerates the truncated tail: no exception, valid records returned.
    recs = wr.read_jsonl_tolerant(journal)
    assert recs and all(isinstance(r, dict) for r in recs)
    assert all(r.get("seq") != 999 for r in recs)  # the partial record is dropped
    assert (root / "logs" / "position-journal.jsonl.corrupt").exists()

    # Replay still rebuilds the open position despite the truncated tail.
    state = wr.load_paper_state()
    assert state == good
    assert _duo(state)["symbols"]["BTCUSDT"]["side"] == "long"


def test_nonfinal_corrupt_line_raises(reload_wr, tmp_path):
    """Corruption that is NOT the trailing line is a hard error, not silently
    skipped -- skipping it would fabricate state."""
    root = tmp_path / "midcorrupt"
    wr = reload_wr(root, backend="journal")
    path = root / "logs" / "mid.jsonl"
    path.write_text(
        '{"schema_version":1,"seq":0,"kind":"transition"}\n'
        "{ broken line in the middle\n"
        '{"schema_version":1,"seq":2,"kind":"transition"}\n',
        encoding="utf-8",
    )
    import pytest

    with pytest.raises((ValueError, json.JSONDecodeError)):
        wr.read_jsonl_tolerant(path)


# ---------------------------------------------------------------------------
# default backend == legacy == current behavior
# ---------------------------------------------------------------------------

def test_default_backend_is_legacy(reload_wr, tmp_path):
    wr = reload_wr(tmp_path / "default", backend=None)  # no HERMX_STATE_BACKEND set
    assert wr.HERMX_STATE_BACKEND == "legacy"
    _run(wr, [(BUY, BUY_AT)])
    # Legacy authority is the snapshot; deleting it loses state (the pre-Phase-1
    # behavior we deliberately preserve in legacy mode -- the journal exists but is
    # not consulted for authority here).
    (tmp_path / "default" / "paper-state.json").unlink()
    assert wr.load_paper_state()["policies"] == {}
    # But the journal WAS written in parallel even in legacy mode (soak data).
    assert (tmp_path / "default" / "logs" / "position-journal.jsonl").exists()
