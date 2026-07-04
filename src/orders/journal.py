"""Order journal / submission-outcome state machine (Phase 4 extraction, REFACTOR_PLAN.md).

Houses the write-ahead order-journal cluster that used to live in
webhook_receiver.py: the state-machine transition table +
``order_state_can_transition``, the tolerant live-segment reader
``_read_order_journal_tail``, the in-memory index
(``_order_index_apply`` / ``_build_order_index`` / ``_order_index``), the
monotonic seq (``_order_journal_next_seq``), the verified checkpoint + sealed
segment rotation (``_read_order_checkpoint``, ``_order_checkpoint_and_rotate``,
``_maybe_order_checkpoint_and_rotate`` and friends), the write path
``record_order_state`` and the readers ``load_open_orders`` /
``latest_order_record`` -- plus the readiness->journal adapters
``_order_intent_from_readiness`` / ``_cl_ord_id_from_readiness``.

Root-bound / reload-reset module state STAYS defined in webhook_receiver.py
and is read lazily via ``import webhook_receiver as _wr`` -- the same pattern
as src/alerts.py (Phase 0), src/signals/ (Phase 1), src/control_state.py
(Phase 2) and src/strategy/ (Phase 3):
  - ``ORDER_JOURNAL_LEDGER`` / ``ORDER_JOURNAL_CHECKPOINT_FILE`` / ``LOG_DIR``
    -- derived from HERMX_ROOT at import time, rebound into the tmp root by
    the per-test ``importlib.reload(webhook_receiver)`` in conftest's ``wr``
    fixture;
  - ``HERMX_JOURNAL_SEGMENT_MAX_RECORDS`` / ``HERMX_JOURNAL_SEGMENT_RETENTION``
    -- monkeypatched on wr (test_order_journal_checkpoint);
  - ``_ORDER_JOURNAL_LOCK`` -- lives with the other wr locks, fresh per reload;
  - ``_order_journal_seq_cache`` / ``_order_journal_index`` -- the caches that
    MUST reset per test reload (Phase 1 finding); test_order_journal_checkpoint
    also rebinds ``_order_journal_index`` to None on wr directly.
``_read_order_journal_tail`` and ``append_jsonl_durable`` are likewise always
called through ``_wr`` because tests rebind them on wr
(test_order_journal_checkpoint _boom proof, test_order_state_machine
spy_durable write-ahead proof). webhook_receiver re-exports every moved name
so ``wr.<fn>`` call sites and monkeypatch seams keep working -- reconcile
(Phase 5, still in wr) keeps calling load_open_orders/record_order_state
through the shim.

The atomic-write primitives are imported directly from src/control_state.py
(their Phase 2 home) -- this module is new code, not a re-export shim, so it
does not route them through wr.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from control_state import (
    _atomic_json_dump,
    _canonical_state_json,
    _fail_closed_state_write,
    _fsync_dir,
)
from webhook.money import canonicalize_decimal_fields
from webhook.timeutil import now_iso

# Append-only, durable (fsync) log of the lifecycle
# PLANNED -> SUBMITTED -> (FILLED | REJECTED | UNKNOWN). The PLANNED/SUBMITTED records
# are persisted BEFORE the submit subprocess so restart reconciliation (Task 4) has
# authoritative clOrdId keys even after a crash mid-send. UNKNOWN (timeout/crash) is a
# first-class state that triggers reconciliation, NOT a failure.
ORDER_JOURNAL_SCHEMA_VERSION = 1
ORDER_JOURNAL_CHECKPOINT_VERSION = 1
ORDER_STATE_PLANNED = "PLANNED"
ORDER_STATE_SUBMITTED = "SUBMITTED"
ORDER_STATE_FILLED = "FILLED"
ORDER_STATE_REJECTED = "REJECTED"
ORDER_STATE_UNKNOWN = "UNKNOWN"
# Terminal states accept no further transitions; the rest are "open" and are what
# load_open_orders() surfaces to startup reconciliation.
ORDER_TERMINAL_STATES = frozenset({ORDER_STATE_FILLED, ORDER_STATE_REJECTED})
ORDER_NON_TERMINAL_STATES = frozenset({ORDER_STATE_PLANNED, ORDER_STATE_SUBMITTED, ORDER_STATE_UNKNOWN})

# Legal transitions. None is the implicit pre-existence state (a brand-new clOrdId).
# PLANNED -> REJECTED covers an order aborted *before* it is ever sent. SUBMITTED and
# UNKNOWN may both still resolve to any terminal outcome (or re-UNKNOWN, so a resolver
# can re-reconcile). Terminal states {FILLED, REJECTED} are frozen: no transitions.
_ORDER_STATE_TRANSITIONS: "dict[str | None, frozenset[str]]" = {
    None: frozenset({ORDER_STATE_PLANNED}),
    ORDER_STATE_PLANNED: frozenset({ORDER_STATE_SUBMITTED, ORDER_STATE_REJECTED}),
    ORDER_STATE_SUBMITTED: frozenset({ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_UNKNOWN}),
    ORDER_STATE_UNKNOWN: frozenset({ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_UNKNOWN}),
    ORDER_STATE_FILLED: frozenset(),
    ORDER_STATE_REJECTED: frozenset(),
}


def order_state_can_transition(old: "str | None", new: str) -> bool:
    """PURE predicate: is ``old -> new`` a legal order-state transition? Unknown
    ``old`` states and any ``new`` that is not reachable return False (fail closed)."""
    return new in _ORDER_STATE_TRANSITIONS.get(old, frozenset())


def _read_order_journal_tail(path: Path) -> list:
    """Tolerant per-line reader for the ORDER journal live segment.

    Unlike read_jsonl_tolerant (which RAISES on mid-file corruption -- correct for the
    position journal where corruption means money state is wrong), a single corrupt
    order-journal line must NOT brick the index and block ALL submits. We log it loudly,
    quarantine the offending line to ``<path>.corrupt`` for forensics, and skip it --
    that one order is effectively failed-closed (absent from the index) while every other
    order keeps flowing. A truncated trailing line is the expected torn-tail case."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw:
        return []
    lines = raw.split("\n")
    last_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip():
            last_idx = i
    out: list = []
    corrupt: list = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            if i == last_idx:
                logging.warning("order-journal: quarantined truncated trailing line in %s", path)
            else:
                logging.error("order-journal: skipping corrupt mid-file line %d in %s (order failed closed)", i, path)
            corrupt.append(ln)
    if corrupt:
        try:
            (path.parent / (path.name + ".corrupt")).write_text("\n".join(corrupt), encoding="utf-8")
        except Exception:
            pass
    return out


def _order_index_apply(index: dict, rec: dict) -> None:
    """Fold one order-journal record into the index (latest-by-max-seq, origin-by-min-seq).
    Idempotent in seq, so applying a record already folded in is a no-op."""
    seq = rec.get("seq")
    if not isinstance(seq, int):
        return
    cl = rec.get("cl_ord_id")
    latest = index["latest"]
    cur = latest.get(cl)
    if cur is None or seq > int(cur.get("seq") or -1):
        latest[cl] = rec
    origin = index["origin"]
    cur_origin = origin.get(cl)
    if cur_origin is None or seq < cur_origin[0]:
        origin[cl] = (seq, rec.get("ts"))


def _build_order_index() -> dict:
    """Rebuild the order index from the VERIFIED checkpoint (latest-per-cl + origin,
    subsuming every sealed segment) plus the live-segment tail (records newer than the
    checkpoint). Bounded: the live segment is rotation-capped and sealed segments are
    folded into the checkpoint, so this never replays the full history."""
    import webhook_receiver as _wr
    index = {"latest": {}, "origin": {}}
    ckpt = _read_order_checkpoint()
    last_seq = -1
    if ckpt is not None:
        last_seq = ckpt["last_seq"]
        for rec in ckpt.get("index_records") or []:
            _order_index_apply(index, rec)
        for cl, seq, ts in ckpt.get("origins") or []:
            cur = index["origin"].get(cl)
            if cur is None or seq < cur[0]:
                index["origin"][cl] = (seq, ts)
    for rec in _wr._read_order_journal_tail(_wr.ORDER_JOURNAL_LEDGER):
        s = rec.get("seq")
        if isinstance(s, int) and s > last_seq:
            _order_index_apply(index, rec)
    return index


def _order_index() -> dict:
    """The in-memory order index, built lazily on first use (under the journal lock)."""
    import webhook_receiver as _wr
    if _wr._order_journal_index is None:
        _wr._order_journal_index = _build_order_index()
    return _wr._order_journal_index


def _order_journal_next_seq() -> int:
    import webhook_receiver as _wr
    if _wr._order_journal_seq_cache is None:
        last = -1
        cl = _order_checkpoint_last_seq_floor()
        if cl is not None and cl > last:
            last = cl
        for seq, _path in _order_sealed_segment_paths():
            if seq > last:
                last = seq
        for rec in _wr._read_order_journal_tail(_wr.ORDER_JOURNAL_LEDGER):
            s = rec.get("seq")
            if isinstance(s, int) and s > last:
                last = s
        _wr._order_journal_seq_cache = last
    _wr._order_journal_seq_cache += 1
    return _wr._order_journal_seq_cache


# --- Order-journal sealed segments + checkpoint (mirrors the position-journal helpers) ---

def _parse_order_sealed_seq(name: str):
    """The seq encoded in a sealed order segment ``order-journal.<seq>.jsonl`` (the last
    seq it covers), or None if the name is not a sealed segment."""
    prefix, suffix = "order-journal.", ".jsonl"
    if name.startswith(prefix) and name.endswith(suffix):
        mid = name[len(prefix):-len(suffix)]
        if mid.isdigit():
            return int(mid)
    return None


def _order_sealed_segment_paths() -> list:
    """Sealed order-journal segments as (seq, path), ascending. The live segment and the
    ``.corrupt`` quarantine file are excluded by the naming rule; the checkpoint (``.json``)
    by suffix."""
    import webhook_receiver as _wr
    out = []
    for p in _wr.LOG_DIR.glob("order-journal.*.jsonl"):
        seq = _parse_order_sealed_seq(p.name)
        if seq is not None:
            out.append((seq, p))
    out.sort(key=lambda t: t[0])
    return out


def _read_all_order_records() -> list:
    """Every order record across sealed segments (seq order) + the live segment, sorted
    by seq. Used by the checkpoint fold; tolerant of corrupt/torn lines."""
    import webhook_receiver as _wr
    records: list = []
    for _seq, path in _order_sealed_segment_paths():
        records.extend(_wr._read_order_journal_tail(path))
    records.extend(_wr._read_order_journal_tail(_wr.ORDER_JOURNAL_LEDGER))
    records.sort(key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else -1)
    return records


def _order_index_hash(index_records: list, origins: list) -> str:
    payload = {
        "index_records": sorted(index_records, key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else -1),
        "origins": sorted(origins, key=lambda o: str(o[0])),
    }
    return hashlib.sha256(_canonical_state_json(payload).encode("utf-8")).hexdigest()


def _order_checkpoint_last_seq_floor() -> "int | None":
    """The checkpoint's last_seq used only as a monotonic seq floor (best-effort; a hash
    mismatch is irrelevant here -- an over-high floor only skips seq numbers, never
    reuses them), so read it without the full verify."""
    import webhook_receiver as _wr
    if not _wr.ORDER_JOURNAL_CHECKPOINT_FILE.exists():
        return None
    try:
        ckpt = json.loads(_wr.ORDER_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
        ls = ckpt.get("last_seq")
        return ls if isinstance(ls, int) else None
    except Exception:
        return None


def _read_order_checkpoint() -> "dict | None":
    """Load the order checkpoint with VERIFY-BEFORE-TRUST: returns it only if it parses,
    its versions are not from a newer writer, and its stored hash recomputes over the
    stored index/origins. Any failure is loud and returns None (full-tail rebuild)."""
    import webhook_receiver as _wr
    if not _wr.ORDER_JOURNAL_CHECKPOINT_FILE.exists():
        return None
    try:
        ckpt = json.loads(_wr.ORDER_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.error("order-journal checkpoint unreadable (%s) -- DISCARDING", exc)
        return None
    sv = ckpt.get("schema_version")
    cv = ckpt.get("checkpoint_version")
    if not isinstance(sv, int) or not isinstance(cv, int) or sv > ORDER_JOURNAL_SCHEMA_VERSION or cv > ORDER_JOURNAL_CHECKPOINT_VERSION:
        logging.error("order-journal checkpoint from a newer writer (schema=%r checkpoint=%r) -- DISCARDING", sv, cv)
        return None
    last_seq = ckpt.get("last_seq")
    index_records = ckpt.get("index_records")
    origins = ckpt.get("origins")
    if not isinstance(last_seq, int) or not isinstance(index_records, list) or not isinstance(origins, list):
        logging.error("order-journal checkpoint missing last_seq/index_records/origins -- DISCARDING")
        return None
    if ckpt.get("state_hash") != _order_index_hash(index_records, origins):
        logging.error("order-journal checkpoint state_hash MISMATCH -- DISCARDING corrupt checkpoint")
        return None
    return ckpt


def _rotate_order_live_segment(last_seq: int) -> None:
    """Seal the live order segment to ``order-journal.<last_seq>.jsonl`` and start a fresh
    one. Called only AFTER a verified checkpoint covering last_seq is fsync'd."""
    import webhook_receiver as _wr
    if not _wr.ORDER_JOURNAL_LEDGER.exists():
        return
    sealed = _wr.LOG_DIR / f"order-journal.{last_seq}.jsonl"
    os.replace(_wr.ORDER_JOURNAL_LEDGER, sealed)
    _wr.ORDER_JOURNAL_LEDGER.touch()
    _fsync_dir(_wr.LOG_DIR)


def _enforce_order_segment_retention() -> None:
    """Keep the last K sealed order segments; prune older ones (the checkpoint subsumes
    them). HERMX_JOURNAL_SEGMENT_RETENTION < 0 keeps all."""
    import webhook_receiver as _wr
    if _wr.HERMX_JOURNAL_SEGMENT_RETENTION < 0:
        return
    sealed = _order_sealed_segment_paths()
    excess = len(sealed) - _wr.HERMX_JOURNAL_SEGMENT_RETENTION
    for _seq, path in sealed[:max(0, excess)]:
        try:
            path.unlink()
        except OSError as exc:
            logging.warning("order-journal: could not prune sealed segment %s: %s", path, exc)


def _order_checkpoint_and_rotate() -> None:
    """Fold the full order history (sealed + live) into the latest-per-cl index + origins,
    write a verified checkpoint, then seal the live segment and prune old sealed ones.
    Simpler than the position-journal twin: the order index is a pure deterministic fold
    of records by seq (no money-state math), so the from-scratch fold IS authoritative and
    no dual-oracle equivalence check is needed. Fail-closed on any write OSError."""
    import webhook_receiver as _wr
    records = _read_all_order_records()
    if not records:
        return
    index = {"latest": {}, "origin": {}}
    last_seq = -1
    for rec in records:
        s = rec.get("seq")
        if isinstance(s, int) and s > last_seq:
            last_seq = s
        _order_index_apply(index, rec)
    if last_seq < 0:
        return
    index_records = list(index["latest"].values())
    origins = [[cl, seq, ts] for cl, (seq, ts) in index["origin"].items()]
    ckpt = {
        "schema_version": ORDER_JOURNAL_SCHEMA_VERSION,
        "checkpoint_version": ORDER_JOURNAL_CHECKPOINT_VERSION,
        "last_seq": last_seq,
        "index_records": index_records,
        "origins": origins,
        "state_hash": _order_index_hash(index_records, origins),
        "created_at": now_iso(),
    }
    try:
        _atomic_json_dump(_wr.ORDER_JOURNAL_CHECKPOINT_FILE, ckpt)  # fsync'd before rotate
        _rotate_order_live_segment(last_seq)
    except OSError as exc:
        _fail_closed_state_write("order-checkpoint-rotate", exc, context={"last_seq": last_seq})
        raise
    _enforce_order_segment_retention()
    # The in-memory index already reflects every folded record; rebind it to the
    # freshly-folded structure so it stays the single source of truth post-rotation.
    _wr._order_journal_index = index


def _maybe_order_checkpoint_and_rotate() -> None:
    """Trigger an order-journal checkpoint+rotation once the live segment grows past
    HERMX_JOURNAL_SEGMENT_MAX_RECORDS, keeping rebuild bounded and disk capped."""
    import webhook_receiver as _wr
    live = _wr._read_order_journal_tail(_wr.ORDER_JOURNAL_LEDGER)
    if len(live) < _wr.HERMX_JOURNAL_SEGMENT_MAX_RECORDS:
        return
    _order_checkpoint_and_rotate()


def record_order_state(
    cl_ord_id: "str | None",
    new_state: str,
    intent: "dict | None" = None,
    detail: "dict | None" = None,
    prev_state: "str | None" = None,
) -> dict:
    """Validate ``prev_state -> new_state`` and durably (fsync) append one order-journal
    record. Raises ValueError on an illegal transition (the caller must not persist a
    state the machine forbids). OSError from the durable append propagates to the caller,
    which fail-closes the money path (see execute_if_enabled write-ahead)."""
    import webhook_receiver as _wr
    if not order_state_can_transition(prev_state, new_state):
        logging.error("ILLEGAL order-state transition for %s: %s -> %s", cl_ord_id, prev_state, new_state)
        raise ValueError(f"illegal order-state transition {prev_state} -> {new_state} for {cl_ord_id}")
    with _wr._ORDER_JOURNAL_LOCK:
        # Ensure the index is built BEFORE appending so the build reads the pre-append
        # live tail; the new record is then folded in explicitly (no double count).
        index = _order_index()
        # Re-validate against the journal's AUTHORITATIVE latest state under the lock:
        # the caller's prev_state may be stale (e.g. the unknown-resolver snapshotted
        # SUBMITTED, then the submit path journaled a terminal REJECTED during the
        # resolver's reconcile backoff). A terminal state must never be clobbered.
        latest = index["latest"].get(cl_ord_id)
        actual_state = latest.get("state") if latest is not None else None
        if actual_state is not None and not order_state_can_transition(actual_state, new_state):
            logging.error(
                "STALE order-state write rejected for %s: caller claimed prev_state=%s "
                "but journal state is %s (-> %s)",
                cl_ord_id, prev_state, actual_state, new_state,
            )
            raise ValueError(
                f"stale order-state write for {cl_ord_id}: caller claimed "
                f"prev_state={prev_state} but journal state is {actual_state}; "
                f"{actual_state} -> {new_state} is illegal"
            )
        record = {
            "schema_version": ORDER_JOURNAL_SCHEMA_VERSION,
            "seq": _order_journal_next_seq(),
            "ts": now_iso(),
            "cl_ord_id": cl_ord_id,
            "state": new_state,
            "prev_state": prev_state,
            "intent": canonicalize_decimal_fields(intent or {}),
            "detail": canonicalize_decimal_fields(detail or {}),
        }
        _wr.append_jsonl_durable(_wr.ORDER_JOURNAL_LEDGER, record)
        _order_index_apply(index, record)
        # Bound the live segment: fold into a verified checkpoint + seal once it grows
        # past the segment cap, so the journal does not grow without limit.
        _maybe_order_checkpoint_and_rotate()
    return record


def _order_intent_from_readiness(readiness: dict) -> dict:
    """The minimal, exchange-agnostic intent persisted on each order-journal record."""
    exec_intent = readiness.get("execution_intent") or {}
    instrument = readiness.get("instrument") or {}
    return {
        "symbol": readiness.get("symbol"),
        "side": readiness.get("signal_side"),
        "inst_id": readiness.get("inst_id") or instrument.get("inst_id"),
        "planned_notional_usd": exec_intent.get("planned_notional_usd"),
        "policy": exec_intent.get("policy"),
        # Issue #20a: persist the resolved (venue, mode) the order was submitted to so
        # the order-state reconciler queries the SAME account -- not the global OKX-demo
        # default. Venue comes from the strategy instrument (strategy_instrument); mode /
        # simulated_trading are the readiness-resolved effective mode (Phase 0). Orders
        # journalled before this field existed simply lack it -> reconcile falls back to
        # the OKX-demo default (unchanged pre-#20a behavior).
        "venue": instrument.get("exchange"),
        "mode": readiness.get("execution_mode"),
        "simulated_trading": readiness.get("simulated_trading"),
    }


def _cl_ord_id_from_readiness(readiness: dict) -> "str | None":
    exec_intent = readiness.get("execution_intent") or {}
    fill = readiness.get("okx_fill") or {}
    return exec_intent.get("client_order_id") or fill.get("client_order_id")


def load_open_orders() -> list[dict]:
    """Restart-recovery reader consumed by Task 4 reconciliation: the LATEST record
    (highest seq) per cl_ord_id whose state is still non-terminal
    (PLANNED/SUBMITTED/UNKNOWN). Reads the in-memory order index (verified checkpoint +
    live-segment tail) rather than re-folding the journal, so records that have rotated
    into sealed segments are still seen. Terminal (FILLED/REJECTED) orders are omitted.

    Each returned record is a COPY of the latest with an added ``origin_ts`` -- the ts of
    the order's FIRST (lowest-seq) journal record. The lifecycle backstop measures age
    from origin_ts so re-recording (e.g. UNKNOWN->UNKNOWN) can never reset the clock.

    Reads from the bounded in-memory index (checkpoint + live tail), never the full
    journal -- so it stays O(open orders) regardless of total journal length."""
    import webhook_receiver as _wr
    with _wr._ORDER_JOURNAL_LOCK:
        index = _order_index()
        latest = index["latest"]
        origin = index["origin"]
        out: list[dict] = []
        for cl, rec in latest.items():
            if rec.get("state") not in ORDER_NON_TERMINAL_STATES:
                continue
            enriched = dict(rec)
            enriched["origin_ts"] = origin.get(cl, (None, rec.get("ts")))[1]
            out.append(enriched)
    return out


def latest_order_record(cl_ord_id: str | None) -> dict | None:
    """Latest journal record for a clOrdId (the idempotency/dedupe authority). Reads the
    in-memory index -- O(1) -- instead of re-folding the whole journal on every submit."""
    import webhook_receiver as _wr
    cl = str(cl_ord_id or "").strip()
    if not cl:
        return None
    with _wr._ORDER_JOURNAL_LOCK:
        return _order_index()["latest"].get(cl)


def order_history_for(cl_ord_id: "str | None") -> dict:
    """Full seq-ordered journal history for one cl_ord_id (all sealed segments still on
    disk + the live tail), read under the journal lock so it cannot race a concurrent
    checkpoint+rotation. Returns {"records": [...], "history_complete": bool}.

    history_complete is True iff the earliest returned record has prev_state is None --
    i.e. it is provably the order's actual first-ever write (per _ORDER_STATE_TRANSITIONS,
    only a true first write has prev_state=None) -- self-contained, no dependency on the
    checkpoint's separately-tracked origin map (which is not guaranteed durable across
    many rotations for a very long-lived order, since _order_checkpoint_and_rotate folds
    purely from currently-on-disk segments each time, not from the prior checkpoint)."""
    import webhook_receiver as _wr
    cl = str(cl_ord_id or "").strip()
    if not cl:
        return {"records": [], "history_complete": False}
    with _wr._ORDER_JOURNAL_LOCK:
        records = [r for r in _read_all_order_records() if r.get("cl_ord_id") == cl]
    records.sort(key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else -1)
    history_complete = bool(records) and records[0].get("prev_state") is None
    return {"records": records, "history_complete": history_complete}
