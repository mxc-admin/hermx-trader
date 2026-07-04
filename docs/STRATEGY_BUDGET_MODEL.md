# Strategy Budget Model

How HermX decides how much money each strategy trades with — and what happens as it wins, loses,
or gets edited by an operator. Every claim below is grounded in a `file:line` reference to the
current code.

## TL;DR

- **`capital.budget_usd`** is the starting capital (the **seed**) you assign to a strategy, set in
  its strategy JSON file. It is **not** editable via the dashboard or any API — only by editing the
  file (`src/dashboard/server.py:212-257` accepts only `mode` and `accounting_start_at`).
- **By default, every strategy compounds.** With `capital.reinvest` true (the schema default, also
  when the key is absent — `src/strategy/records.py:91-103`), each new trade sizes off **equity**
  (`seed + realized net P&L` from the closed-trade ledger, `src/strategy/readiness.py:127`), not
  the fixed seed. Win, and the next trade is bigger. Lose, and the next trade is smaller.
- **If equity hits zero or goes negative, the strategy stops opening new trades** — Gate 6
  (`equity_stop`, `src/execution/service.py:236-247`) — but a close always passes. It resumes
  automatically the moment equity is positive again; no restart or manual re-arm needed.
- **Demo and live are always separate pools.** The realized-P&L component is filtered by the
  ledger's `mode` column (`src/strategy/readiness.py:118`, `src/pnl_ledger.py:418-420`).
- **Editing the seed requires a receiver restart.** Strategy files are loaded once at import
  (`src/webhook_receiver.py:500`); only the realized-P&L, accounting-window, and mode-override
  components are re-read from disk per signal. See "Editing the budget" below.
- **Two independent absolute ceilings** can refuse an order before it reaches the venue:
  `capital.max_notional_usd` and the `HERMX_MAX_NOTIONAL_USD` env var
  (`src/execution/service.py:23-50`). Both unset = no cap.
- Want the old fixed-size behavior instead of compounding? Set `"reinvest": false` in the
  strategy's `capital` block.

---

## 1. Where budget is defined: the strategy file

Strategy files live in `strategies/*.json` and validate against
`schemas/strategy.schema.json`. The canonical shape is **schema_version 2**; v1 is deprecated
(schema `$defs`, `schemas/strategy.schema.json:5`).

| Field | Schema | Meaning | Default if absent |
|---|---|---|---|
| `capital.budget_usd` | v2, required (`strategy.schema.json:158,170`) | Seed capital in USD. The number sizing starts from. | Read helper returns `0.0` (`src/strategy/records.py:80-88`), but the schema makes it required with `exclusiveMinimum: 0`. |
| `capital.reinvest` | v2, optional (`strategy.schema.json:159-163`) | Compounding switch. | **`true`** — compounding on (`src/strategy/records.py:91-103`). |
| `capital.max_notional_usd` | v2, optional (`strategy.schema.json:164-168`) | Absolute per-strategy pre-trade notional ceiling (USD), independent of `budget_usd × leverage`. | Unset ⇒ no cap (`src/execution/service.py:43,46-47`). |
| `leverage` | v1+v2, required (`strategy.schema.json:172`) | Notional multiplier only — never sent to the venue (see §4). | `1.0` if falsy (`src/strategy/readiness.py:138`). |
| `budget_usd` (flat, top-level) | v1 (`strategy.schema.json:83`) | Legacy flat form; read as fallback when `capital.budget_usd` is absent (`src/strategy/records.py:87-88`). | — |

Read helpers (single source of truth for both execution and dashboard):
`strategy_budget_usd()` (`src/strategy/records.py:80-88`, nested-then-flat) and
`strategy_reinvest_enabled()` (`src/strategy/records.py:91-103`, nested-then-flat, default `True`).

## 2. The core idea: seed vs. equity

| Term | What it means |
|---|---|
| **Budget (seed)** | The number you set in the file. Fixed. Only changes on a manual file edit (+ restart). |
| **Equity** | Seed *plus* every realized **net** profit or loss from that strategy's closed trades, scoped to its account mode and accounting window. Moves automatically as trades close. |

The effective-budget computation happens in `build_strategy_execution_readiness`
(`src/strategy/readiness.py:112-137`):

```python
equity_usd = seed_budget_usd + realized_net        # readiness.py:127
sizing_budget_usd = max(equity_usd, 0.0)           # readiness.py:130 (never negative)
base_notional = sizing_budget_usd * leverage       # readiness.py:138
```

`realized_net` comes from `net_realized_for_strategy()` (`src/pnl_ledger.py:404-420`), which sums
`net_realized_pnl` over `closed-trades.jsonl` rows for this `strategy_id`, filtered by:

- **mode** (`demo`|`live`): `readiness.py:118` collapses the effective execution mode to the
  ledger's mode column; the filter is `src/pnl_ledger.py:418-420`.
- **accounting window**: `accounting_start_for()` (`src/control_state.py:235-248`) reads the
  optional per-strategy `accounting_start_at` from `control-state.json`; rows with
  `closed_at_ms` before the floor are excluded from the sum (`src/pnl_ledger.py:268-296`) but
  never deleted from the append-only ledger.

**Net, not gross.** Equity uses `net_realized_pnl = pnl_gross + signed fee_cost`
(`_compute_net_realized`, `src/pnl_ledger.py:67-102`; all venues currently `ORDER_PNL_IS_NET =
False`, `src/pnl_ledger.py:43-49`, i.e. the venue reports gross and HermX subtracts the fee
itself). The **displayed** realized-P&L figure stays gross for now, pending per-venue empirical
fee-semantics verification (`src/pnl_ledger.py:695-696`), so the equity/sizing number is typically
slightly below the gross realized figure the UI shows.

**Fail-safe:** a ledger read error degrades to seed-only sizing with `equity_usd = None`
(`readiness.py:131-137`), and the equity-stop gate never fires on unknown equity
(`service.py:228-231` comment, `:237`).

**Attribution:** exchange history rows carry no `strategy_id`; it is resolved through the
submit-time `{cl_ord_id → strategy_id}` map written when the order is journalled SUBMITTED
(`src/execution/service.py:292-306`, `src/pnl_strategy_map.py:74-106`) and read back during
reconcile (`src/pnl_ledger.py:674-679`).

## 3. Turning compounding on or off

The switch is `capital.reinvest` in the strategy JSON file:

```json
"capital": {
  "budget_usd": 1500,
  "reinvest": true
}
```

| `reinvest` value | What happens |
|---|---|
| `true` (**default — also when the key is absent**, `records.py:91-103`) | Trades size off equity (seed + realized net P&L). Compounding. `equity_usd` is populated in readiness, arming the equity-stop gate. |
| `false` | Trades always size off the fixed seed `budget_usd` (`readiness.py:113-115`). `equity_usd` stays `None`, so the equity-stop gate never fires for this strategy. |

All four current strategies (`btcusdt_duo_base_dev_2h`, `ethusdt_duo_base_dev_2h`,
`solusdt_duo_base_dev_3h`, `xrpusdt_duo_base_dev_4h`) run `reinvest: true` in `demo` mode.

## 4. From budget to an order: the sizing pipeline

1. **Notional** — `planned_notional = sizing_budget × leverage`, decimal-quantized
   (`readiness.py:138-139`), carried as `execution_intent.planned_notional_usd`
   (`readiness.py:190-191`).
2. **Price** — `_reference_price()` (`src/executors/ccxt_adapter.py:578-599`) prefers the signal
   price, then mark/last price from the alert, then the paper execution price, and finally a live
   `fetch_ticker` — `None` on failure yields no order (`zero_size`).
3. **Quantity** — `_contracts_for_notional()` (`src/executors/ccxt_adapter.py:620-642`):
   `qty = floor(planned_notional / (price × contract_size), step)`, with two instrument floors
   from the venue's market limits: `min_cost` (notional floor, `:634`) and `min_amount`
   (`:638`) — either returns `below_instrument_min` and no order is sent.

**Leverage is a sizing multiplier only.** The adapter never calls `set_leverage` on the venue;
`_order_params` (`ccxt_adapter.py:554-576`) sends only client order id, margin mode (`tdMode`),
and `reduceOnly`. Leverage's other runtime use is the pre-trade balance check's required-margin
estimate `notional / leverage` (`ccxt_adapter.py:678`).

## 5. Pre-trade gates that touch budget

Both gates run in `ExecutionService` **before** the write-ahead journal, so a blocked order leaves
no PLANNED row.

**Gate 4 — pre-trade notional cap** (`_check_pretrade_risk`, `src/execution/service.py:23-50`,
wired at `:203-215`):

```
ceiling = min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD env)
refuse if planned_notional_usd > ceiling
```

The ceiling is deliberately **independent-absolute** — never derived from `budget × leverage`,
which could not catch a fat-fingered budget in the same file the notional came from
(`service.py:15-19`). Unset or non-positive ceiling ⇒ no cap; a `None` planned notional (a close)
is a fail-safe pass. `HERMX_MAX_NOTIONAL_USD` is read **once at module load**
(`service.py:20`) — changing the env var requires a restart.

**Gate 6 — reinvest equity stop** (`src/execution/service.py:236-247`): when `equity_usd <= 0`,
opening is blocked with `equity_depleted` / gate `equity_stop`. It only arms when reinvest sizing
actually resolved (`equity_usd is not None`), and a `close_only` record **always passes** — the
same never-block-a-close invariant as the kill switch, symbol pause, and global
`trading_state=reducing` (`service.py:217-223`).

## 6. What happens when the strategy runs out of money

If equity drops to zero or below, HermX stops the strategy from opening **new** trades (Gate 6
above). It will never refuse to **close** an existing position. Recovery is stateless
(`service.py:233-235` comment): the moment equity is positive again — because you raised
`budget_usd` (and restarted), reset the accounting window, or a later close realized a profit —
the very next signal re-arms trading. Nothing needs manual clearing.

## 7. Editing the budget — what takes effect when

Sizing is recomputed per signal, but its inputs have **different freshness**:

| Input | Re-read per signal? | Reference |
|---|---|---|
| `budget_usd`, `reinvest`, `leverage`, `max_notional_usd` (strategy file) | **No — cached at receiver import.** `STRATEGIES = load_strategy_files()` runs once (`src/webhook_receiver.py:500`); per-signal lookup reads the cached dict (`src/signals/normalize.py:127`). There is no reload/watcher. **A file edit requires a receiver restart.** | `webhook_receiver.py:500` |
| Realized net P&L (`closed-trades.jsonl`) | **Yes** — the ledger file is opened on every readiness build (`readiness.py:120-126` → `pnl_ledger.py:258`). | per signal |
| Accounting window (`control-state.json`) | **Yes** — `accounting_start_for()` reads the file per call (`control_state.py:235-248`). | per signal |
| Strategy mode override (`control-state.json`) | **Yes** (`readiness.py:79`). | per signal |
| `HERMX_MAX_NOTIONAL_USD` env | **No** — module-load constant (`service.py:20`). Restart required. | restart |

> ⚠️ The in-code comment at `readiness.py:108-110` ("a mid-flight budget_usd edit … applies on the
> very next signal") is accurate only for the ledger/accounting-window components; the seed itself
> is import-time cached as shown above.

Consequences of an edit (after restart):

- **(a) Open positions are left alone.** No code resizes an existing position when budget
  changes — sizing only happens when a new signal builds a readiness record. The next
  opposite-side signal closes the old position at its original size and opens the new one at the
  new size (`readiness.py:189` — `CLOSE_OPPOSITE_IF_ANY` then `OPEN_*`).
- **(b) The next new trade** sizes off `new seed + realized net P&L so far` — the trading history
  is not reset by the edit; raising or lowering the seed just shifts the equity curve up or down.
  Lowering it can trip the equity stop if it pushes equity to ≤ 0.
- **(c) The pre-trade ceiling** is unaffected unless you also edited `capital.max_notional_usd`
  (that edit likewise needs a restart; the env half needs a restart too).

To **wipe the slate clean** — count P&L from now forward without touching the seed — set an
accounting window instead: `POST /api/control/strategy/{id}` with `accounting_start_at` (ms epoch;
`null` clears) (`src/dashboard/server.py:243-253,301-312` → `src/dashboard.py:270-308`). That
excludes older closes from the equity sum without deleting them from the permanent ledger, and it
takes effect on the next signal with **no restart**.

## 8. Demo vs. live are always separate

Equity is computed only from trades closed in the **same account mode** the strategy is currently
effectively running in (`readiness.py:118`; row filter `pnl_ledger.py:418-420`):

- `demo` mode counts only demo closes; `live` counts only live closes.
- Demo profit can never inflate live sizing, and vice versa. `budget_usd` is the same seed in both
  environments — only the accumulated realized P&L differs.
- Flipping a strategy's mode (control-state override) also flips **which** ledger column its
  equity sums, immediately on the next signal.

## 9. What the dashboard shows — and where it can diverge

**Server-rendered card** (`src/dashboard/render.py:507-559`) shows per strategy: "Seed budget",
"Realized P&L", "UPnL", "Effective budget" (`= seed + realized_net`, `:518`), "Total equity"
(`= seed + realized_net + UPnL`, `:519-520`).

**API (`/api`)**: `_strategy_pnl_contract` (`src/dashboard/model.py:449-511`) calls
`aggregate_strategy_pnl` (`src/pnl_ledger.py:423-465`), whose contract includes `budget_usd`,
`closed_net_pnl_usd`, `open_upl_usd`, and `equity_now_usd = budget + closed_net + upl` (`:461`).
A portfolio roll-up (`portfolio_contract`, `model.py:532-560`) is exposed as `"portfolio"` in the
payload (`model.py:619`).

**Shared math.** The execution path and the server card/API compute effective budget from the same
ledger functions (`net_realized_for_strategy` / `aggregate_strategy_pnl`) with the same mode and
accounting-window scoping — by design ("Phase 5 Decision ⑤A", `readiness.py:104-111`). Two caveats:

1. **The React UI card diverges.** `dashboard-ui/components/StrategyCard.tsx:99-102` computes
   `equityNow = budget + position.realized_pnl + upl` from the **live position snapshot** (which
   resets to 0 when flat), not the durable ledger — so its "Equity now" can differ from the server
   card's "Total equity" and the API's `equity_now_usd`. The API payload does carry
   `strategy_pnl.equity_now_usd`, but the React card does not consume it.
2. **Seed freshness differs until restart.** The dashboard re-reads strategy files per request
   (`src/dashboard/model.py:107,137,373`), while the receiver caches them at import (§7) — after a
   file edit and before a restart, the dashboard shows the new seed while execution still sizes
   off the old one.

**Budget is display-only on the dashboard.** No endpoint writes `budget_usd`, `capital`, or any
strategy file. `POST /api/control/strategy/{id}` accepts only `mode` and `accounting_start_at`
(`src/dashboard/server.py:212-257`), writing to `control-state.json`; there is no strategy CRUD.
The shipped React UI only sends `mode` (`dashboard-ui/lib/api.ts:74`), so accounting windows are
settable via the raw API only.

**No equity curve exists.** Equity is point-in-time only ("Effective budget" / "Total equity" /
`equity_now_usd`). The closest historical element is the alert table's per-row `equity_after` /
"Budget after" columns (`render.py:259,264`) — per-alert snapshots, not a time series. The
portfolio aggregate is exposed by the API but not rendered by any React component.

## 10. Where the realized P&L comes from

`closed-trades.jsonl` is the append-only lifetime ledger (never pruned —
`src/pnl_ledger.py:1-11`). Rows are written by `reconcile_from_order_history`
(`pnl_ledger.py:708-799`), invoked from the dashboard snapshot reconcile
(`src/dashboard/snapshots.py:372-374,546-549`), with write-side dedup under a full-cycle
`fcntl.flock` (`pnl_ledger.py:588-607`) and read-side dedup by `(exchange, inst_id, ord_id, mode)`
last-wins (`pnl_ledger.py:319-333`). Each row carries `pnl_gross`, signed `fee_cost`,
`net_realized_pnl`, `mode`, and the attributed `strategy_id` (`pnl_ledger.py:680-705`).

## 11. Current strategy budgets

Total assigned demo margin across all strategies: **$6,000** (verified against
`strategies/*.json`).

| Asset | Seed budget | Leverage | Fixed target notional |
|---|---:|---:|---:|
| BTCUSDT | $1,500 | 2x | $3,000 |
| ETHUSDT | $1,500 | 2x | $3,000 |
| SOLUSDT | $1,500 | 2x | $3,000 |
| XRPUSDT | $1,500 | 2x | $3,000 |

"Fixed target notional" is what each strategy trades at `reinvest: false`, or on its first-ever
trade before any P&L has accrued. With `reinvest: true` (the current setting for all four), each
strategy's actual notional compounds independently based on its own realized results.

## Reference: definitions

| Term | Meaning |
|---|---|
| Budget seed (`capital.budget_usd`) | Operator-set starting capital; changes only on a manual strategy-file edit **plus receiver restart** (`records.py:80-88`, `webhook_receiver.py:500`). |
| Realized net P&L | Σ `net_realized_pnl` over closed-trade rows, scoped to this strategy, this account mode (demo/live), and the accounting window if set (`pnl_ledger.py:404-420`). Net = gross + signed fee (`pnl_ledger.py:67-102`). |
| Equity | `seed + realized net P&L` — what order sizing uses when `reinvest: true` (`readiness.py:127`). |
| Sizing budget | `max(equity, 0)` — the clamped figure notional is derived from (`readiness.py:130`). |
| Open UPnL | Unrealized P&L on a currently open position. Shown on the dashboard and included in `equity_now_usd`; **not** part of order sizing. |
| Total equity (dashboard) | `equity + open UPnL` (`render.py:519-520`; API `equity_now_usd`, `pnl_ledger.py:461`). |
| Accounting window | Optional per-strategy reset point (`accounting_start_at` in `control-state.json`) excluding closed trades before it from the equity sum, without deleting them (`control_state.py:191-248`, `pnl_ledger.py:268-296`). |
| Notional ceiling | `min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD)` — independent absolute pre-trade cap (`service.py:23-50`). |

## Important rule

Demo and live budgets/equity must never be mixed, and the dashboard should always make clear
whether a number comes from:

- historical paper replay
- sandbox execution (`execution_mode: demo` — routes to the sandbox/paper account; treated as
  `simulated_trading`)
- live execution (`execution_mode: live` — the only real-money mode; requires
  `HERMX_LIVE_TRADING=true`)
