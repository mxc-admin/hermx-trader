# Dashboard Model

The dashboard is a **read-only** state viewer. It never submits, cancels, or mutates the
money path — its only job is to render state it reads from strategy files, logs, ledgers,
and exchange readback. It must not invent positions.

## Authority: live panel vs. ledger

Two kinds of data appear on the dashboard, and they have **different authority**. Do not
conflate them.

| Source | What it is | Authority | Failure mode |
|--------|-----------|-----------|--------------|
| **Live panel** (exchange readback, executor health, live price/position) | A best-effort **health snapshot** queried at render time | Informational only — *not* a record of truth. May be stale, degraded, or unavailable. | When the executor is unavailable/stale, show an explicit `EXECUTOR ERROR` / `STALE` banner and report **UNKNOWN** — never silently render "flat". |
| **Ledgers** (`executions.jsonl`, `order-journal.jsonl`, `paper-state.json`) | The durable, append-only **record of what happened** | **Authoritative.** The order journal's `PLANNED → SUBMITTED → (FILLED \| REJECTED \| UNKNOWN)` lifecycle is the source of truth for order state. | A torn trailing line is tolerated (quarantined); the canonical state is whatever the journal says. |

Rule of thumb: if the live panel and a ledger disagree, the **ledger wins** for "what
happened"; the live panel only answers "what does the venue look like right now". An
order the journal holds as `UNKNOWN` stays UNKNOWN on the dashboard even if the live panel
happens to show a flat position — reconciliation (not the dashboard) resolves it.

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

- asset (derived from `instrument.inst_id`)
- timeframe
- execution mode (`demo` or `live`; only `live` is real-money, `demo` is sandbox)
- budget start (`capital.budget_usd`)
- budget now
- PnL now
- live price
- current exchange position
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
