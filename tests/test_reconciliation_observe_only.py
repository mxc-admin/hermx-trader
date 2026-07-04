"""Exchange reconciliation -- OBSERVE-ONLY (REFACTOR_PLAN.md:208-215, acceptance
:236/:237 -- Phase 1 task 4).

Fixture-driven, NO network: a StubExecutor replaces the Task-3 query interface
(get_order / get_open_orders / get_order_history_archive / get_positions) so the
reconciliation decision logic is exercised against canned normalized responses.

Covers:
  (a) get_order filled                         -> FILLED
  (b) partial fill                             -> FILLED, partial=True
  (c) not_found + pending empty + archive empty-> UNKNOWN (money-safety: absence is
      NEVER an auto-rejection; only a venue-confirmed canceled/zero-fill => REJECTED)
  (d) non-terminal (live) through all retries  -> UNKNOWN after bounded attempts
  (e) fallback chain: order miss -> pending hit; order+pending miss -> archive hit
  (f) post-submit hook (forced should_execute) -> reconciliation writes the
      authoritative SUBMITTED->terminal transition; mismatch emits RECONCILE_MISMATCH
  (g) startup bootstrap: open orders reconciled (terminal transition written),
      local-vs-exchange position divergence emits RECONCILE_MISMATCH, startup flag set
  (h) disabled config path is unchanged: not_submitted, NO reconcile call, no journal
"""
from __future__ import annotations

from unittest import mock

import pytest



# ---------------------------------------------------------------------------
# Normalized-shape builders (mirror the venue-neutral query interface output).
# ---------------------------------------------------------------------------

def norm_order(state, *, cl_ord_id="cl-1", ord_id="ord-1", inst_id="XRP-USDT-SWAP", acc=0.0, avg=None):
    return {
        "exchange": "okx_demo", "inst_id": inst_id, "ord_id": ord_id, "cl_ord_id": cl_ord_id,
        "state": state, "acc_fill_sz": acc, "avg_px": avg, "ord_type": "market",
        "side": "buy", "pos_side": "net", "ts": "2026-06-26T00:00:00Z", "raw": {},
    }


def norm_not_found():
    return {
        "exchange": "okx_demo", "inst_id": None, "ord_id": None, "cl_ord_id": None,
        "state": "not_found", "acc_fill_sz": 0.0, "avg_px": None, "ord_type": None,
        "side": None, "pos_side": None, "ts": None, "raw": {"code": "51603"},
    }


def norm_position(inst_id, pos, *, pos_side="net", avg=1.0):
    return {"exchange": "okx_demo", "inst_id": inst_id, "pos": pos, "pos_side": pos_side, "avg_px": avg, "upl": 0.0, "raw": {}}


class StubExecutor:
    """In-memory stand-in for the venue query interface. No subprocess, no network."""

    def __init__(self, *, order=None, pending=None, archive=None, positions=None):
        self._order = order
        self._pending = pending or []
        self._archive = archive or []
        self._positions = positions or []
        self.calls = []

    def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        self.calls.append(("get_order", inst_id, ord_id, cl_ord_id))
        o = self._order(inst_id, ord_id, cl_ord_id) if callable(self._order) else self._order
        return o if o is not None else norm_not_found()

    def get_open_orders(self, inst_id=None):
        self.calls.append(("get_open_orders", inst_id))
        return list(self._pending)

    def get_order_history_archive(self, inst_id=None, limit=100):
        self.calls.append(("get_order_history_archive", inst_id, limit))
        return list(self._archive)

    def get_positions(self, inst_id=None):
        self.calls.append(("get_positions", inst_id))
        return list(self._positions)


LOOKUP = {"inst_id": "XRP-USDT-SWAP", "cl_ord_id": "cl-1", "ord_id": "ord-1"}


# ---------------------------------------------------------------------------
# (a)-(c),(e) reconcile_order_once outcome mapping + fallback chain.
# ---------------------------------------------------------------------------

def test_get_order_filled_maps_filled(wr):
    ex = StubExecutor(order=norm_order("filled", acc=10.0, avg=0.5))
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_FILLED
    assert out["partial"] is False
    assert out["source"] == "get_order"
    assert out["acc_fill_sz"] == 10.0


def test_partial_fill_maps_filled_partial(wr):
    # state=partially_filled => FILLED + partial=True (:211).
    ex = StubExecutor(order=norm_order("partially_filled", acc=4.0, avg=0.5))
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert (out["state"], out["partial"]) == (wr.ORDER_STATE_FILLED, True)
    # And the 0 < accFillSz < ordered branch (state=filled but underfilled).
    ex2 = StubExecutor(order=norm_order("filled", acc=4.0))
    out3 = wr.reconcile_order_once(ex2, {**LOOKUP, "ordered": 10.0})
    assert (out3["state"], out3["partial"]) == (wr.ORDER_STATE_FILLED, True)


def test_not_found_everywhere_maps_unknown(wr):
    # MONEY-SAFETY: an order absent from get_order + pending + archive is NOT proof of
    # rejection (it may have filled and aged out, or the query may be transiently
    # failing). Absence => UNKNOWN (keep tracking), never REJECTED (drop it as flat).
    ex = StubExecutor(order=norm_not_found(), pending=[], archive=[])
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert out["reason"] == "not_found"
    assert out["matched_order"] is None
    # The full fallback chain was exercised: order -> pending -> archive.
    kinds = [c[0] for c in ex.calls]
    assert kinds == ["get_order", "get_open_orders", "get_order_history_archive"]


def test_fresh_not_found_backoff_stays_unknown_never_rejected(wr):
    # The backoff driver must NOT short-circuit a not_found to REJECTED on attempt 1.
    # A persistently not_found order is re-polled to the bound and ends UNKNOWN.
    ex = StubExecutor(order=norm_not_found(), pending=[], archive=[])
    sleeps = []
    out = wr.reconcile_order_with_backoff(ex, LOOKUP, sleep=lambda d: sleeps.append(d))
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert out["state"] != wr.ORDER_STATE_REJECTED
    assert out["attempts"] == wr.RECONCILE_MAX_ATTEMPTS
    assert out["reason"].startswith("deadline_exhausted")
    assert "not_found" in out["reason"]
    # not_found is non-terminal now, so every attempt is re-polled (no early return).
    assert sum(1 for c in ex.calls if c[0] == "get_order") == wr.RECONCILE_MAX_ATTEMPTS


def test_canceled_zero_fill_maps_rejected(wr):
    ex = StubExecutor(order=norm_order("canceled", acc=0.0))
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_REJECTED
    assert out["reason"] == "canceled_zero_fill"


def test_canceled_after_partial_maps_filled_partial(wr):
    ex = StubExecutor(order=norm_order("canceled", acc=3.0))
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert (out["state"], out["partial"]) == (wr.ORDER_STATE_FILLED, True)


def test_canceled_missing_acc_fill_sz_maps_unknown_not_rejected(wr):
    """Regression: a canceled order whose acc_fill_sz is absent (None) must NOT be
    coerced to a fabricated 0 and rejected -- that would drop a canceled-after-partial
    position as flat. Absent fill size is inconclusive -> UNKNOWN, stays tracked."""
    order = norm_order("canceled", acc=0.0)
    order["acc_fill_sz"] = None  # venue omitted the field / query returned it missing
    ex = StubExecutor(order=order)
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert out["state"] != wr.ORDER_STATE_REJECTED
    assert out["reason"] == "canceled_fill_size_unavailable"


def test_canceled_confirmed_zero_fill_still_rejected(wr):
    """A canceled order with a REAL confirmed zero fill preserves the terminal
    REJECTED outcome (the only venue-confirmed rejection)."""
    ex = StubExecutor(order=norm_order("canceled", acc=0.0))
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_REJECTED
    assert out["reason"] == "canceled_zero_fill"


def test_fallback_order_miss_pending_hit(wr):
    # get_order not-found, but the order is live in orders-pending.
    ex = StubExecutor(order=norm_not_found(), pending=[norm_order("filled", acc=10.0)])
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_FILLED
    assert out["source"] == "orders_pending"


def test_fallback_order_and_pending_miss_archive_hit(wr):
    ex = StubExecutor(order=norm_not_found(), pending=[], archive=[norm_order("filled", acc=10.0)])
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_FILLED
    assert out["source"] == "orders_history_archive"
    assert [c[0] for c in ex.calls] == ["get_order", "get_open_orders", "get_order_history_archive"]


def test_pending_archive_match_filters_by_clordid(wr):
    # A non-matching order in pending must be skipped; the archive match wins.
    ex = StubExecutor(
        order=norm_not_found(),
        pending=[norm_order("filled", cl_ord_id="someone-else", ord_id="other")],
        archive=[norm_order("filled", cl_ord_id="cl-1", ord_id="ord-1", acc=10.0)],
    )
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["source"] == "orders_history_archive"
    assert out["cl_ord_id"] == "cl-1"


# ---------------------------------------------------------------------------
# (d) bounded backoff -> UNKNOWN; terminal short-circuits; wall-clock bound.
# ---------------------------------------------------------------------------

def test_nonterminal_through_retries_maps_unknown(wr):
    ex = StubExecutor(order=norm_order("live"))  # never terminal
    sleeps = []
    out = wr.reconcile_order_with_backoff(ex, LOOKUP, sleep=lambda d: sleeps.append(d))
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert out["attempts"] == wr.RECONCILE_MAX_ATTEMPTS == 5
    # 5 attempts => 4 backoff sleeps with base 500ms, doubling, capped at 8s.
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert out["reason"].startswith("deadline_exhausted")
    # The order-status query was polled 5 times -- the submission itself is never retried.
    assert sum(1 for c in ex.calls if c[0] == "get_order") == 5


def test_terminal_short_circuits_no_sleep(wr):
    ex = StubExecutor(order=norm_order("filled", acc=10.0))
    sleeps = []
    out = wr.reconcile_order_with_backoff(ex, LOOKUP, sleep=lambda d: sleeps.append(d))
    assert out["state"] == wr.ORDER_STATE_FILLED
    assert out["attempts"] == 1
    assert sleeps == []


def test_wall_clock_budget_bounds_attempts(wr):
    ex = StubExecutor(order=norm_order("live"))
    sleeps = []
    # A clock that jumps 30s on the first elapsed check forces the <=20s budget to
    # stop before the attempt count is exhausted.
    ticks = iter([0.0, 30.0, 30.0, 30.0, 30.0, 30.0])
    out = wr.reconcile_order_with_backoff(
        ex, LOOKUP, sleep=lambda d: sleeps.append(d), clock=lambda: next(ticks),
    )
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert sleeps == []  # budget tripped before the first sleep
    assert out["attempts"] == 1


# ---------------------------------------------------------------------------
# (f) post-submit hook: reconciliation writes the authoritative terminal transition.
# ---------------------------------------------------------------------------

def _blocked_record(cl="mxc-xrpusdt-buy-blocked0000000de") -> dict:
    """An otherwise-armed record whose per-strategy submit flag is off (gate blocked)."""
    rec = _armed_record(cl)
    rec["execution_readiness"]["live_execution_enabled"] = False
    return rec


def _armed_record(cl="mxc-xrpusdt-buy-abc0123456789de") -> dict:
    return {
        "received_at": "2026-06-25T00:00:00Z",
        "execution_readiness": {
            "live_execution_enabled": True,
            "symbol": "XRPUSDT",
            "signal_side": "buy",
            "inst_id": "XRP-USDT-SWAP",
            "execution_intent": {"policy": "weighted_v1", "planned_notional_usd": 1500.0, "client_order_id": cl},
            "okx_fill": {"client_order_id": cl},
            "block_reason": None,
        },
    }


def _submit_executor(*, ok=True, mode="submit_enabled"):
    """Fake CCXT submit executor whose .execute returns a canned tentative outcome.
    Distinct from the reconciliation `stub` (the read-only query executor)."""
    fake = mock.Mock()
    fake.execute = mock.Mock(return_value={
        "ok": ok, "mode": mode, "exchange": "ccxt", "elapsed_ms": 5,
        "fill_summary": {"status": "submitted", "order_id": "ord-1", "client_order_id": None},
        "payload": {"symbol": "XRP/USDT:USDT"},
    })
    return fake


def _force_submit(wr, monkeypatch, *, stub, submit_executor=None):
    monkeypatch.setenv("HERMX_LIVE_TRADING", "1")
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", "1")
    # Submit goes through the CCXT executor (success => tentative FILLED); the
    # post-submit reconciliation then queries the read-only `stub`.
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda: stub)
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: submit_executor or _submit_executor())
    return wr.execute_if_enabled(_armed_record())


def test_post_submit_reconcile_writes_terminal_transition(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-abc0123456789de"
    stub = StubExecutor(order=norm_order("filled", cl_ord_id=cl, acc=10.0, avg=0.5))
    result = _force_submit(wr, monkeypatch, stub=stub)

    # Reconciliation enriched the execution result with a `reconcile` key.
    assert result["reconcile"]["state"] == wr.ORDER_STATE_FILLED
    assert result["reconcile"]["source"] == "get_order"

    # The order journal terminal record came from reconciliation (SUBMITTED->FILLED),
    # written authoritatively rather than from stdout.
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_FILLED]
    terminal = records[-1]
    assert terminal["prev_state"] == wr.ORDER_STATE_SUBMITTED
    assert terminal["detail"]["reconcile"]["state"] == wr.ORDER_STATE_FILLED
    assert wr.load_open_orders() == []  # FILLED is terminal


def test_post_submit_reconcile_mismatch_overrides_stdout_and_alerts(wr, monkeypatch):
    # stdout says success, but the EXCHANGE says canceled/zero-fill => REJECTED.
    cl = "mxc-xrpusdt-buy-abc0123456789de"
    stub = StubExecutor(order=norm_order("canceled", cl_ord_id=cl, acc=0.0))
    result = _force_submit(wr, monkeypatch, stub=stub)

    assert result["reconcile"]["state"] == wr.ORDER_STATE_REJECTED
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    assert records[-1]["state"] == wr.ORDER_STATE_REJECTED
    # A RECONCILE_MISMATCH alert was emitted (stdout FILLED vs reconciled REJECTED).
    alerts = wr.read_jsonl_tolerant(wr.ALERTS_LEDGER)
    mism = [a for a in alerts if a.get("kind") == "reconcile" and a["alert"] == "RECONCILE_MISMATCH" and a["detail"].get("stage") == "post_submit"]
    assert len(mism) == 1
    # The local tentative outcome of a bare ACK is now SUBMITTED (not an optimistic
    # FILLED); the exchange says REJECTED, which is still a genuine mismatch.
    assert mism[0]["detail"]["stdout_outcome"] == wr.ORDER_STATE_SUBMITTED
    assert mism[0]["detail"]["reconciled_outcome"] == wr.ORDER_STATE_REJECTED


def test_reconcile_disabled_uses_stdout_outcome(wr, monkeypatch):
    # HERMX_RECONCILE_ENABLED unset => legacy stdout-driven outcome, executor untouched.
    monkeypatch.setenv("HERMX_LIVE_TRADING", "1")
    monkeypatch.delenv("HERMX_RECONCILE_ENABLED", raising=False)
    exec_calls = []
    monkeypatch.setattr(wr, "_reconciliation_executor", lambda: exec_calls.append(1) or StubExecutor())
    monkeypatch.setattr(wr.ExecutorFactory, "create", lambda cfg, root: _submit_executor())

    result = wr.execute_if_enabled(_armed_record())
    assert "reconcile" not in result
    assert exec_calls == []  # no reconciliation executor constructed
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    # A bare ACK records SUBMITTED (from the write-ahead), not a terminal FILLED -- with
    # reconciliation disabled it stays SUBMITTED until a later resolver confirms the fill.
    assert records[-1]["state"] == wr.ORDER_STATE_SUBMITTED


# ---------------------------------------------------------------------------
# (g) startup bootstrap: open-order reconcile + position mismatch + flag.
# ---------------------------------------------------------------------------

def test_startup_reconcile_open_orders(wr, monkeypatch):
    cl = "mxc-xrpusdt-buy-startup00000001"
    # Seed an OPEN (SUBMITTED) order in the journal with a symbol/inst intent.
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)

    stub = StubExecutor(order=norm_order("filled", cl_ord_id=cl, acc=10.0, avg=0.5), positions=[])
    summary = wr.reconcile_startup(executor=stub)

    # Startup flag set for future enforcement.
    assert wr.RECONCILE_STARTUP_COMPLETE is True
    assert wr.RECONCILE_STARTUP_AT is not None

    # The open SUBMITTED order was reconciled to FILLED and the transition persisted.
    assert summary["open_orders"][0]["outcome"] == wr.ORDER_STATE_FILLED
    assert summary["open_orders"][0]["wrote_transition"] is True
    assert wr.load_open_orders() == []
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    assert records[-1]["state"] == wr.ORDER_STATE_FILLED
    assert records[-1]["detail"]["startup_reconcile"] is True

    # position_mismatches retains a backward-compatible (always-empty) shape now that
    # the paper/journal position comparison has been removed.
    assert summary["position_mismatches"] == []


def test_startup_reconcile_clean_match_no_alert(wr, monkeypatch):
    # Flat exchange + no open orders => clean, no mismatch (:236).
    stub = StubExecutor(positions=[])
    summary = wr.reconcile_startup(executor=stub)
    assert wr.RECONCILE_STARTUP_COMPLETE is True
    assert summary["open_orders"] == []
    assert summary["position_mismatches"] == []
    assert not wr.ALERTS_LEDGER.exists()


# ---------------------------------------------------------------------------
# (h) disabled config path is unchanged: not_submitted, no reconcile, no journal.
# ---------------------------------------------------------------------------

def test_disabled_config_no_reconcile_no_journal(wr, monkeypatch):
    # Even if the reconcile flag is set, the blocked gate (per-strategy submit flag off)
    # returns before any submit.
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", "1")

    def boom():
        raise AssertionError("reconciliation executor must not be built on the blocked path")

    monkeypatch.setattr(wr, "_reconciliation_executor", boom)
    with mock.patch.object(wr.ExecutorFactory, "create") as create_mock:
        result = wr.execute_if_enabled(_blocked_record())

    create_mock.assert_not_called()
    assert result["mode"] == "not_submitted"
    assert "reconcile" not in result
    assert not wr.ORDER_JOURNAL_LEDGER.exists()
    assert wr.load_open_orders() == []


# ---------------------------------------------------------------------------
# Reconciliation feature-flag gates: the three paths share ONE truthiness rule and
# differ ONLY by their documented default (post-submit OFF, periodic resolver ON).
# These lock in the shared-helper semantics so the gates can never silently drift.
# ---------------------------------------------------------------------------

def test_post_submit_gate_defaults_off(wr, monkeypatch):
    monkeypatch.delenv("HERMX_RECONCILE_ENABLED", raising=False)
    assert wr.reconcile_post_submit_enabled() is False


def test_unknown_resolver_gate_defaults_on(wr):
    # The resolver enable bool was merged into UNKNOWN_RESOLVER_INTERVAL_SECONDS
    # (> 0 => on). Default interval is positive, so the gate defaults ON.
    assert wr.unknown_resolver_enabled() is True


@pytest.mark.parametrize("interval,expected", [
    (30.0, True), (1.0, True), (0.0, False), (-5.0, False),
])
def test_unknown_resolver_gate_follows_interval(wr, monkeypatch, interval, expected):
    # INTERVAL_SECONDS <= 0 disables the resolver; any positive interval enables it.
    monkeypatch.setattr(wr, "UNKNOWN_RESOLVER_INTERVAL_SECONDS", interval)
    assert wr.unknown_resolver_enabled() is expected


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("enabled", True),
    ("false", False), ("0", False), ("no", False), ("", False), ("  FALSE  ", False),
])
def test_reconcile_gate_truthiness(wr, monkeypatch, value, expected):
    # The POST-SUBMIT reconcile gate still parses HERMX_RECONCILE_ENABLED from env.
    monkeypatch.setenv("HERMX_RECONCILE_ENABLED", value)
    assert wr.reconcile_post_submit_enabled() is expected


# ---------------------------------------------------------------------------
# OBSERVE-ONLY proof: reconciliation NEVER submits / cancels / auto-trades. The
# read-only query executor below raises if any state-mutating venue method is hit.
# ---------------------------------------------------------------------------

class ObserveOnlyStub(StubExecutor):
    """A read-only query executor that fails loudly if reconciliation ever invokes a
    state-mutating venue method (submit/cancel/create/place). Completing a test with
    this stub is itself the proof that the path is observe-only."""

    def _forbidden(self, *args, **kwargs):  # noqa: D401
        raise AssertionError("reconciliation invoked a state-mutating venue method")

    execute = _forbidden
    submit = _forbidden
    cancel = _forbidden
    cancel_order = _forbidden
    cancel_all = _forbidden
    create_order = _forbidden
    place_order = _forbidden


def _no_wait_backoff(wr, monkeypatch):
    """Inject a no-op sleep into reconcile_order_with_backoff so a live-forever order
    reaches the UNKNOWN deadline without the test waiting ~7.5s of real backoff."""
    real = wr.reconcile_order_with_backoff
    monkeypatch.setattr(
        wr,
        "reconcile_order_with_backoff",
        lambda executor, lookup, **kw: real(executor, lookup, sleep=lambda *_: None, **kw),
    )


def test_startup_reconcile_submitted_to_rejected_observe_only(wr):
    # An orphaned SUBMITTED order the venue reports as canceled/zero-fill => REJECTED.
    cl = "mxc-xrpusdt-buy-startup-reject01"
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)

    stub = ObserveOnlyStub(order=norm_order("canceled", cl_ord_id=cl, acc=0.0), positions=[])
    summary = wr.reconcile_startup(executor=stub)

    assert summary["open_orders"][0]["outcome"] == wr.ORDER_STATE_REJECTED
    assert summary["open_orders"][0]["wrote_transition"] is True
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_REJECTED]
    assert wr.load_open_orders() == []  # REJECTED is terminal


def test_post_submit_reconcile_is_observe_only(wr, monkeypatch):
    # The SUBMIT goes through the (separate) CCXT submit executor; the post-submit
    # RECONCILIATION executor is read-only and must never be asked to trade.
    cl = "mxc-xrpusdt-buy-abc0123456789de"
    stub = ObserveOnlyStub(order=norm_order("filled", cl_ord_id=cl, acc=10.0, avg=0.5))
    result = _force_submit(wr, monkeypatch, stub=stub)
    assert result["reconcile"]["state"] == wr.ORDER_STATE_FILLED  # completed w/o trading


def test_post_submit_reconcile_submitted_to_unknown(wr, monkeypatch):
    # Exchange order stays "live" through every retry => SUBMITTED -> UNKNOWN, the order
    # stays open for the periodic resolver, and a mismatch is surfaced.
    cl = "mxc-xrpusdt-buy-abc0123456789de"
    _no_wait_backoff(wr, monkeypatch)
    stub = StubExecutor(order=norm_order("live", cl_ord_id=cl))
    result = _force_submit(wr, monkeypatch, stub=stub)

    assert result["reconcile"]["state"] == wr.ORDER_STATE_UNKNOWN
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED, wr.ORDER_STATE_UNKNOWN]
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1 and open_orders[0]["state"] == wr.ORDER_STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Phase 5 prep — characterization of the remaining reconcile_order_once /
# map_order_outcome branches (REFACTOR_PLAN Phase 5: tests-first before the
# reconcile/ extraction). These lock in CURRENT behavior; they do not change it.
# ---------------------------------------------------------------------------

def test_live_order_maps_submitted_keep_polling(wr):
    # A non-terminal (live) order maps to SUBMITTED so the backoff keeps polling.
    ex = StubExecutor(order=norm_order("live"))
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_SUBMITTED
    assert out["partial"] is False
    assert out["reason"] == "live"
    assert out["source"] == "get_order"


def test_error_state_order_falls_through_chain_to_not_found(wr):
    # An unrecognized venue state (error/not_implemented/garbage) is not "present"
    # (_PRESENT_ORDER_STATES), so reconcile_order_once falls through the whole
    # fallback chain and maps UNKNOWN/not_found — never a terminal guess. (The
    # map_order_outcome "inconclusive:<state>" reason is only reachable by calling
    # the pure mapper directly; the chain filters non-present orders first.)
    ex = StubExecutor(order=norm_order("error"), pending=[], archive=[])
    out = wr.reconcile_order_once(ex, LOOKUP)
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert out["reason"] == "not_found"
    assert out["matched_order"] is None
    # The pure mapper, fed the same order directly, reports the inconclusive reason.
    state, _, reason = wr.map_order_outcome(norm_order("error"))
    assert (state, reason) == (wr.ORDER_STATE_UNKNOWN, "inconclusive:error")


def test_map_order_outcome_pure_branches(wr):
    # None (absent everywhere) -> UNKNOWN/not_found.
    assert wr.map_order_outcome(None) == (wr.ORDER_STATE_UNKNOWN, False, "not_found")
    # Empty state string -> inconclusive:empty.
    state, partial, reason = wr.map_order_outcome({"state": "", "acc_fill_sz": 0.0})
    assert (state, reason) == (wr.ORDER_STATE_UNKNOWN, "inconclusive:empty")
    # live + 0 < acc < ordered: still SUBMITTED (non-terminal), partial informs logging.
    state, partial, reason = wr.map_order_outcome(
        {"state": "live", "acc_fill_sz": 4.0}, ordered=10.0
    )
    assert (state, partial, reason) == (wr.ORDER_STATE_SUBMITTED, True, "live")
    # Exact fill (acc == ordered) is a full FILLED, not partial_by_size.
    state, partial, reason = wr.map_order_outcome(
        {"state": "filled", "acc_fill_sz": 10.0}, ordered=10.0
    )
    assert (state, partial, reason) == (wr.ORDER_STATE_FILLED, False, "filled")


def test_lookup_without_inst_id_never_queries_venue(wr):
    # No inst_id => every fallback stage is skipped (each is gated on inst_id) and the
    # outcome is UNKNOWN/not_found with the lookup's own ids echoed back.
    ex = StubExecutor(order=norm_order("filled", acc=10.0))
    out = wr.reconcile_order_once(ex, {"cl_ord_id": "cl-1", "ord_id": "ord-1"})
    assert ex.calls == []  # venue never touched
    assert out["state"] == wr.ORDER_STATE_UNKNOWN
    assert out["reason"] == "not_found"
    assert out["ord_id"] == "ord-1"
    assert out["cl_ord_id"] == "cl-1"


def test_pending_match_by_ord_id_only(wr):
    # A pending order is matched by ordId alone when the lookup has no clOrdId.
    ex = StubExecutor(
        order=norm_not_found(),
        pending=[norm_order("live", cl_ord_id="venue-generated", ord_id="ord-9")],
    )
    out = wr.reconcile_order_once(ex, {"inst_id": "XRP-USDT-SWAP", "ord_id": "ord-9"})
    assert out["source"] == "orders_pending"
    assert out["ord_id"] == "ord-9"
    assert out["state"] == wr.ORDER_STATE_SUBMITTED  # live -> keep polling


def test_keyless_lookup_accepts_first_present_order(wr):
    # With neither ordId nor clOrdId, the first PRESENT order for the instrument is
    # accepted (documented _order_matches fallback).
    ex = StubExecutor(
        order=norm_not_found(),
        pending=[norm_order("filled", cl_ord_id="whoever", ord_id="any", acc=10.0)],
    )
    out = wr.reconcile_order_once(ex, {"inst_id": "XRP-USDT-SWAP"})
    assert out["source"] == "orders_pending"
    assert out["state"] == wr.ORDER_STATE_FILLED


def test_history_limit_from_lookup_threaded_to_archive(wr):
    ex = StubExecutor(order=norm_not_found(), pending=[], archive=[])
    wr.reconcile_order_once(ex, {**LOOKUP, "history_limit": 7})
    archive_calls = [c for c in ex.calls if c[0] == "get_order_history_archive"]
    assert archive_calls == [("get_order_history_archive", "XRP-USDT-SWAP", 7)]


def test_emit_reconcile_alert_ledger_oserror_still_notifies_operator(wr, monkeypatch):
    # Log-and-continue on the observability path: a failed ledger append must not
    # swallow the operator transport (and must not raise into the reconcile loop).
    operator_calls = []
    monkeypatch.setattr(
        wr, "emit_operator_alert",
        lambda kind, detail, severity="warning": operator_calls.append((kind, severity)),
    )

    def boom(path, rec):
        raise OSError("disk full")

    monkeypatch.setattr(wr, "append_jsonl", boom)
    record = wr.emit_reconcile_alert(wr.RECONCILE_ALERT_MISMATCH, {"stage": "test"})
    assert record["kind"] == "reconcile"
    assert operator_calls == [(wr.RECONCILE_ALERT_MISMATCH, "warning")]


def test_startup_not_found_orphan_stays_unknown_not_rejected(wr):
    # MONEY-SAFETY at startup: an orphaned SUBMITTED order the venue cannot find is NOT
    # auto-rejected. It stays UNKNOWN + open so the periodic resolver keeps chasing it;
    # no terminal REJECTED is ever written from mere absence.
    cl = "mxc-xrpusdt-buy-startup-notfound"
    intent = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=intent, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=intent, prev_state=wr.ORDER_STATE_PLANNED)

    stub = ObserveOnlyStub(order=norm_not_found(), pending=[], archive=[], positions=[])
    summary = wr.reconcile_startup(executor=stub)

    assert summary["open_orders"][0]["outcome"] == wr.ORDER_STATE_UNKNOWN
    assert summary["open_orders"][0]["wrote_transition"] is False  # no terminal write
    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    states = [r["state"] for r in records if r["cl_ord_id"] == cl]
    assert wr.ORDER_STATE_REJECTED not in states
    assert states == [wr.ORDER_STATE_PLANNED, wr.ORDER_STATE_SUBMITTED]  # unchanged, still open
    open_orders = wr.load_open_orders()
    assert len(open_orders) == 1 and open_orders[0]["state"] == wr.ORDER_STATE_SUBMITTED


