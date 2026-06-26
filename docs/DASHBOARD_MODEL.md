# Dashboard Model

The dashboard is a state viewer.

It must not invent positions. It must read from strategy files, logs, ledgers, and exchange readback.

## Main View

Recommended clean tabs:

```text
Duo Base Dev Trial
Health
```

The main trading view should focus on the active Duo Base Dev trial. Research-only or paper-only views should not appear as primary tabs.

## Strategy Cards

Each active strategy gets one card.

Card fields:

- asset
- timeframe
- upper/lower parameters
- budget start
- budget now
- PnL now
- live price
- current OKX position
- alert count
- health state

Do not place leverage, target notional, or detailed order legs inside the cards. Those belong in the trade log. The cards should answer one question quickly: "What is this strategy doing right now?"

## Unified Trade Log

The trade log should be unified across strategies.

Recommended columns:

| Column | Meaning |
|---|---|
| Time | TradingView signal time |
| Strategy | strategy ID |
| Asset | symbol |
| Signal | buy/sell |
| Leg | open long, close short, open short, close long |
| Status | live, filled, skipped, failed |
| Alert price | TradingView signal price |
| Fill price | exchange fill price |
| Slippage | alert vs fill difference |
| Size | contracts/coin amount |
| Value | order value |
| Fee | exchange fee |
| PnL | realized PnL for close legs or live PnL for active open leg |

## Close/Open Display Rule

When a signal flips direction, the dashboard may show two rows:

```text
Close current position
Open new position
```

The close row should show realized PnL.

The open row should show live PnL if it is currently active.

## Clean Dashboard Scope

Do not show research-only tabs in the clean dashboard.

The dashboard should show only active strategy state, exchange readback, execution logs, and health information.
