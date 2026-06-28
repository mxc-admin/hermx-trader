"""Phase 8 -- pre-execution advisor (Hermes/LLM overseer).

The advisor is OFF by default and, when on, can only VETO (skip) or annotate a
trade whose symbol/side/size/leverage/strategy are ALREADY fixed in code. It can
never change them. Veto power is a second, separately-off switch. Any timeout /
transport error / malformed reply FAILS OPEN to deterministic execution.

These tests monkeypatch the transport seam (``_advisor_agent_query``, which shells
out to ``hermes -z --skills hermx-control``) so no real agent is ever invoked, and
flip the module-level advisor globals on the reloaded ``wr`` module per case.
"""

from conftest import load_alert

RECEIVED_AT = "2026-06-24T00:00:00Z"
ALERT = "strategy/btcusdt_buy.json"


def _enable(wr, monkeypatch, *, allow_veto: bool):
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", True)
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ALLOW_VETO", allow_veto)


# --- default OFF: byte-identical to pre-Phase-8 -----------------------------

def test_advisor_disabled_by_default_no_annotation(wr):
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    # Disabled advisor never runs, never annotates, never vetoes.
    assert "advisor" not in record
    assert record["okx_execution"]["reason"] != "vetoed_by_advisor"


def test_run_execution_advisor_returns_none_when_disabled(wr, monkeypatch):
    monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", False)
    assert wr.run_execution_advisor({"normalized": {}}) is None


# --- veto path (enabled + allow_veto) ---------------------------------------

def test_advisor_skip_with_veto_blocks_execution(wr, monkeypatch):
    _enable(wr, monkeypatch, allow_veto=True)
    monkeypatch.setattr(
        wr, "_advisor_agent_query",
        lambda prompt: '{"action": "skip", "risk_note": "elevated risk", "score": 88}',
    )
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    assert record["okx_execution"]["reason"] == "vetoed_by_advisor"
    assert record["okx_execution"]["mode"] == "not_submitted"
    assert record["advisor"]["action"] == "skip"
    assert record["advisor"]["veto_applied"] is True
    assert record["advisor"]["risk_note"] == "elevated risk"
    assert record["advisor"]["score"] == 88


def test_advisor_skip_writes_advisor_ledger(wr, wr_root, monkeypatch):
    _enable(wr, monkeypatch, allow_veto=True)
    monkeypatch.setattr(
        wr, "_advisor_agent_query",
        lambda prompt: '{"action": "skip", "risk_note": "x", "score": 50}',
    )
    wr.build_record(load_alert(ALERT), RECEIVED_AT)
    ledger = wr_root / "logs" / "advisor-decisions.jsonl"
    assert ledger.exists()
    assert "vetoed" not in ledger.read_text(encoding="utf-8")  # ledger stores the decision, not the exec result
    assert '"action": "skip"' in ledger.read_text(encoding="utf-8") or '"action":"skip"' in ledger.read_text(encoding="utf-8")


# --- proceed path -----------------------------------------------------------

def test_advisor_proceed_executes_normally(wr, monkeypatch):
    _enable(wr, monkeypatch, allow_veto=True)
    monkeypatch.setattr(
        wr, "_advisor_agent_query",
        lambda prompt: '{"action": "proceed", "risk_note": "looks fine", "score": 10}',
    )
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    assert record["okx_execution"]["reason"] != "vetoed_by_advisor"
    assert record["advisor"]["action"] == "proceed"
    assert record["advisor"]["veto_applied"] is False


# --- annotate-only: skip but veto power NOT granted -------------------------

def test_advisor_skip_without_veto_is_annotate_only(wr, monkeypatch):
    _enable(wr, monkeypatch, allow_veto=False)
    monkeypatch.setattr(
        wr, "_advisor_agent_query",
        lambda prompt: '{"action": "skip", "risk_note": "would veto if allowed", "score": 95}',
    )
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    # Decision recorded, but execution is NOT blocked (veto not granted).
    assert record["advisor"]["action"] == "skip"
    assert record["advisor"]["veto_applied"] is False
    assert record["okx_execution"]["reason"] != "vetoed_by_advisor"


# --- fail OPEN: any LLM failure proceeds deterministically ------------------

def test_advisor_timeout_fails_open(wr, monkeypatch):
    _enable(wr, monkeypatch, allow_veto=True)

    def _boom(prompt):
        raise TimeoutError("agent timed out")

    monkeypatch.setattr(wr, "_advisor_agent_query", _boom)
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    assert record["advisor"]["ok"] is False
    assert record["advisor"]["action"] == "proceed"
    assert record["advisor"]["veto_applied"] is False
    assert "error" in record["advisor"]
    assert record["okx_execution"]["reason"] != "vetoed_by_advisor"


def test_advisor_malformed_reply_fails_open(wr, monkeypatch):
    _enable(wr, monkeypatch, allow_veto=True)
    monkeypatch.setattr(wr, "_advisor_agent_query", lambda prompt: "not json at all")
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    assert record["advisor"]["ok"] is False
    assert record["advisor"]["veto_applied"] is False
    assert record["okx_execution"]["reason"] != "vetoed_by_advisor"


def test_advisor_missing_hermes_binary_fails_open(wr, monkeypatch):
    # Real transport seam, but the hermes binary does not exist -> FileNotFoundError
    # -> fails open to deterministic execution (the agent is never the front door).
    _enable(wr, monkeypatch, allow_veto=True)
    monkeypatch.setattr(wr, "HERMX_ADVISOR_COMMAND", "hermes-nonexistent-xyz-123")
    status, record = wr.build_record(load_alert(ALERT), RECEIVED_AT)
    assert status == 200
    assert record["advisor"]["ok"] is False
    assert "error" in record["advisor"]
    assert record["okx_execution"]["reason"] != "vetoed_by_advisor"


# --- parser unit tests ------------------------------------------------------

def test_advisor_parse_bare_json(wr):
    out = wr._advisor_parse('{"action": "skip", "risk_note": "r", "score": 7}')
    assert out == {"action": "skip", "risk_note": "r", "score": 7}


def test_advisor_parse_embedded_in_fences(wr):
    raw = 'Here is my answer:\n```json\n{"action": "proceed", "risk_note": "ok", "score": 3}\n```\n'
    out = wr._advisor_parse(raw)
    assert out["action"] == "proceed"
    assert out["score"] == 3


def test_advisor_parse_invalid_action_raises(wr):
    import pytest

    with pytest.raises(ValueError):
        wr._advisor_parse('{"action": "buy", "risk_note": "x"}')


def test_advisor_parse_non_object_raises(wr):
    import pytest

    with pytest.raises((ValueError, Exception)):
        wr._advisor_parse("totally not json")


def test_advisor_parse_missing_score_is_none(wr):
    out = wr._advisor_parse('{"action": "proceed", "risk_note": "x"}')
    assert out["score"] is None
