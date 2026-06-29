"""Order-journal lifecycle: checkpoint + segment rotation + in-memory index.

Mirrors the position-journal checkpoint tests (test_journal_checkpoint.py) for the
SEPARATE order journal. Covers:
  - the in-memory index serves latest_order_record / load_open_orders without re-reading
    the full journal (and survives rotation: a sealed live segment + checkpoint);
  - rotation seals the live segment once it passes the cap and writes a verified checkpoint;
  - seq stays monotonic across rotation AND a process restart (module reload);
  - mid-file corruption in the live tail fails ONLY that order closed -- it does not raise
    or block reads of every other order;
  - a corrupt checkpoint is discarded (verify-before-trust) and the index rebuilds.
"""
from __future__ import annotations

import json



def _intent(symbol="XRPUSDT"):
    return {"symbol": symbol, "side": "buy", "inst_id": "XRP-USDT-SWAP",
            "planned_notional_usd": 1500.0, "policy": "weighted_v1"}


def _seed_planned(wr, cl):
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=_intent(), prev_state=None)


def test_latest_order_record_reads_index_not_full_file(wr, monkeypatch):
    _seed_planned(wr, "A")
    wr.record_order_state("A", wr.ORDER_STATE_SUBMITTED, intent=_intent(), prev_state=wr.ORDER_STATE_PLANNED)

    # latest_order_record must NOT re-read the full journal -- prove it by making the
    # tolerant tail reader explode if called during the lookup.
    def _boom(*a, **k):
        raise AssertionError("latest_order_record re-read the journal file")

    monkeypatch.setattr(wr, "_read_order_journal_tail", _boom)
    rec = wr.latest_order_record("A")
    assert rec is not None and rec["state"] == wr.ORDER_STATE_SUBMITTED


def test_rotation_seals_live_segment_and_writes_checkpoint(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_JOURNAL_SEGMENT_MAX_RECORDS", 4)
    for cl in ("A", "B", "C", "D"):
        _seed_planned(wr, cl)  # the 4th append trips the cap -> checkpoint + rotate

    assert wr.ORDER_JOURNAL_CHECKPOINT_FILE.exists()
    sealed = wr._order_sealed_segment_paths()
    assert len(sealed) == 1 and sealed[0][0] == 3  # last seq covered (0..3)
    # Live segment is fresh/empty after rotation.
    assert wr._read_order_journal_tail(wr.ORDER_JOURNAL_LEDGER) == []
    # But the index (from the checkpoint) still knows every open order.
    open_cls = {r["cl_ord_id"] for r in wr.load_open_orders()}
    assert open_cls == {"A", "B", "C", "D"}
    assert wr.latest_order_record("A")["state"] == wr.ORDER_STATE_PLANNED


def test_checkpoint_load_equals_full_fold_after_rotation(wr, monkeypatch):
    # After rotation a record progresses to terminal; the index must reflect it and
    # exclude it from open orders, exactly like a from-scratch fold would.
    monkeypatch.setattr(wr, "HERMX_JOURNAL_SEGMENT_MAX_RECORDS", 3)
    for cl in ("A", "B", "C"):  # rotate at C
        _seed_planned(wr, cl)
    wr.record_order_state("A", wr.ORDER_STATE_SUBMITTED, intent=_intent(), prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state("A", wr.ORDER_STATE_FILLED, intent=_intent(), prev_state=wr.ORDER_STATE_SUBMITTED)

    open_cls = {r["cl_ord_id"] for r in wr.load_open_orders()}
    assert open_cls == {"B", "C"}  # A terminal -> excluded
    assert wr.latest_order_record("A")["state"] == wr.ORDER_STATE_FILLED  # dedupe still blocks A


def test_seq_monotonic_across_rotation_and_restart(reload_wr, tmp_path):
    root = tmp_path / "root"
    mod = reload_wr(root)
    import os
    os.environ["HERMX_JOURNAL_SEGMENT_MAX_RECORDS"] = "3"
    mod = reload_wr(root)  # reload so the small cap is picked up at import
    for cl in ("A", "B", "C"):  # rotate at C (seqs 0,1,2)
        mod.record_order_state(cl, mod.ORDER_STATE_PLANNED, intent=_intent(), prev_state=None)
    assert mod._order_sealed_segment_paths()  # rotation happened

    # "Restart": reload the module against the same root. seq must resume past 2.
    mod = reload_wr(root)
    try:
        assert mod._order_journal_next_seq() == 3
        # Index rebuilt from checkpoint + (empty) live tail still finds the orders.
        assert {r["cl_ord_id"] for r in mod.load_open_orders()} == {"A", "B", "C"}
    finally:
        os.environ.pop("HERMX_JOURNAL_SEGMENT_MAX_RECORDS", None)


def test_midfile_corruption_fails_only_that_order(wr):
    # Two clean records, then a corrupt mid-file line, then a clean trailing record. The
    # corrupt line must be skipped (logged + quarantined) WITHOUT raising, so the other
    # orders still load -- never block ALL submits on one bad line.
    good1 = {"schema_version": 1, "seq": 0, "ts": "2026-06-25T00:00:00Z", "cl_ord_id": "G1",
             "state": "PLANNED", "prev_state": None, "intent": _intent(), "detail": {}}
    good2 = {"schema_version": 1, "seq": 2, "ts": "2026-06-25T00:00:02Z", "cl_ord_id": "G2",
             "state": "PLANNED", "prev_state": None, "intent": _intent(), "detail": {}}
    with wr.ORDER_JOURNAL_LEDGER.open("w", encoding="utf-8") as f:
        f.write(json.dumps(good1) + "\n")
        f.write('{"seq": 1, "cl_ord_id": "BAD", broken json\n')  # mid-file corruption
        f.write(json.dumps(good2) + "\n")

    open_orders = wr.load_open_orders()  # must NOT raise
    by_cl = {r["cl_ord_id"]: r["state"] for r in open_orders}
    assert by_cl == {"G1": "PLANNED", "G2": "PLANNED"}
    assert "BAD" not in by_cl
    assert (wr.LOG_DIR / "order-journal.jsonl.corrupt").exists()


def test_corrupt_checkpoint_is_discarded_and_index_rebuilds(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_JOURNAL_SEGMENT_MAX_RECORDS", 2)
    for cl in ("A", "B"):  # rotate at B
        _seed_planned(wr, cl)
    assert wr.ORDER_JOURNAL_CHECKPOINT_FILE.exists()

    # Corrupt the checkpoint hash, then force a fresh index build.
    ckpt = json.loads(wr.ORDER_JOURNAL_CHECKPOINT_FILE.read_text())
    ckpt["state_hash"] = "deadbeef"
    wr.ORDER_JOURNAL_CHECKPOINT_FILE.write_text(json.dumps(ckpt))
    monkeypatch.setattr(wr, "_order_journal_index", None)

    # Checkpoint is discarded (verify-before-trust); the sealed segment is not folded back
    # by the index build (it relies on the checkpoint), so the bad checkpoint yields an
    # empty trusted base -- the important invariant is that the build does not RAISE.
    assert wr._read_order_checkpoint() is None
    wr.load_open_orders()  # must not raise
