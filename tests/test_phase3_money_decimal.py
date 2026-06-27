from __future__ import annotations

import webhook_receiver as wr


def test_decimal_helpers_quantize_money_and_pct():
    assert float(wr.dec_usd("1.55697")) == 1.557
    assert float(wr.dec_pct("2.0000004")) == 2.0
    assert float(wr.dec_units("0.000000019")) == 0.00000002


def test_pnl_fee_math_is_stable_to_expected_cent_precision():
    pct = wr.pnl_pct("long", 65000.0, 66300.0)
    assert pct == 2.0

    entry_notional = 3000.0
    entry_fee = wr.fee_usd(entry_notional, "taker")
    assert entry_fee == 1.5

    qty = entry_notional / 65000.0
    exit_notional = qty * 66300.0
    exit_fee = wr.fee_usd(exit_notional, "taker")
    assert exit_fee == 1.53

    gross = float(wr.dec_usd(wr.D(entry_notional) * wr.D(pct) / wr.D("100")))
    net = float(wr.dec_usd(wr.D(gross) - wr.D(entry_fee) - wr.D(exit_fee)))
    assert gross == 60.0
    assert net == 56.97
