"""Event-sourced position-state mutation (extracted from webhook_receiver.py, Phase 0a).

ONE mutation routine, apply_effect(), is the *only* code that mutates paper
state. Both the live path (paper_apply_policy, via _record_transition) and
replay (replay_position_journal) drive state exclusively through it, so a replay
of the journal is identical to the live run that produced it — the math is
written once. An "effect" is a fully-resolved description of a single
transition: it carries the already-computed numbers (stat deltas, the new
position dict, the close result, the new equity), so apply_effect performs NO
business math — only assignment/accumulation. paper_apply_policy still computes
those numbers exactly as before and packages them into the effect.

effect shape (schema_version 1):
  {"op": "skip"}                                   # stats.skips += 1
  {"op": "adjust", "fields": {...}}                # pos.update(fields) (same dir)
  {"op": "close",  "gross_usd","net_usd","weighted_pct","net_weighted_pct",
                   "exit_fee","funding_usd","win": bool,
                   "equity_set": <float, compound only>}    # close + stat deltas
  {"op": "open",   "position": {...}, "entry_fee": <float>} # open + entries/fees
Any effect may also carry "compound": true and "initial_equity_seed": <float> so
apply_effect can (idempotently) seed initial_equity/equity for the symbol the
same way the live path's compound preamble does.

Import discipline: this module is imported BY webhook_receiver (re-exported there
so wr.apply_effect stays importable and monkeypatchable). The pure money/decimal
primitives (D, dec_usd, dec_pct, empty_policy_stats and the
_USD_KEYS/_PCT_KEYS/_UNITS_KEYS sets) now live in the webhook.money leaf module,
and POLICY_LABELS lives in the pure strategy.decision_math module, so both are
imported directly here (Phase 0b) — no late-bound _wr() shim and no circular
import, because neither leaf imports webhook_receiver.
"""
from __future__ import annotations

from strategy.decision_math import POLICY_LABELS
from webhook.money import (
    D,
    dec_usd,
    dec_pct,
    empty_policy_stats,
    _USD_KEYS,
    _PCT_KEYS,
    _UNITS_KEYS,
)


def refresh_compound_stats(ps: dict) -> None:
    equity = ps.get("equity") or {}
    initial = ps.get("initial_equity") or {}
    total_equity = sum((D(v or 0.0) for v in equity.values()), D("0"))
    total_initial = sum((D(v or 0.0) for v in initial.values()), D("0"))
    stats = ps.setdefault("stats", empty_policy_stats())
    stats["current_equity_usd"] = float(dec_usd(total_equity))
    stats["initial_equity_usd"] = float(dec_usd(total_initial))
    stats["equity_change_usd"] = float(dec_usd(total_equity - total_initial))
    stats["equity_change_pct"] = float(dec_pct(((total_equity / total_initial) - D("1")) * D("100"))) if total_initial != 0 else 0.0


def _coerce_state_numeric_fields(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if isinstance(item, (dict, list)):
                out[key] = _coerce_state_numeric_fields(item)
                continue
            if not isinstance(item, str):
                out[key] = item
                continue
            looks_numeric = (
                key_text in _USD_KEYS
                or key_text in _PCT_KEYS
                or key_text in _UNITS_KEYS
                or key_text.endswith("_usd")
                or key_text.endswith("_pct")
                or key_text.endswith("_units")
                or key_text.endswith("_price")
                or key_text in {"weight", "entry_fee", "equity_set"}
            )
            if looks_numeric:
                try:
                    out[key] = float(D(item))
                    continue
                except Exception:
                    pass
            out[key] = item
        return out
    if isinstance(value, list):
        return [_coerce_state_numeric_fields(item) for item in value]
    return value


def _ensure_policy_bucket(state: dict, account_key: str, policy_key: str) -> dict:
    policies_state = state.setdefault(account_key, {})
    ps = policies_state.setdefault(policy_key, {"label": POLICY_LABELS.get(policy_key, policy_key), "symbols": {}, "stats": empty_policy_stats()})
    ps.setdefault("stats", empty_policy_stats())
    ps.setdefault("symbols", {})
    return ps


def apply_effect(state: dict, account: str, policy: str, symbol: str, effect: dict) -> None:
    """The single, shared state-mutation routine. Live and replay both call this."""
    ps = _ensure_policy_bucket(state, account, policy)
    if effect.get("compound"):
        initial = ps.setdefault("initial_equity", {})
        equity = ps.setdefault("equity", {})
        initial.setdefault(symbol, effect.get("initial_equity_seed"))
        equity.setdefault(symbol, initial[symbol])
    stats = ps["stats"]
    symbols = ps["symbols"]
    op = effect.get("op")
    if op == "skip":
        stats["skips"] = int(stats.get("skips", 0)) + 1
        return
    if op == "adjust":
        pos = symbols.get(symbol)
        if pos is not None:
            pos.update(_coerce_state_numeric_fields(effect.get("fields") or {}))
        return
    if op == "close":
        symbols.pop(symbol, None)
        stats["closed_trades"] = int(stats.get("closed_trades", 0)) + 1
        stats["realized_gross_pnl_usd"] = float(dec_usd(D(stats.get("realized_gross_pnl_usd", 0)) + D(effect["gross_usd"])))
        stats["realized_net_pnl_usd"] = float(dec_usd(D(stats.get("realized_net_pnl_usd", 0)) + D(effect["net_usd"])))
        stats["realized_pnl_usd"] = stats["realized_net_pnl_usd"]
        stats["realized_gross_pnl_pct_weighted"] = float(dec_pct(D(stats.get("realized_gross_pnl_pct_weighted", 0)) + D(effect["weighted_pct"])))
        stats["realized_net_pnl_pct_weighted"] = float(dec_pct(D(stats.get("realized_net_pnl_pct_weighted", 0)) + D(effect["net_weighted_pct"])))
        stats["realized_pnl_pct_weighted"] = stats["realized_net_pnl_pct_weighted"]
        stats["total_fees_usd"] = float(dec_usd(D(stats.get("total_fees_usd", 0)) + D(effect["exit_fee"])))
        stats["total_funding_usd"] = float(dec_usd(D(stats.get("total_funding_usd", 0)) + D(effect["funding_usd"])))
        if effect.get("compound"):
            equity = ps.setdefault("equity", {})
            equity[symbol] = float(dec_usd(effect["equity_set"]))
            refresh_compound_stats(ps)
        if effect["win"]:
            stats["wins"] = int(stats.get("wins", 0)) + 1
        else:
            stats["losses"] = int(stats.get("losses", 0)) + 1
        return
    if op == "open":
        entry_fee = effect["entry_fee"]
        stats["total_fees_usd"] = float(dec_usd(D(stats.get("total_fees_usd", 0)) + D(entry_fee)))
        symbols[symbol] = _coerce_state_numeric_fields(effect["position"])
        stats["entries"] = int(stats.get("entries", 0)) + 1
        if effect.get("compound"):
            refresh_compound_stats(ps)
        return
    raise ValueError(f"apply_effect: unknown op {op!r}")
