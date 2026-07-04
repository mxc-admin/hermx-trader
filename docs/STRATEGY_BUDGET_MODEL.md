# Strategy Budget Model

How HermX decides how much money each strategy trades with — and what happens as it wins, loses,
or gets edited by an operator.

## TL;DR

- **`budget_usd`** is the starting capital you assign to a strategy, set in its strategy file
  (`capital.budget_usd`). It only ever changes when you manually edit that file.
- **By default, every strategy compounds.** Each new trade sizes off **equity**
  (`budget_usd + realized profit/loss so far`), not the original fixed number. Win, and the next
  trade is bigger. Lose, and the next trade is smaller.
- **If equity hits zero or goes negative, the strategy stops opening new trades** — but it can
  always still close an existing position. It automatically starts trading again the moment equity
  is positive (e.g. you raise `budget_usd`, or a later close brings it back above zero).
- **Demo and live are always separate pools.** A strategy's demo P&L never affects its live equity,
  and vice versa.
- **The dashboard and the actual order sizing always agree** — the "Effective budget" / "Total
  equity" numbers on the dashboard are exactly what the next trade will size off, not a separate
  display-only estimate.
- Want the old fixed-size behavior instead of compounding? Set `"reinvest": false` in the
  strategy's `capital` block.

---

## The core idea, in plain terms

Every strategy starts with a **seed budget** — the dollar amount you decide to risk on it. From
there, HermX tracks two different numbers:

| Term | What it means |
|---|---|
| **Budget (seed)** | The number you set. Fixed. Only changes if you edit the strategy file. |
| **Equity** | The seed *plus* every realized profit or loss from that strategy's closed trades. Moves automatically as trades close. |

By default, **each new trade sizes off equity, not the fixed seed** — this is called
**reinvesting** or **compounding**. It's the same idea as leaving your winnings in the account
instead of only ever betting the original stake: if the strategy is profitable, later trades are
proportionally bigger; if it's losing, later trades shrink to match, and eventually stop once the
capital is used up.

```
equity = budget_usd (seed) + realized profit/loss so far
notional traded = equity × leverage
```

## Turning compounding on or off

The switch is `capital.reinvest` in the strategy JSON file:

```json
"capital": {
  "budget_usd": 1500,
  "reinvest": true
}
```

| `reinvest` value | What happens |
|---|---|
| `true` (**the default — also what happens if you leave the key out entirely**) | Trades size off equity (seed + realized P&L). Compounding. |
| `false` | Trades always size off the fixed seed `budget_usd`, no matter what the strategy has made or lost. |

All four of HermX's current live strategies (`btcusdt_duo_base_dev_2h`, `ethusdt_duo_base_dev_2h`,
`solusdt_duo_base_dev_3h`, `xrpusdt_duo_base_dev_4h`) run with `reinvest: true`.

## What happens when the strategy runs out of money

If a strategy's equity drops to zero or below — it's lost everything you assigned it — HermX
stops it from opening **new** trades. It will never, however, refuse to **close** a position that's
already open; flattening a losing position is always allowed, exactly like the emergency
kill-switch and the per-symbol pause work elsewhere in the system. Nothing about this needs manual
intervention to recover: the moment equity is positive again — because you raised the budget, reset
the accounting window (below), or a later trade closes in profit — the strategy automatically
resumes trading on its very next signal.

## Editing the budget file

Because sizing is recalculated fresh on every single trade signal (nothing is "remembered" from
before), changing `budget_usd` in the strategy file takes effect immediately on the next signal:

```
new equity = new budget_usd + realized profit/loss so far
```

Nothing about the strategy's trading history is lost or reset by this edit — raising or lowering
the seed just shifts the whole equity curve up or down by that amount. Increasing the budget gives
the strategy more room; decreasing it tightens sizing (and can trigger the "no new trades" stop
above if it pushes equity to zero or below).

If instead you want to **wipe the slate clean** — start counting profit/loss from today forward,
ignoring everything before — use the accounting-window feature (set via the dashboard/API) rather
than editing the budget number. That excludes older closed trades from the equity calculation
without deleting them from the permanent trade history.

## Demo vs. live are always separate

A strategy's equity is calculated only from trades closed in the **same environment** it's
currently running in:

- A strategy running in `demo` mode only ever counts its own demo closes toward equity.
- A strategy running in `live` mode only ever counts its own live closes toward equity.

Demo profit can never inflate live sizing, and a live loss can never affect a demo strategy's
equity. `budget_usd` itself is the same fixed number in both environments — only the accumulated
realized P&L differs between them.

## The dashboard shows exactly what trading uses

The "Effective budget" / "Total equity" figures on the strategy dashboard card use the identical
calculation and the identical demo/live scoping as real order sizing. There's no separate
"for display only" number — what you see on the dashboard is what the next trade will size off.

## Current strategy budgets

Total assigned demo margin across all strategies: **$6,000**.

| Asset | Seed budget | Leverage | Fixed target notional |
|---|---:|---:|---:|
| SOLUSDT | $1,500 | 2x | $3,000 |
| ETHUSDT | $1,500 | 2x | $3,000 |
| XRPUSDT | $1,500 | 2x | $3,000 |
| BTCUSDT | $1,500 | 2x | $3,000 |

"Fixed target notional" is what each strategy trades at `reinvest: false`, or on its very first
ever trade before any P&L has accrued. With `reinvest: true` (the current setting for all four),
each strategy's actual notional compounds independently based on its own realized results.

## Reference: definitions

| Term | Meaning |
|---|---|
| Budget seed (`budget_usd`) | Operator-set starting capital; changes only on a manual strategy-file edit. |
| Realized net P&L | Sum of closed-trade profit/loss, scoped to this strategy, this account mode (demo/live), and the accounting window if set. |
| Equity | `budget seed + realized net P&L` — what order sizing uses when `reinvest: true`. |
| Open UPnL | Unrealized profit/loss on a currently open position. Shown on the dashboard; not part of order sizing. |
| Total equity (dashboard) | `equity + open UPnL` — the full account value including the open position. |
| Accounting window | An optional per-strategy reset point (`accounting_start_at`) that excludes closed trades before it from the equity calculation, without deleting them from the permanent trade history. |

## Important rule

Demo and live budgets/equity must never be mixed, and the dashboard should always make clear
whether a number comes from:

- historical paper replay
- sandbox execution (`execution_mode: demo` — routes to the sandbox/paper account; treated as
  `simulated_trading`)
- live execution (`execution_mode: live` — the only real-money mode; requires
  `HERMX_LIVE_TRADING=true`)
