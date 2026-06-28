"""Pure strategy-decision math extracted verbatim from webhook_receiver.py.

Behavior-preserving move. These functions are STATELESS: they reference only
their own peers, small pure helpers (as_float, fmt_float), static label
constants, and stdlib. They MUST NOT import webhook_receiver (that module binds
CONFIG-derived globals at import time and is reloaded by the test suite).
"""
from __future__ import annotations


POLICY_LABELS = {
    "duo_raw": "Duo Full",
    "v52_fast_1h": "30m+1H Candidate",
    "v6_regime_duo": "Regime Duo",
    "duo_conviction_sized": "Duo Conviction Sized",
    "conviction_v2_candidate": "Conviction V2 Candidate",
    "duo_regime_rsi_sized": "Duo Regime RSI Sized",
    "duo_regime_rsi_30m": "Duo Regime RSI 30m",
    "v3_r75": "Legacy V3 R75",
    "v5_mtf": "Legacy V5 MTF",
    "v51_balanced": "Legacy V5.1",
}


def as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def regime_from_acc(acc):
    if acc is None:
        return "unknown"
    if acc >= 10:
        return "BULL_STRONG"
    if acc > 2:
        return "BULL"
    if acc <= -10:
        return "BEAR_STRONG"
    if acc < -2:
        return "BEAR"
    return "TRANSITION"


def phase_from_acc_vel(acc, vel):
    if acc is None or vel is None:
        return "unknown"
    if vel >= 0 and acc >= 0:
        return "Q1_RISE"
    if vel >= 0 and acc < 0:
        return "Q2_TOP"
    if vel < 0 and acc < 0:
        return "Q3_DROP"
    return "Q4_BASE"


def pulse_label(values, direction: str):
    bkt = as_float(values.get("pulse_bkt"))
    rs = as_float(values.get("pulse_rs_mkt"))
    score = 0.0
    if bkt is not None:
        score += 25 if bkt >= 65 else (-20 if bkt < 50 else 0)
    if rs is not None:
        if direction == "long":
            score += 10 if rs > -20 else -15
        else:
            score += 10 if rs < 30 else -15
    if score >= 35:
        return "PULSE_STRONG"
    if score >= 15:
        return "PULSE_OK"
    if score <= -20:
        return "PULSE_WEAK"
    return "PULSE_MIXED"


def risk_on(values, direction: str):
    acc = as_float(values.get("pp_acc"))
    vel = as_float(values.get("pp_vel"))
    if acc is None or vel is None:
        return None
    bkt = as_float(values.get("pulse_bkt"))
    rs = as_float(values.get("pulse_rs_mkt"))
    sign = 1 if direction == "long" else -1
    score = max(-25, min(25, acc * sign)) * 1.4
    score += max(-20, min(20, vel * sign))
    if bkt is not None:
        score += (bkt - 50) * 0.35
    if rs is not None:
        score += max(-40, min(40, rs * sign)) * 0.35
    return round(max(0.0, min(100.0, 50.0 + score)), 2)


def no_pulse_score(values, direction: str):
    acc = as_float((values or {}).get("pp_acc"))
    vel = as_float((values or {}).get("pp_vel"))
    if acc is None or vel is None:
        return None
    sign = 1 if direction == "long" else -1
    score = 50.0
    score += max(-25, min(25, acc * sign)) * 1.55
    score += max(-20, min(20, vel * sign)) * 0.85
    return round(max(0.0, min(100.0, score)), 2)


def extract_jrsx(values: dict | None):
    for study in (values or {}).get("raw_studies") or []:
        name = str(study.get("name") or "").lower()
        if "jrsx" in name or "rsi" in name:
            last = study.get("last") or {}
            raw_values = last.get("values") or {}
            for key in ("plot_0", "plot_1"):
                val = as_float(raw_values.get(key))
                if val is not None and 0 <= val <= 100:
                    return val
    return None


def rsi_caution(direction: str, rsi):
    rsi = as_float(rsi)
    if rsi is None:
        return "RSI_UNKNOWN"
    if direction == "long":
        if rsi >= 80:
            return "RSI_OVERHEATED"
        if rsi <= 25:
            return "RSI_RECOVERY_ZONE"
    else:
        if rsi <= 20:
            return "RSI_OVERSOLD_SHORT_CAUTION"
        if rsi >= 70:
            return "RSI_DISTRIBUTION_ZONE"
    return "RSI_NEUTRAL"


def base_context(normalized: dict, values: dict | None) -> dict:
    if not values:
        return {
            "available": False,
            "direction": "long" if normalized["side"] == "buy" else "short",
            "regime": "unknown",
            "phase": "unknown",
            "pulse": "unknown",
            "risk_on": None,
            "no_pulse_score": None,
            "jrsx": None,
            "rsi_caution": "RSI_UNKNOWN",
        }
    direction = "long" if normalized["side"] == "buy" else "short"
    acc = as_float(values.get("pp_acc"))
    vel = as_float(values.get("pp_vel"))
    jrsx = extract_jrsx(values)
    return {
        "available": acc is not None and vel is not None,
        "direction": direction,
        "regime": regime_from_acc(acc),
        "phase": phase_from_acc_vel(acc, vel),
        "pulse": pulse_label(values, direction),
        "risk_on": risk_on(values, direction),
        "no_pulse_score": no_pulse_score(values, direction),
        "jrsx": jrsx,
        "rsi_caution": rsi_caution(direction, jrsx),
        "pp_acc": acc,
        "pp_vel": vel,
        "pulse_bkt": as_float(values.get("pulse_bkt")),
        "pulse_rs_mkt": as_float(values.get("pulse_rs_mkt")),
    }


def policy_result(name, decision, risk_weight, leverage, reasons, **extra):
    out = {
        "name": name,
        "decision": decision,
        "risk_weight": risk_weight,
        "leverage": leverage,
        "reasons": reasons,
    }
    out.update(extra)
    return out


def decide_duo_raw(normalized: dict, ctx: dict) -> dict:
    direction = "LONG" if normalized["side"] == "buy" else "SHORT"
    return policy_result(
        "duo_raw",
        "TRADE",
        1.0,
        1.0,
        [f"Duo Crypto {normalized['side'].upper()} opens/reverses {direction}", "Baseline only; no MXC intelligence filter"],
        action="FULL_ENTRY",
        target_direction=direction,
    )


def direction_confirms(ctx: dict) -> bool:
    if not ctx.get("available"):
        return False
    if ctx["direction"] == "long":
        return str(ctx["regime"]).startswith("BULL") and ctx["phase"] in {"Q1_RISE", "Q4_BASE"}
    return str(ctx["regime"]).startswith("BEAR") and ctx["phase"] in {"Q2_TOP", "Q3_DROP"}


def decide_v3_r75(ctx: dict) -> dict:
    if not ctx.get("available"):
        return policy_result("v3_r75", "SKIP", 0, 0, ["No live MXC CDP values available"])
    confirms = direction_confirms(ctx)
    ro = ctx.get("risk_on")
    pulse = ctx.get("pulse")
    reasons = [f"30m context: regime={ctx['regime']}, phase={ctx['phase']}, pulse={pulse}, risk_on={ro}"]
    if confirms and ro is not None and ro >= 75 and pulse in {"PULSE_OK", "PULSE_STRONG"}:
        reasons.append("R75-style recovery: direction confirmed and risk_on >= 75")
        return policy_result("v3_r75", "REDUCE", 0.5, 1.0, reasons, action="PARTIAL_ENTRY", confidence="medium", approximation="vps_30m_only")
    if confirms and ro is not None and ro >= 60:
        reasons.append("Directional context exists but below R75 conviction")
        return policy_result("v3_r75", "REDUCE_SMALL", 0.25, 1.0, reasons, action="SMALL_ENTRY", confidence="low", approximation="vps_30m_only")
    reasons.append("No base 30m edge")
    return policy_result("v3_r75", "SKIP", 0, 0, reasons, action="SKIP", confidence="none", approximation="vps_30m_only")


def step_weight(weight: float, delta: float) -> float:
    levels = [0.0, 0.25, 0.5, 0.75, 1.0]
    target = max(0.0, min(1.0, weight + delta))
    return min(levels, key=lambda x: abs(x - target))


def convert_weight_policy(name: str, weight: float, notes: list[str], *, mtf_status: str, mtf_summary: dict | None = None) -> dict:
    if weight >= 0.9:
        decision, leverage, action = "TRADE", 1.0, "FULL_ENTRY"
    elif weight >= 0.2:
        decision, leverage, action = "REDUCE", 1.0, "PARTIAL_ENTRY"
    else:
        decision, leverage, action = "SKIP", 0.0, "SKIP"
    extra = {"action": action, "mtf_status": mtf_status}
    if mtf_summary is not None:
        extra["mtf_summary"] = mtf_summary
    return policy_result(name, decision, weight, leverage, notes, **extra)


def mtf_summary(ctxs: dict, direction: str) -> dict:
    available = {tf: ctx for tf, ctx in ctxs.items() if ctx.get("available") and ctx.get("risk_on") is not None}
    confirms = [tf for tf, ctx in available.items() if direction_confirms(ctx)]
    opposes = []
    for tf, ctx in available.items():
        regime = str(ctx.get("regime", ""))
        phase = ctx.get("phase")
        if direction == "long" and regime.startswith("BEAR") and phase in {"Q2_TOP", "Q3_DROP"}:
            opposes.append(tf)
        if direction == "short" and regime.startswith("BULL") and phase in {"Q1_RISE", "Q4_BASE"}:
            opposes.append(tf)
    risks = [float(ctx.get("risk_on")) for ctx in available.values() if ctx.get("risk_on") is not None]
    avg_risk = round(sum(risks) / len(risks), 2) if risks else None
    detail = {
        tf: {
            "available": ctx.get("available"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "risk_on": ctx.get("risk_on"),
            "pulse": ctx.get("pulse"),
            "confirms": direction_confirms(ctx),
        }
        for tf, ctx in ctxs.items()
    }
    return {
        "available_count": len(available),
        "confirm_count": len(confirms),
        "oppose_count": len(opposes),
        "confirm_timeframes": confirms,
        "oppose_timeframes": opposes,
        "avg_risk_on": avg_risk,
        "detail": detail,
    }


def context_opposes(ctx: dict) -> bool:
    if not ctx.get("available"):
        return False
    direction = ctx.get("direction", "long")
    regime = str(ctx.get("regime", ""))
    phase = ctx.get("phase")
    if direction == "long":
        return regime.startswith("BEAR") and phase in {"Q2_TOP", "Q3_DROP"}
    return regime.startswith("BULL") and phase in {"Q1_RISE", "Q4_BASE"}


def single_tf_summary(tf: str, ctx: dict) -> dict:
    available = bool(ctx.get("available") and ctx.get("risk_on") is not None)
    confirms = available and direction_confirms(ctx)
    opposes = available and context_opposes(ctx)
    risk = ctx.get("risk_on") if available else None
    return {
        "available_count": 1 if available else 0,
        "confirm_count": 1 if confirms else 0,
        "oppose_count": 1 if opposes else 0,
        "confirm_timeframes": [tf] if confirms else [],
        "oppose_timeframes": [tf] if opposes else [],
        "avg_risk_on": risk,
        "detail": {
            tf: {
                "available": ctx.get("available"),
                "regime": ctx.get("regime"),
                "phase": ctx.get("phase"),
                "risk_on": ctx.get("risk_on"),
                "pulse": ctx.get("pulse"),
                "confirms": bool(confirms),
                "opposes": bool(opposes),
            }
        },
    }


def decide_v52_fast_1h(ctx: dict, v3: dict, mtf_ctxs: dict) -> dict:
    name = "v52_fast_1h"
    base_weight = float(v3.get("risk_weight") or 0)
    one_h = (mtf_ctxs or {}).get("1h") or {}
    summary = single_tf_summary("1h", one_h)
    available = summary["available_count"] == 1
    confirms = summary["confirm_count"] == 1
    opposes = summary["oppose_count"] == 1
    one_h_risk = summary["avg_risk_on"]
    risk_30m = ctx.get("risk_on") or 0
    notes = [
        f"base 30m conviction weight={base_weight:.2f}",
        f"Fast MTF 30m+1H: available={available}, confirms={confirms}, opposes={opposes}, 1H_risk={one_h_risk}",
    ]
    weight = base_weight
    status = "ready" if available else "pending"

    if not available:
        weight = step_weight(base_weight, -0.25) if base_weight > 0 else 0.0
        notes.append("1H context unavailable; V5.2 reduces because fast confirmation is missing")
        return convert_weight_policy(name, weight, notes, mtf_status=status, mtf_summary=summary)

    if base_weight > 0:
        if opposes:
            weight = step_weight(base_weight, -0.25)
            if base_weight <= 0.25 and risk_30m < 65:
                weight = 0.0
            notes.append("1H opposes the 30m signal; V5.2 cuts risk")
        elif confirms and (one_h_risk or 0) >= 65 and risk_30m >= 60:
            weight = step_weight(base_weight, 0.25)
            notes.append("1H confirms and risk is healthy; V5.2 upgrades one step")
        elif confirms:
            notes.append("1H confirms but not enough to upgrade; 30m+1H keeps base size")
        else:
            notes.append("1H is neutral; 30m+1H keeps base size without upgrade")
    else:
        if direction_confirms(ctx) and confirms and risk_30m >= 75 and (one_h_risk or 0) >= 65 and ctx.get("pulse") in {"PULSE_OK", "PULSE_STRONG"}:
            weight = 0.25
            notes.append("Fast recovery: 30m and 1H align strongly enough for a small probe")
        else:
            weight = 0.0
            notes.append("No base 30m edge and fast 1H filter does not justify recovery")
    return convert_weight_policy(name, weight, notes, mtf_status=status, mtf_summary=summary)


def decide_v6_regime_duo(ctx: dict, mtf_ctxs: dict) -> dict:
    name = "v6_regime_duo"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    score_30m = ctx.get("no_pulse_score")
    score_1h = one_h.get("no_pulse_score")
    confirms_30m = direction_confirms(ctx)
    confirms_1h = direction_confirms(one_h)
    opposes_1h = context_opposes(one_h)
    rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    notes = [
        f"Regime Alignment V6: 30m regime={ctx.get('regime')}, phase={ctx.get('phase')}, score={score_30m}, RSI={fmt_float(ctx.get('jrsx'))} {rsi_state}",
        f"1H Regime check: available={bool(one_h.get('available'))}, confirms={confirms_1h}, opposes={opposes_1h}, score={score_1h}",
        "Pulse and VTM/BTM ignored by design; Duo signal is still the trigger",
    ]
    if not ctx.get("available") or score_30m is None:
        notes.append("30m Regime data unavailable; V6 cannot judge the Duo signal")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP", confidence="none", approximation="regime_duo_no_pulse")

    weight = 0.0
    if confirms_30m and score_30m >= 72:
        weight = 0.5
        notes.append("30m Regime strongly confirms Duo direction")
    elif confirms_30m and score_30m >= 60:
        weight = 0.25
        notes.append("30m Regime confirms Duo direction but conviction is modest")
    elif score_30m >= 66 and not context_opposes(ctx):
        weight = 0.25
        notes.append("30m Regime is constructive but phase is not ideal; small probe only")
    else:
        notes.append("30m Regime does not provide enough conviction without Pulse")

    if weight > 0:
        if one_h.get("available"):
            if opposes_1h:
                weight = step_weight(weight, -0.25)
                notes.append("1H Regime opposes the Duo direction; V6 reduces one step")
            elif confirms_1h and (score_1h or 0) >= 65:
                weight = step_weight(weight, 0.25)
                notes.append("1H Regime confirms; V6 upgrades one step")
            else:
                notes.append("1H Regime is neutral; V6 keeps 30m size")
        else:
            weight = step_weight(weight, -0.25)
            notes.append("1H Regime unavailable; V6 reduces one step")

    if weight > 0 and rsi_state in {"RSI_OVERHEATED", "RSI_OVERSOLD_SHORT_CAUTION"}:
        weight = step_weight(weight, -0.25)
        notes.append("RSI is stretched against fresh entry; V6 reduces softly, not a hard skip")
    elif weight > 0 and rsi_state in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
        notes.append("RSI supports a possible turn; no extra size added until DuoBase update arrives")

    summary = single_tf_summary("1h", one_h)
    summary["no_pulse"] = True
    summary["score_30m"] = score_30m
    summary["score_1h"] = score_1h
    summary["rsi"] = ctx.get("jrsx")
    return convert_weight_policy(name, weight, notes, mtf_status="ready" if one_h.get("available") else "partial", mtf_summary=summary)


def regime_rsi_points(ctx: dict, direction: str, label: str, *, primary: bool) -> tuple[int, list[str]]:
    notes = []
    if not ctx.get("available"):
        return 0, [f"{label} unavailable"]
    score = 0
    confirms = direction_confirms(ctx)
    opposes = context_opposes(ctx)
    regime = str(ctx.get("regime") or "unknown")
    phase = str(ctx.get("phase") or "unknown")
    no_pulse = as_float(ctx.get("no_pulse_score"))
    if confirms:
        score += 3 if primary else 1
        notes.append(f"{label} Regime confirms Duo ({regime} {phase})")
    elif opposes:
        score -= 3 if primary else 1
        notes.append(f"{label} Regime opposes Duo ({regime} {phase})")
    else:
        score += 1 if primary and regime == "TRANSITION" else 0
        notes.append(f"{label} Regime is neutral/mixed ({regime} {phase})")
    if no_pulse is not None:
        if no_pulse >= 70:
            score += 2 if primary else 1
            notes.append(f"{label} Regime Alignment Score is strong ({fmt_float(no_pulse)})")
        elif no_pulse >= 58:
            score += 1 if primary else 0
            notes.append(f"{label} Regime Alignment Score is constructive ({fmt_float(no_pulse)})")
        elif no_pulse <= 42:
            score -= 2 if primary else 1
            notes.append(f"{label} Regime Alignment Score is weak ({fmt_float(no_pulse)})")
        else:
            notes.append(f"{label} Regime Alignment Score is middle/chop ({fmt_float(no_pulse)})")
    for value, strong, weak, metric in (
        (ctx.get("pp_acc"), 10, 2, "acceleration"),
        (ctx.get("pp_vel"), 5, 1, "velocity"),
    ):
        signed = as_float(value)
        if signed is None:
            notes.append(f"{label} {metric} unavailable")
            continue
        signed = signed if direction == "long" else -signed
        if signed >= strong:
            score += 2 if primary else 1
            notes.append(f"{label} {metric} strongly supports Duo ({fmt_float(value)})")
        elif signed >= weak:
            score += 1 if primary else 0
            notes.append(f"{label} {metric} supports Duo ({fmt_float(value)})")
        elif signed <= -strong:
            score -= 2 if primary else 1
            notes.append(f"{label} {metric} strongly opposes Duo ({fmt_float(value)})")
        elif signed <= -weak:
            score -= 1 if primary else 0
            notes.append(f"{label} {metric} opposes Duo ({fmt_float(value)})")
        else:
            notes.append(f"{label} {metric} neutral ({fmt_float(value)})")
    return score, notes


def rsi_quality_points(ctx: dict, direction: str, label: str, *, primary: bool) -> tuple[int, list[str]]:
    rsi = as_float(ctx.get("jrsx"))
    state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    if rsi is None:
        return 0, [f"{label} RSI unavailable"]
    score = 0
    notes = []
    if direction == "long":
        if rsi <= 25:
            score += 2 if primary else 1
            notes.append(f"{label} RSI recovery zone supports BUY ({fmt_float(rsi)})")
        elif 25 < rsi <= 62:
            score += 1 if primary else 0
            notes.append(f"{label} RSI is usable for BUY ({fmt_float(rsi)})")
        elif rsi >= 80:
            score -= 2 if primary else 1
            notes.append(f"{label} RSI is overheated for fresh BUY ({fmt_float(rsi)})")
        else:
            score -= 1 if primary else 0
            notes.append(f"{label} RSI is late/chasing for BUY ({fmt_float(rsi)})")
    else:
        if rsi >= 70:
            score += 2 if primary else 1
            notes.append(f"{label} RSI distribution zone supports SELL ({fmt_float(rsi)})")
        elif 38 <= rsi < 70:
            score += 1 if primary else 0
            notes.append(f"{label} RSI is usable for SELL ({fmt_float(rsi)})")
        elif rsi <= 20:
            score -= 2 if primary else 1
            notes.append(f"{label} RSI is oversold for fresh SELL ({fmt_float(rsi)})")
        else:
            score -= 1 if primary else 0
            notes.append(f"{label} RSI is late/chasing for SELL ({fmt_float(rsi)})")
    notes.append(f"{label} RSI state={state}")
    return score, notes


def decide_duo_regime_rsi_sized(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "duo_regime_rsi_sized"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "no_pulse_score": ctx.get("no_pulse_score"),
            "jrsx": ctx.get("jrsx"),
            "rsi_caution": ctx.get("rsi_caution"),
        },
        "1h": {
            "available": one_h.get("available"),
            "pp_acc": one_h.get("pp_acc"),
            "pp_vel": one_h.get("pp_vel"),
            "regime": one_h.get("regime"),
            "phase": one_h.get("phase"),
            "no_pulse_score": one_h.get("no_pulse_score"),
            "jrsx": one_h.get("jrsx"),
            "rsi_caution": one_h.get("rsi_caution"),
        },
        "no_pulse": True,
    }
    notes = [
        f"Duo {normalized['side'].upper()} is the trigger; Regime + RSI only size the new reverse entry",
        "Pulse, VTM/BTM and higher MTFs are ignored to reduce latency and indicator noise",
        "State machine rule: opposite Duo signal closes first; this score only decides the new entry size",
    ]
    if not ctx.get("available"):
        notes.append("30m Regime unavailable; close is allowed but no new position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)

    if context_opposes(ctx) and one_h.get("available") and context_opposes(one_h):
        rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
        if rsi_state not in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
            notes.append("Hard protection: 30m and 1H Regime both oppose Duo and RSI does not show a reversal zone")
            return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_REGIME_RSI_HARD_AGAINST", target_direction=target_direction, score=-5, mtf_summary=summary)

    score = 0
    delta, subnotes = regime_rsi_points(ctx, direction, "30m", primary=True)
    score += delta
    notes.extend(subnotes)
    delta, subnotes = rsi_quality_points(ctx, direction, "30m", primary=True)
    score += delta
    notes.extend(subnotes)
    if one_h.get("available"):
        delta, subnotes = regime_rsi_points(one_h, direction, "1H", primary=False)
        score += delta
        notes.extend(subnotes)
        delta, subnotes = rsi_quality_points(one_h, direction, "1H", primary=False)
        score += delta
        notes.extend(subnotes)
    else:
        notes.append("1H unavailable; policy continues from 30m only without penalty")

    if score >= 7:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 4:
        return policy_result(name, "REDUCE", 0.75, 1.0, notes, action="THREE_QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 2:
        return policy_result(name, "REDUCE", 0.5, 1.0, notes, action="HALF_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 0:
        return policy_result(name, "REDUCE_SMALL", 0.25, 1.0, notes, action="QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score below zero; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_REGIME_RSI", target_direction=target_direction, score=score, mtf_summary=summary)


def decide_duo_regime_rsi_30m(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "duo_regime_rsi_30m"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "no_pulse_score": ctx.get("no_pulse_score"),
            "jrsx": ctx.get("jrsx"),
            "rsi_caution": ctx.get("rsi_caution"),
        },
        "timeframes_used": ["30m"],
        "no_pulse": True,
    }
    notes = [
        f"Duo {normalized['side'].upper()} trigger; 30m Regime + RSI size only the new reverse entry",
        "Strict 30m-only policy: no Pulse, no VTM/BTM, no AVWAP, no 1H/2H/3H/4H",
        "Duo Risk-On is intentionally ignored because this local reader can derive it from Pulse inputs",
        "State machine rule: opposite Duo signal closes first; this score only decides the new entry size",
    ]
    if not ctx.get("available"):
        notes.append("30m Regime unavailable; close is allowed but no new position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)

    score = 0
    regime = str(ctx.get("regime") or "unknown")
    phase = str(ctx.get("phase") or "unknown")
    if direction_confirms(ctx):
        score += 1
        notes.append(f"30m Regime confirms Duo ({regime} {phase})")
    elif context_opposes(ctx):
        score -= 1
        notes.append(f"30m Regime opposes Duo ({regime} {phase})")
    else:
        notes.append(f"30m Regime is mixed/chop ({regime} {phase})")

    nps = as_float(ctx.get("no_pulse_score"))
    if nps is not None:
        if nps >= 70:
            score += 2
            notes.append(f"30m Regime Alignment Score strong ({fmt_float(nps)})")
        elif nps >= 52:
            score += 1
            notes.append(f"30m Regime Alignment Score constructive ({fmt_float(nps)})")
        elif nps <= 45:
            score -= 2
            notes.append(f"30m Regime Alignment Score weak/chop ({fmt_float(nps)})")
        else:
            notes.append(f"30m Regime Alignment Score middle ({fmt_float(nps)})")

    for value, strong, weak, label in (
        (ctx.get("pp_acc"), 10, 2, "30m acceleration"),
        (ctx.get("pp_vel"), 5, 1, "30m velocity"),
    ):
        raw = as_float(value)
        if raw is None:
            notes.append(f"{label} unavailable")
            continue
        signed = raw if direction == "long" else -raw
        if signed >= strong:
            score += 2
            notes.append(f"{label} strongly supports Duo ({fmt_float(raw)})")
        elif signed >= weak:
            score += 1
            notes.append(f"{label} supports Duo ({fmt_float(raw)})")
        elif signed <= -strong:
            score -= 2
            notes.append(f"{label} strongly opposes Duo ({fmt_float(raw)})")
        elif signed <= -weak:
            score -= 1
            notes.append(f"{label} opposes Duo ({fmt_float(raw)})")
        else:
            notes.append(f"{label} neutral ({fmt_float(raw)})")

    rsi = as_float(ctx.get("jrsx"))
    if rsi is None:
        notes.append("30m RSI unavailable")
    elif direction == "long":
        if rsi <= 25:
            score += 2
            notes.append(f"30m RSI recovery zone supports BUY ({fmt_float(rsi)})")
        elif rsi <= 68:
            score += 1
            notes.append(f"30m RSI usable for BUY ({fmt_float(rsi)})")
        elif rsi >= 80:
            score -= 2
            notes.append(f"30m RSI overheated for fresh BUY ({fmt_float(rsi)})")
        else:
            score -= 1
            notes.append(f"30m RSI late/chasing for BUY ({fmt_float(rsi)})")
    else:
        if rsi >= 70:
            score += 2
            notes.append(f"30m RSI distribution zone supports SELL ({fmt_float(rsi)})")
        elif rsi >= 32:
            score += 1
            notes.append(f"30m RSI usable for SELL ({fmt_float(rsi)})")
        elif rsi <= 20:
            score -= 2
            notes.append(f"30m RSI oversold for fresh SELL ({fmt_float(rsi)})")
        else:
            score -= 1
            notes.append(f"30m RSI late/chasing for SELL ({fmt_float(rsi)})")

    if score >= 4:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 0:
        return policy_result(name, "REDUCE_SMALL", 0.25, 1.0, notes, action="QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score below zero; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_30M_REGIME_RSI", target_direction=target_direction, score=score, mtf_summary=summary)


def metric_align_points(value, direction: str, *, strong: float, weak: float, label: str, points: int = 1) -> tuple[int, str]:
    value = as_float(value)
    if value is None:
        return 0, f"{label} unavailable"
    sign = 1 if direction == "long" else -1
    aligned = value * sign
    if aligned >= strong:
        return points, f"{label} strongly aligned ({fmt_float(value)})"
    if aligned >= weak:
        return max(1, points - 1), f"{label} aligned ({fmt_float(value)})"
    if aligned <= -strong:
        return -points, f"{label} strongly against ({fmt_float(value)})"
    if aligned <= -weak:
        return -max(1, points - 1), f"{label} against ({fmt_float(value)})"
    return 0, f"{label} neutral ({fmt_float(value)})"


def both_acc_vel_against(ctx: dict, direction: str) -> bool:
    acc = as_float(ctx.get("pp_acc"))
    vel = as_float(ctx.get("pp_vel"))
    if acc is None or vel is None:
        return False
    sign = 1 if direction == "long" else -1
    return (acc * sign) < -2 and (vel * sign) < -1


def decide_duo_conviction_sized(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "duo_conviction_sized"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    notes = [
        f"Duo {normalized['side'].upper()} is the trigger; this policy only sizes the reversal",
        "State machine rule: opposite signal closes first; size controls only the new reverse entry",
    ]
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "jrsx": ctx.get("jrsx"),
            "pulse": ctx.get("pulse"),
            "pulse_bkt": ctx.get("pulse_bkt"),
            "pulse_rs_mkt": ctx.get("pulse_rs_mkt"),
        },
        "1h": {
            "available": one_h.get("available"),
            "pp_acc": one_h.get("pp_acc"),
            "pp_vel": one_h.get("pp_vel"),
            "regime": one_h.get("regime"),
            "phase": one_h.get("phase"),
        },
    }

    if not ctx.get("available"):
        notes.append("30m MXC unavailable; closes are allowed by paper state, but no new position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)

    if both_acc_vel_against(ctx, direction) and both_acc_vel_against(one_h, direction):
        notes.append("Hard override: 30m and 1H acceleration plus velocity both oppose Duo direction")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_HARD_AGAINST", target_direction=target_direction, score=-3, mtf_summary=summary)

    score = 0
    for value, strong, weak, label, points in (
        (ctx.get("pp_acc"), 10, 2, "30m pp_acc", 2),
        (ctx.get("pp_vel"), 5, 1, "30m pp_vel", 2),
        (one_h.get("pp_acc"), 10, 2, "1H pp_acc", 1),
        (one_h.get("pp_vel"), 5, 1, "1H pp_vel", 1),
    ):
        delta, reason = metric_align_points(value, direction, strong=strong, weak=weak, label=label, points=points)
        score += delta
        notes.append(reason)

    rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    jrsx = as_float(ctx.get("jrsx"))
    if rsi_state in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
        score += 1
        notes.append(f"RSI quality supports a turn ({rsi_state}, JRSX={fmt_float(jrsx)})")
    elif rsi_state in {"RSI_OVERHEATED", "RSI_OVERSOLD_SHORT_CAUTION"}:
        score -= 1
        notes.append(f"RSI quality warns against fresh entry ({rsi_state}, JRSX={fmt_float(jrsx)})")
    else:
        notes.append(f"RSI quality neutral/unknown ({rsi_state}, JRSX={fmt_float(jrsx)})")

    pulse_bkt = as_float(ctx.get("pulse_bkt"))
    pulse_rs = as_float(ctx.get("pulse_rs_mkt"))
    pulse = ctx.get("pulse")
    if pulse in {"PULSE_OK", "PULSE_STRONG"}:
        score += 1
        notes.append(f"Low-weight Pulse confirmation ({pulse}, breakout={fmt_float(pulse_bkt)}, RS:Mkt={fmt_float(pulse_rs)})")
    elif pulse == "PULSE_WEAK":
        score -= 1
        notes.append(f"Low-weight Pulse late/chop caution ({pulse}, breakout={fmt_float(pulse_bkt)}, RS:Mkt={fmt_float(pulse_rs)})")
    else:
        notes.append(f"Low-weight Pulse neutral ({pulse}, breakout={fmt_float(pulse_bkt)}, RS:Mkt={fmt_float(pulse_rs)})")

    if score >= 5:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 3:
        return policy_result(name, "REDUCE", 0.5, 1.0, notes, action="HALF_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 1:
        return policy_result(name, "REDUCE_SMALL", 0.25, 1.0, notes, action="QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score <= 0; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_CONVICTION", target_direction=target_direction, score=score, mtf_summary=summary)


def decide_conviction_v2_candidate(normalized: dict, ctx: dict, mtf_ctxs: dict) -> dict:
    name = "conviction_v2_candidate"
    direction = "long" if normalized["side"] == "buy" else "short"
    target_direction = "LONG" if direction == "long" else "SHORT"
    one_h = (mtf_ctxs or {}).get("1h") or {}
    notes = [
        f"Duo {normalized['side'].upper()} trigger",
        "State machine rule: opposite signal closes first; score sizes only the new reverse entry",
        "V2 sizing: full >=3, 0.75x >=2, 0.40x >=0, skip <0",
    ]
    summary = {
        "30m": {
            "available": ctx.get("available"),
            "pp_acc": ctx.get("pp_acc"),
            "pp_vel": ctx.get("pp_vel"),
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "jrsx": ctx.get("jrsx"),
            "rsi_caution": ctx.get("rsi_caution"),
            "pulse": ctx.get("pulse"),
            "pulse_bkt": ctx.get("pulse_bkt"),
            "pulse_rs_mkt": ctx.get("pulse_rs_mkt"),
        },
        "1h": {
            "available": one_h.get("available"),
            "pp_acc": one_h.get("pp_acc"),
            "pp_vel": one_h.get("pp_vel"),
            "regime": one_h.get("regime"),
            "phase": one_h.get("phase"),
        },
    }
    if not ctx.get("available"):
        notes.append("30m MXC unavailable; no new reverse position opens")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="BLOCKED_30M_HEALTH", target_direction=target_direction, score=0, health_status="primary_30m_blocked", mtf_summary=summary)
    if both_acc_vel_against(ctx, direction) and both_acc_vel_against(one_h, direction):
        notes.append("Hard override: 30m and 1H acceleration plus velocity both oppose Duo direction")
        return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_HARD_AGAINST", target_direction=target_direction, score=-3, mtf_summary=summary)

    score = 0
    for value, strong, weak, label, points in (
        (ctx.get("pp_acc"), 10, 2, "30m pp_acc", 2),
        (ctx.get("pp_vel"), 5, 1, "30m pp_vel", 2),
        (one_h.get("pp_acc"), 10, 2, "1H pp_acc", 1),
        (one_h.get("pp_vel"), 5, 1, "1H pp_vel", 1),
    ):
        delta, reason = metric_align_points(value, direction, strong=strong, weak=weak, label=label, points=points)
        score += delta
        notes.append(reason)

    rsi_state = ctx.get("rsi_caution") or "RSI_UNKNOWN"
    jrsx = as_float(ctx.get("jrsx"))
    if rsi_state in {"RSI_RECOVERY_ZONE", "RSI_DISTRIBUTION_ZONE"}:
        score += 1
        notes.append(f"RSI supports turn ({rsi_state}, JRSX={fmt_float(jrsx)})")
    elif rsi_state in {"RSI_OVERHEATED", "RSI_OVERSOLD_SHORT_CAUTION"}:
        score -= 1
        notes.append(f"RSI warns against entry ({rsi_state}, JRSX={fmt_float(jrsx)})")
    else:
        notes.append(f"RSI neutral/unknown ({rsi_state}, JRSX={fmt_float(jrsx)})")

    pulse = ctx.get("pulse")
    if pulse in {"PULSE_OK", "PULSE_STRONG"}:
        score += 1
        notes.append(f"Low-weight Pulse confirms ({pulse})")
    elif pulse == "PULSE_WEAK":
        score -= 1
        notes.append(f"Low-weight Pulse caution ({pulse})")
    else:
        notes.append(f"Low-weight Pulse neutral ({pulse})")

    if score >= 3:
        return policy_result(name, "TRADE", 1.0, 1.0, notes, action="FULL_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 2:
        return policy_result(name, "REDUCE", 0.75, 1.0, notes, action="THREE_QUARTER_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    if score >= 0:
        return policy_result(name, "REDUCE_SMALL", 0.40, 1.0, notes, action="FORTY_PERCENT_ENTRY", target_direction=target_direction, score=score, mtf_summary=summary)
    notes.append("Score < 0; no reverse entry after any required close")
    return policy_result(name, "SKIP", 0.0, 0.0, notes, action="SKIP_LOW_CONVICTION", target_direction=target_direction, score=score, mtf_summary=summary)


def fmt_float(value):
    try:
        if value is None:
            return "-"
        return f"{float(value):.2f}"
    except Exception:
        return str(value)
