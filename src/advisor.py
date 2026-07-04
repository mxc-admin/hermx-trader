"""Pre-execution advisor (Phase 6 extraction, REFACTOR_PLAN.md).

Houses the Phase 8 advisory / risk-gating cluster that used to live in
webhook_receiver.py: ``run_execution_advisor``, ``execute_with_advisor`` and
the helpers they exclusively use (``_advisor_state_snapshot``,
``_advisor_build_prompt``, ``_advisor_agent_query``, ``_advisor_parse``,
``ADVISOR_SYSTEM_PROMPT``).

The advisor is a SAFETY OVERSEER, never a trader. It sees the (already fully
determined) trade intent and may only return action="proceed" or "skip", plus
a free-text risk_note and an optional 0-100 score. It can NEVER change symbol,
side, size, leverage, or strategy -- those are locked in code upstream. When
enabled, a "skip" is a veto and blocks the trade.
Any timeout / transport error / malformed reply FAILS OPEN to deterministic
execution: the deterministic front door is never down because of the LLM.

Transport: the Hermes Agent run as a one-shot with our skills loaded
(`hermes -z "<prompt>" --skills hermx-control`). This runs the full agent loop
through Hermes (its configured provider + credentials) so the agent can use the
hermx-control skill to read the live local API before deciding -- it is NOT a
bare LLM passthrough.

Config-bound / reload-reset module state STAYS defined in webhook_receiver.py:
the HERMX_ADVISOR_* constants (resolved from the engine-config "advisor" block
at wr import time; kept as wr module constants so tests can
``monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED", ...)`` /
``"HERMX_ADVISOR_COMMAND"``), the ``_advisor_agent_query`` transport seam
(rebound on wr by test_phase8_advisor / test_phase6_advisor_execute),
``execute_if_enabled`` (execution-service glue, rebound on wr by
test_phase6_advisor_execute), ``record_pipeline_event`` (rebound on wr by
test_phase6_advisor_execute's ledger-guard proofs) and ``_signal_id_of``. The
moved functions therefore read all of them lazily via ``import
webhook_receiver as _wr`` -- the same pattern as src/alerts.py (Phase 0),
src/signals/ (Phase 1), src/control_state.py (Phase 2), src/strategy/
(Phase 3) and src/orders/journal.py (Phase 4). webhook_receiver re-exports
every moved name so ``wr.<fn>`` call sites and monkeypatch seams keep working.

The leaf-pure collaborator ``strategy_budget_usd`` is imported directly from
its home (strategy.records, Phase 3).
"""
from __future__ import annotations

import json
import logging
import subprocess
import time

from strategy.records import strategy_budget_usd

ADVISOR_SYSTEM_PROMPT = (
    "You are HermX's pre-execution risk overseer. You are given a trading signal "
    "whose symbol, side, size, leverage and strategy are ALREADY FIXED by code and "
    "a sanctioned strategy file. You cannot change any of them. Your ONLY job is to "
    "decide whether this already-sanctioned trade should still be allowed to "
    "execute right now, or skipped on risk grounds. "
    "Respond with STRICT JSON only, no prose, no code fences, exactly: "
    '{"action": "proceed" | "skip", "risk_note": "<short reason>", "score": <0-100 risk score>}. '
    "Default to \"proceed\" unless you see a concrete, specific risk. Never invent "
    "sizes or prices."
)


def _advisor_state_snapshot(record: dict) -> dict:
    """Minimal, read-only context the advisor reasons over. Intentionally small:
    the trade intent + the sanctioned strategy params + planned notional. Sizing
    is shown for context ONLY; the advisor cannot alter it."""
    normalized = record.get("normalized") or {}
    strategy = record.get("strategy_config") or {}
    readiness = record.get("execution_readiness") or {}
    intent = readiness.get("execution_intent") or {}
    return {
        "symbol": normalized.get("symbol"),
        "side": normalized.get("side"),
        "timeframe": normalized.get("timeframe"),
        "signal_price": normalized.get("tv_signal_price"),
        "strategy_id": normalized.get("strategy_id"),
        "budget_usd": strategy_budget_usd(strategy),
        "leverage": strategy.get("leverage"),
        "planned_notional_usd": intent.get("planned_notional_usd") or readiness.get("planned_notional_usd"),
        "live_execution_enabled": readiness.get("live_execution_enabled"),
    }


def _advisor_build_prompt(record: dict) -> str:
    snapshot = _advisor_state_snapshot(record)
    return (
        ADVISOR_SYSTEM_PROMPT
        + "\n\nYou may use the hermx-control skill to read current positions, PnL and "
        "arm state from the local API before deciding.\n"
        "Trade intent (FIXED, do not change):\n"
        + json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        + "\n\nOutput ONLY the strict JSON object."
    )


def _advisor_agent_query(prompt: str) -> str:
    """Transport seam (monkeypatched in tests). Runs the Hermes Agent as a one-shot
    with our skills loaded and returns its stdout (ONLY the agent's response).
    Raises on a missing binary / non-zero exit / timeout so the caller fails open.
    This goes THROUGH Hermes (its configured provider + skills), not a bare LLM."""
    import webhook_receiver as _wr
    cmd = [_wr.HERMX_ADVISOR_COMMAND, "-z", prompt, "--skills", _wr.HERMX_ADVISOR_SKILLS]
    if _wr.HERMX_ADVISOR_MODEL:
        cmd += ["-m", _wr.HERMX_ADVISOR_MODEL]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_wr.HERMX_ADVISOR_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"hermes one-shot exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def _advisor_parse(content: str) -> dict:
    """Tolerant strict-JSON parse of the advisor reply. Accepts a bare JSON object
    or one embedded in surrounding text/code fences. Raises if no valid object or
    if ``action`` is not one of proceed/skip."""
    text = (content or "").strip()
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("advisor reply is not a JSON object")
    action = str(obj.get("action") or "").strip().lower()
    if action not in {"proceed", "skip"}:
        raise ValueError(f"advisor action invalid: {action!r}")
    score = obj.get("score")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "action": action,
        "risk_note": str(obj.get("risk_note") or "")[:500],
        "score": score,
    }


def run_execution_advisor(record: dict) -> "dict | None":
    """Consult the pre-execution advisor. Returns None when disabled (caller then
    behaves byte-identically to before). Otherwise returns a decision dict that
    ALWAYS includes ``veto_applied`` (bool). FAILS OPEN (veto_applied=False) on any
    error so a down/slow/garbage LLM can never block a sanctioned trade."""
    import webhook_receiver as _wr
    if not _wr.HERMX_ADVISOR_ENABLED:
        return None
    started = time.monotonic()
    decision = {
        "enabled": True,
        "ok": False,
        "action": "proceed",
        "risk_note": "",
        "score": None,
        "veto_applied": False,
        "model": _wr.HERMX_ADVISOR_MODEL or "(hermes default)",
        "skills": _wr.HERMX_ADVISOR_SKILLS,
    }
    try:
        parsed = _advisor_parse(_wr._advisor_agent_query(_advisor_build_prompt(record)))
        decision.update(ok=True, action=parsed["action"], risk_note=parsed["risk_note"], score=parsed["score"])
        decision["veto_applied"] = bool(parsed["action"] == "skip")
    except Exception as exc:  # fail OPEN -> proceed deterministically
        decision["error"] = str(exc)[:300]
        logging.warning("execution advisor failed open (proceeding): %s", exc)
    decision["latency_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    try:
        _wr.record_pipeline_event("advisor", _wr._signal_id_of(record), {"received_at": record.get("received_at"), "advisor": decision, "snapshot": _advisor_state_snapshot(record)})
    except Exception as exc:  # advisory logging must never block execution
        logging.warning("advisor ledger append failed: %s", exc)
    return decision


def execute_with_advisor(record: dict) -> dict:
    """Single wrapper used by the execution paths: consult the advisor, honor a
    veto if granted, otherwise delegate to the authoritative submission path. With
    the advisor disabled (default) this is exactly ``execute_if_enabled``."""
    import webhook_receiver as _wr
    decision = run_execution_advisor(record)
    if decision is not None:
        record["advisor"] = decision
        if decision.get("veto_applied"):
            result = {
                "ok": True,
                "mode": "not_submitted",
                "reason": "vetoed_by_advisor",
                "advisor": {"risk_note": decision.get("risk_note"), "score": decision.get("score")},
            }
            try:
                _wr.record_pipeline_event("execution", _wr._signal_id_of(record), {"received_at": record.get("received_at"), "okx_execution": result})
            except Exception as exc:  # pipeline ledger is observability; must never block the veto outcome
                logging.warning("execution ledger append failed: %s", exc)
            return result
    return _wr.execute_if_enabled(record)
