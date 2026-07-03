#!/usr/bin/env python3
"""Reverse-engineer the phantom OKX clOrdId `mxc6c9af98fef430983b6fd31f73373c`.

Uses the REAL production functions (webhook_receiver.normalize / _signal_identity /
stable_client_order_id) — no re-implementation — to search which (strategy, symbol,
side, timeframe, bar-time, role) tuple hashes to the phantom id. A hit proves the
order was minted by HermX code and pinpoints the exact signal.
"""
import sys, itertools, datetime as dt
sys.path.insert(0, "src")
import webhook_receiver as w

TARGET = "mxc6c9af98fef430983b6fd31f73373c"

STRATEGIES = [
    ("btcusdt_duo_base_dev_2h", "2h", 2),
    ("ethusdt_duo_base_dev_2h", "2h", 2),
    ("solusdt_duo_base_dev_3h", "3h", 3),
    ("xrpusdt_duo_base_dev_4h", "4h", 4),
]
# Symbol strings a TradingView BTC alert might carry (pre-normalize forms + odd ones).
SYMBOLS = ["BTCUSDT", "BTCUSDT.P", "BTCUSDTPERP", "BTC-USDT-SWAP", "BTCUSDT.PS",
           "OKX:BTCUSDT.P", "BTCUSD", "BTCUSDTSWAP", "ETHUSDT", "SOLUSDT", "XRPUSDT",
           "ETHUSDT.P", "SOLUSDT.P", "XRPUSDT.P"]
SIDES = ["buy", "sell", "close"]
ROLES = ["open", "close", "base"]

def bar_times(hours, start, end):
    """ISO bar timestamps aligned to `hours`, plus common format variants + epochs."""
    step = dt.timedelta(hours=hours)
    t = start
    while t <= end:
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        yield iso
        yield iso.replace("Z", "+00:00")
        yield t.strftime("%Y-%m-%dT%H:%M:%S")
        yield str(int(t.replace(tzinfo=dt.timezone.utc).timestamp()))          # epoch s
        yield str(int(t.replace(tzinfo=dt.timezone.utc).timestamp()) * 1000)   # epoch ms
        t += step

def build_id(strategy_id, symbol, side, timeframe, tv_time, role):
    payload = {"strategy_id": strategy_id, "symbol": symbol, "action": side,
               "side": side, "timeframe": timeframe, "tv_time": tv_time,
               "source": "tradingview"}
    norm = w.normalize(payload)
    identity = w._signal_identity(norm)
    return w.stable_client_order_id(identity, role=role), identity

def main():
    start = dt.datetime(2025, 1, 1, 0, 0, 0)
    end = dt.datetime(2026, 7, 5, 0, 0, 0)
    tried = 0
    for (sid, tf, hrs), symbol, side, role in itertools.product(STRATEGIES, SYMBOLS, SIDES, ROLES):
        for tv_time in bar_times(hrs, start, end):
            cl, identity = build_id(sid, symbol, side, tf, tv_time, role)
            tried += 1
            if cl == TARGET:
                print(f"\n*** MATCH after {tried:,} tries ***")
                print(f"strategy_id = {sid}\nsymbol      = {symbol}\nside        = {side}")
                print(f"timeframe   = {tf}\ntv_time     = {tv_time}\nrole        = {role}")
                print(f"identity    = {identity}")
                return 0
    print(f"NO MATCH in {tried:,} candidates "
          f"(strategies×symbols×sides×roles×bar-times {start.date()}..{end.date()})")
    return 1

if __name__ == "__main__":
    sys.exit(main())
