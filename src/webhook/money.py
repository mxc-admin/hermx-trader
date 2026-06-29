"""Pure money/decimal primitives (Phase 0b leaf extraction).

This module holds the byte-for-byte money math that used to live in
webhook_receiver.py: the Decimal coercion helper ``D``, the fixed-precision
quantizers (``dec_usd`` / ``dec_notional`` / ``dec_pct`` / ``dec_units``), their
text formatters, the ``_USD_KEYS`` / ``_PCT_KEYS`` / ``_UNITS_KEYS`` field-name
sets, the recursive ``canonicalize_decimal_fields`` coercion, and the
``empty_policy_stats`` factory.

It is a TRUE leaf: it imports only ``decimal`` from the stdlib and reads NO
mutable global state. webhook_receiver re-exports every name here for backward
compatibility, and webhook.position_journal imports straight from this module
(no more late-bound ``_wr()`` lookup).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def D(value, default: str = "0") -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def dec_usd(value) -> Decimal:
    return D(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def dec_notional(value) -> Decimal:
    return D(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def dec_pct(value) -> Decimal:
    return D(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def dec_units(value) -> Decimal:
    return D(value).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def dec_text(value: Decimal | str | float | int) -> str:
    return format(D(value), "f")


def usd_text(value) -> str:
    return dec_text(dec_usd(value))


def notional_text(value) -> str:
    return dec_text(dec_notional(value))


def pct_text(value) -> str:
    return dec_text(dec_pct(value))


def units_text(value) -> str:
    return dec_text(dec_units(value))


_USD_KEYS = {
    "base_notional_usd",
    "notional_usd",
    "entry_fee_usd",
    "exit_fee_usd",
    "total_fees_usd",
    "funding_usd",
    "gross_pnl_usd",
    "net_pnl_usd",
    "pnl_usd",
    "realized_pnl_usd",
    "realized_net_pnl_usd",
    "realized_gross_pnl_usd",
    "current_equity_usd",
    "initial_equity_usd",
    "equity_change_usd",
    "equity_usd",
    "gross_usd",
    "net_usd",
    "entry_fee",
    "exit_fee",
    "equity_set",
}
_PCT_KEYS = {
    "risk_weight",
    "weight",
    "pnl_pct",
    "weighted_pnl_pct",
    "net_weighted_pnl_pct",
    "realized_pnl_pct",
    "realized_net_pnl_pct_weighted",
    "realized_gross_pnl_pct_weighted",
    "realized_pnl_pct_weighted",
    "alert_execution_diff_pct",
    "weighted_pct",
    "net_weighted_pct",
    "equity_change_pct",
}
_UNITS_KEYS = {"qty_units", "filled_size"}


def canonicalize_decimal_fields(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if item is None:
                out[key] = None
            elif isinstance(item, (dict, list)):
                out[key] = canonicalize_decimal_fields(item)
            elif key_text == "planned_notional_usd":
                out[key] = notional_text(item)
            elif key_text in _USD_KEYS or key_text.endswith("_usd"):
                out[key] = usd_text(item)
            elif key_text in _PCT_KEYS or key_text.endswith("_pct"):
                out[key] = pct_text(item)
            elif key_text in _UNITS_KEYS or key_text.endswith("_units"):
                out[key] = units_text(item)
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        return [canonicalize_decimal_fields(item) for item in value]
    return value


def empty_policy_stats() -> dict:
    return {
        "realized_pnl_usd": 0.0,
        "realized_gross_pnl_usd": 0.0,
        "realized_net_pnl_usd": 0.0,
        "realized_pnl_pct_weighted": 0.0,
        "realized_gross_pnl_pct_weighted": 0.0,
        "realized_net_pnl_pct_weighted": 0.0,
        "total_fees_usd": 0.0,
        "total_funding_usd": 0.0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "skips": 0,
        "entries": 0,
    }
