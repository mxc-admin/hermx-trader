"""Tests for GET /api/admin/order-troubleshoot and POST
/api/admin/order-journal/apply-action in webhook_receiver.py.

Mirrors tests/test_operator_close.py's Handler-construction pattern (bypasses the
socket-bound BaseHTTPRequestHandler.__init__, stubs request inputs, captures _send).
"""
from __future__ import annotations

import io
import json

_INTENT = {"symbol": "XRPUSDT", "side": "buy", "inst_id": "XRP-USDT-SWAP", "planned_notional_usd": 1500.0, "policy": "weighted_v1"}


def _make_handler(wr, *, token=None, body=None, raw=None, content_length=None):
    handler = wr.Handler.__new__(wr.Handler)
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-Dashboard-Token"] = token
    if raw is None:
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
    headers["Content-Length"] = str(len(raw) if content_length is None else content_length)
    handler.headers = headers
    handler.rfile = io.BytesIO(raw)
    captured: list[tuple[int, dict]] = []
    handler._send = lambda status, payload: captured.append((status, payload))
    return handler, captured


def _seed_corrupted_unknown(wr, cl: str) -> None:
    """Same pre-fix-corruption simulation as test_order_troubleshoot.py -- direct
    append bypassing record_order_state's stale-write guard."""
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=_INTENT, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=_INTENT, prev_state=wr.ORDER_STATE_PLANNED)
    wr.record_order_state(cl, wr.ORDER_STATE_REJECTED, intent=_INTENT, prev_state=wr.ORDER_STATE_SUBMITTED)
    seq = wr._order_journal_next_seq()
    wr.append_jsonl_durable(wr.ORDER_JOURNAL_LEDGER, {
        "schema_version": wr.ORDER_JOURNAL_SCHEMA_VERSION,
        "seq": seq,
        "ts": "2026-01-01T00:00:01Z",
        "cl_ord_id": cl,
        "state": wr.ORDER_STATE_UNKNOWN,
        "prev_state": wr.ORDER_STATE_SUBMITTED,
        "intent": _INTENT,
        "detail": {"unknown_resolver": True, "simulated_corruption": True},
    })
    wr._order_journal_index = None


# ---------------------------------------------------------------------------
# GET /api/admin/order-troubleshoot
# ---------------------------------------------------------------------------

def test_troubleshoot_report_rejects_missing_token(wr):
    handler, captured = _make_handler(wr, token=None)
    handler._handle_order_troubleshoot_report()
    status, payload = captured[-1]
    assert status == 401 and payload["ok"] is False


def test_troubleshoot_report_rejects_wrong_token(wr):
    handler, captured = _make_handler(wr, token="not-the-secret")
    handler._handle_order_troubleshoot_report()
    status, payload = captured[-1]
    assert status == 401 and payload["ok"] is False


def test_troubleshoot_report_lists_corrupted_order(wr):
    cl = "mxc-xrpusdt-buy-endpoint-report"
    _seed_corrupted_unknown(wr, cl)

    handler, captured = _make_handler(wr, token=wr.SECRET)
    handler._handle_order_troubleshoot_report()
    status, payload = captured[-1]

    assert status == 200 and payload["ok"] is True
    by_cl = {r["cl_ord_id"]: r for r in payload["results"]}
    assert cl in by_cl
    assert by_cl[cl]["issue_type"] == "terminal_overwritten"
    assert by_cl[cl]["actions"] == [{"id": "restore_terminal", "label": "Restore to REJECTED"}]


# ---------------------------------------------------------------------------
# POST /api/admin/order-journal/apply-action
# ---------------------------------------------------------------------------

def test_apply_action_rejects_missing_token(wr):
    handler, captured = _make_handler(wr, token=None, body={"cl_ord_id": "x", "action_id": "restore_terminal"})
    handler._handle_order_journal_apply_action()
    status, payload = captured[-1]
    assert status == 401 and payload["ok"] is False


def test_apply_action_requires_cl_ord_id(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"action_id": "restore_terminal"})
    handler._handle_order_journal_apply_action()
    status, payload = captured[-1]
    assert status == 400 and payload["error"] == "missing_cl_ord_id"


def test_apply_action_requires_action_id(wr):
    handler, captured = _make_handler(wr, token=wr.SECRET, body={"cl_ord_id": "x"})
    handler._handle_order_journal_apply_action()
    status, payload = captured[-1]
    assert status == 400 and payload["error"] == "missing_action_id"


def test_apply_action_refuses_when_not_eligible(wr):
    # A normal, uncorrupted SUBMITTED order -- run_classifiers finds nothing.
    cl = "mxc-xrpusdt-buy-not-eligible"
    wr.record_order_state(cl, wr.ORDER_STATE_PLANNED, intent=_INTENT, prev_state=None)
    wr.record_order_state(cl, wr.ORDER_STATE_SUBMITTED, intent=_INTENT, prev_state=wr.ORDER_STATE_PLANNED)

    handler, captured = _make_handler(wr, token=wr.SECRET, body={"cl_ord_id": cl, "action_id": "restore_terminal"})
    handler._handle_order_journal_apply_action()
    status, payload = captured[-1]

    assert status == 200
    assert payload["outcome"] == "refused"
    assert payload["reason"] == "action_not_currently_eligible"


def test_apply_action_refuses_unrequested_action_id_even_when_order_is_eligible(wr):
    # The server never trusts a client-supplied action beyond what it independently
    # computed as eligible -- an unrecognized action_id is refused even though the
    # order itself DOES have an eligible action (restore_terminal).
    cl = "mxc-xrpusdt-buy-wrong-action"
    _seed_corrupted_unknown(wr, cl)

    handler, captured = _make_handler(wr, token=wr.SECRET, body={"cl_ord_id": cl, "action_id": "delete_order"})
    handler._handle_order_journal_apply_action()
    status, payload = captured[-1]

    assert status == 200
    assert payload["outcome"] == "refused"
    assert payload["reason"] == "action_not_currently_eligible"
    assert wr.latest_order_record(cl)["state"] == wr.ORDER_STATE_UNKNOWN  # untouched


def test_apply_action_heals_and_is_idempotent(wr):
    cl = "mxc-xrpusdt-buy-heal-success"
    _seed_corrupted_unknown(wr, cl)

    handler, captured = _make_handler(wr, token=wr.SECRET, body={
        "cl_ord_id": cl, "action_id": "restore_terminal", "operator": "test-operator", "reason": "test-heal",
    })
    handler._handle_order_journal_apply_action()
    status, payload = captured[-1]

    assert status == 200
    assert payload["outcome"] == "healed"
    assert payload["from_state"] == wr.ORDER_STATE_UNKNOWN
    assert payload["to_state"] == wr.ORDER_STATE_REJECTED
    assert wr.latest_order_record(cl)["state"] == wr.ORDER_STATE_REJECTED

    records = wr.read_jsonl_tolerant(wr.ORDER_JOURNAL_LEDGER)
    healed = [r for r in records if r["cl_ord_id"] == cl][-1]
    assert healed["detail"]["troubleshoot_heal"] is True
    assert healed["detail"]["operator"] == "test-operator"

    # Idempotent: a second attempt finds no issue (the order is now legitimately terminal).
    handler2, captured2 = _make_handler(wr, token=wr.SECRET, body={"cl_ord_id": cl, "action_id": "restore_terminal"})
    handler2._handle_order_journal_apply_action()
    status2, payload2 = captured2[-1]
    assert status2 == 200 and payload2["outcome"] == "refused"
