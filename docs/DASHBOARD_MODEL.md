# HermX Dashboard — Operator Guide

How to read the dashboard, track strategies and trades, and use the controls.
This is an operator guide; for signal semantics see [ALERT_CONTRACT.md](ALERT_CONTRACT.md),
for CLI/agent controls see [hermx-slash-commands.md](hermx-slash-commands.md).

---

## 1. What it is and where to find it

The dashboard is a local web app served by `src/dashboard.py`:

- **URL:** `http://127.0.0.1:8098` (port from `HERMX_DASHBOARD_PORT`, default `8098`; binds loopback unless `HERMX_BIND_HOST` is set, e.g. in the Docker compose).
- **Auth:** every page and API route requires the `HERMX_SECRET` token. The browser prompts via Basic auth — leave username blank, paste the secret as the password. Tools/scripts can use `Authorization: Bearer <secret>` or `X-Dashboard-Token: <secret>`. If auth is enabled but the secret is blank, everything fails closed with 401.
- **UI:** a Next.js single-page app built into `dashboard-ui/out/`. If that directory is missing, the server silently falls back to a legacy server-rendered HTML page (`render()` in `src/dashboard/render.py`) — same data, older look. See [Troubleshooting](#8-troubleshooting).
- **Pages:** the main page (`/`) and a **System Health** page (`/health` route inside the SPA, i.e. `/dashboard/health/`).

Data flow and freshness: the backend rebuilds its model in a background loop every
15 s (`_refresh_dashboard_cache_loop`) and serves the cached model from `/api`; the
UI polls `/api` + `/health` every 10 s. So numbers can lag reality by up to ~25 s.
The backend computes a `freshness` verdict from true data age (not render time), so
a hung feed shows as stale rather than quietly looking current.

## 2. Page layout (top to bottom)

Component names match files in `dashboard-ui/components/`.

| Section | Component | What it shows |
|---|---|---|
| Arming strip | `ArmingBanner` | Full-width banner: red **LIVE TRADING ARMED — N live strategies**, amber **DEMO MODE — N strategies, kill switch active**, or muted **System disarmed**. Driven by `health.arm` (see §3). |
| Header | `TopBar` | Title, live dot, "Updated Ns ago" (or the last fetch error in red). |
| Summary row | `SummaryCards` (4 × `StatCard`) | **SYSTEM STATUS** (ARMED / DEMO / DISARMED), **STRATEGIES** (count, demo/live split), **OPEN POSITIONS** (count, longs/shorts), **EXECUTION ENGINE** (`Engine - OK / STALE / ERROR` plus a Hermes advisor line). |
| Strategy cards | `StrategyGrid` → one `StrategyCard` per file in `strategies/*.json` | Per-strategy state: symbol, timeframe, config badges (indicator, leverage, margin mode, instrument type, exchange), the **Pause / Demo / Live** mode pill, position side badge (LONG/SHORT/FLAT + live dot), Budget, Equity now, UPnL, Mark price, Alerts count. |
| Execution ledger | `ExecutionLedger` | Trade rows from the execution pipeline: Time, Asset, Side, Fill Px, Notional, State (FILLED green / REJECTED red / UNKNOWN amber), PnL. Skipped (`not_submitted`) rows are filtered out. |
| Strategy alerts | `StrategyAlertLog` | Every TradingView signal matched to a strategy: TV time, strategy, side, price, decision (TRADE / SKIP / DUPLICATE / BLOCKED), block reason, latency. |
| Open orders | `OpenOrdersTable` | Order-journal rows in non-terminal states (everything not FILLED/REJECTED — i.e. in-flight and **UNKNOWN** orders). |
| Reconcile alerts | `ReconcileAlerts` | Rows from `logs/alerts.jsonl` with `kind="reconcile"` — reconcile mismatches, including **position drift** (`stage="position_drift"`, journal vs venue quantity; observe-only, never auto-corrected). |
| Operator alerts | `OperatorAlerts` | Rows with `kind="operator"` — operator actions and warnings, with severity badges. |

The **/health page** shows the arming status as cards (Kill Switch ENGAGED/CLEAR,
Live Trading ENABLED/DISABLED, Armed ARMED/SAFE, demo/live strategy counts) plus
executor status and service info.

## 3. Tracking strategy state

### The mode pill: Pause / Demo / Live

Each strategy card has a three-state pill wired to `POST /api/control/strategy/{id}`:

- **Pause** — no orders are submitted for this strategy (signals are still validated and logged).
- **Demo** — orders go to the sandbox/demo account.
- **Live** — orders go to the real-money account. The Live button is **locked (🔒)** unless the global kill switch is released with `HERMX_LIVE_TRADING=true`.

Clicking the pill writes an override into `control-state.json` (`strategy_overrides`);
it does **not** edit the strategy file. Resolution order (`_effective_strategy_mode`
in `src/dashboard/model.py`): override → strategy file (`submit_orders: false` ⇒
pause, else `execution_mode`). Clearing the override (API `mode: "clear"` or
`/hx-strategy-mode <id> resume`) reverts to the file's mode. Legacy labels in old
control-state files are remapped: `shadow` → `pause`, `paper` → `demo`.

### The arming banner

Two independent controls must agree before real money moves:
per-strategy `execution_mode = live` **and** the global `HERMX_LIVE_TRADING` kill
switch. `armed` in `/health` is true only when at least one strategy is live *and*
live trading is enabled. So:

- **Red "LIVE TRADING ARMED"** — real orders can reach a real account. Expected only when you intend it.
- **Amber "DEMO MODE … kill switch active"** — strategies exist but the kill switch keeps everything off real accounts.
- **"System disarmed"** — no strategies loaded at all.

### Global trading state: `active` vs `reducing`

A separate risk-off switch, stored as `trading_state` in `control-state.json` and
returned in the `/api` payload:

- **`active`** — normal trading.
- **`reducing`** — new opens/reversals are blocked; **closes always pass** (`close_only=True` signals bypass the gate). This is the safe "wind down" state — emergency flatten still works.

There is **no button for this in the UI** — set it via the API (§5) or check the
`trading_state` field in `/api`. Unknown/legacy values read as `active`.

### Symbol pauses

`control-state.json` also carries `symbol_pauses` — a per-symbol hard block on
submission. Pauses are set automatically by the UNKNOWN-order resolver
(`src/reconcile/unknown_resolver.py`) when an order gets stuck in an unresolvable
state, or manually via `/hx-emergency-stop pause-symbol <sym>`. A paused symbol
rejects all new submissions **except closes**. There is no UI toggle; inspect
`control-state.json` or use `/hx-strategy-list`, and clear via the slash-command
path once the symbol is safe.

## 4. Tracking trades and P&L

### Which number to trust

Two kinds of data appear side by side; they have different authority:

- **Live panel** (positions, UPnL, mark price on the cards) — a best-effort venue readback at build time. Informational; may be stale or unavailable. When the read fails or ages out, the Engine card shows **ERROR/STALE** rather than silently rendering "flat".
- **Ledgers** — the durable record. If the live panel and a ledger disagree, the ledger wins for "what happened"; the live panel only answers "what does the venue look like right now". An order the journal holds as UNKNOWN stays UNKNOWN even if the venue looks flat — reconciliation resolves it, not the dashboard.

The dashboard never invents positions and never mutates the money path; its only
writes are the control-state fields in §5.

### The files behind the panels

| File | Feeds | Contents |
|---|---|---|
| `logs/pipeline.jsonl` | ExecutionLedger (`stage="execution"`), StrategyAlertLog (`stage="strategy_match"`) | Every signal-processing event, tagged by stage. |
| `logs/alerts.jsonl` | ReconcileAlerts / OperatorAlerts (`kind="reconcile"` / `"operator"`) | Reconcile mismatches, drift, operator actions. |
| `logs/order-journal.jsonl` (+ checkpoint) | OpenOrdersTable | Order lifecycle `PLANNED → SUBMITTED → (FILLED \| REJECTED \| UNKNOWN)`. The panel shows non-terminal rows. |
| `closed-trades.jsonl` (in `HERMX_DATA_DIR`) | `strategy_pnl` / `portfolio` in `/api` | **Append-only lifetime P&L ledger.** Never rotated or pruned. Deduped at read time by (exchange, inst_id, ord_id, mode). |

### Per-strategy P&L: the `strategy_pnl` contract

Each strategy in the `/api` payload carries a `strategy_pnl` object
(`_strategy_pnl_contract`), built from the closed-trades ledger scoped to the
strategy, its account mode (demo|live), and its accounting window:

- `realized_gross` — sum of venue-reported closed P&L, **before fees**.
- `fees` — summed fees; `realized_net = gross + signed fees`.
- `upl` — open unrealized P&L from the strategy's own (venue, mode) snapshot.
- `total_net = realized_net + upl`; plus `trade_count`, `last_close_at_ms`, `budget_usd`, `equity_now_usd`.

**Gross vs net:** `ORDER_PNL_IS_NET` (`src/pnl_ledger.py`) is `False` for every
venue until its fee-inclusion semantics are verified empirically on a real close.
Until then treat **gross as the authoritative displayed figure**; `realized_net`
is a best-effort derivation.

A top-level `portfolio` object rolls the same fields up across all strategies.

**Caveat:** the `StrategyCard` "Equity now" and "UPnL" numbers come from the *live
position readback* (`budget + position.realized_pnl + upl`), not from the durable
ledger. For accounting-grade numbers, read `strategy_pnl` from `/api` directly
(workflow in §6).

### Accounting windows

A per-strategy `accounting_start_at` (ms epoch) in `control-state.json` scopes all
`strategy_pnl` figures to trades at/after that timestamp — a "clean slate" for a
strategy without deleting ledger history. Set/clear via the API (§5); the current
value is echoed as `accounting_start_at` on each strategy and in the
`accounting_windows` map in `/api`.

## 5. API endpoints and controls

All routes live on the dashboard server (same port). Read routes:

| Route | Returns |
|---|---|
| `GET /api` | Full model: `strategies` (with `effective_mode`, `venue`, `strategy_pnl`, `accounting_start_at`), `portfolio`, `trading_state`, `strategy_overrides`, `accounting_windows`, `okx_live` / `okx_live_by_env` positions, `okx_executions`, `strategy_alerts`, `open_orders`, `reconcile_alerts`, `operator_alerts`, `executor` / `ledger_health` / `freshness` / `reconcile_health` verdicts. |
| `GET /health` | Service + arming: `arm.kill_switch_engaged`, `arm.live_trading_enabled`, `arm.armed`, demo/live counts, `strategy_files`. |
| `GET /api/signals?n=50&symbol=BTCUSDT` | Last *n* execution events (TV-triggered + operator closes), most recent first. `n` capped at 500. |

Control routes (all require auth; `TOKEN` is `HERMX_SECRET`):

```bash
# Pause / resume / switch a strategy's mode (writes strategy_overrides)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode": "pause"}' http://127.0.0.1:8098/api/control/strategy/<strategy_id>
# mode: "pause" | "demo" | "live" | "clear"  (clear = revert to file default)

# Same thing via DELETE (equivalent to mode:"clear")
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8098/api/control/strategy/<strategy_id>

# Start a clean accounting window (ms epoch); null clears it
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"accounting_start_at": 1751500800000}' \
  http://127.0.0.1:8098/api/control/strategy/<strategy_id>

# Risk-off: block new opens, allow closes
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"state": "reducing"}' http://127.0.0.1:8098/api/control/trading-state
# Back to normal ("active"); DELETE on the same route also resets to active
```

A strategy-control POST may combine `mode` and `accounting_start_at` in one body.
Unknown strategy IDs return 404; invalid modes/states return 400. The UI's mode
pill is the only control with a button — trading state and accounting windows are
API/slash-command only (`/hx-strategy-mode`, `/hx-emergency-stop`).

## 6. Common workflows

**"Is strategy X losing money?"**
Don't eyeball the card — pull the durable contract:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8098/api \
  | jq '.strategies[] | select(.strategy_id=="X") | .strategy_pnl'
```
Read `realized_gross` (authoritative), `fees`, `upl`, `total_net`, `trade_count`.
Remember it's scoped to the accounting window if one is set.

**"Pause a strategy during high volatility."**
Click **Pause** on its card (or `POST … {"mode":"pause"}`). Signals keep being
logged, nothing is submitted. Click **Demo**/**Live** or send `mode:"clear"` to
resume. To go risk-off across *all* strategies while still allowing position
exits, set `trading_state` to `reducing` instead.

**"Verify a trade was recorded."**
1. **StrategyAlertLog** — the TV signal arrived and was matched (decision TRADE).
2. **ExecutionLedger** — a row with State FILLED and a fill price/notional.
3. **OpenOrdersTable** — the order should *not* linger here; a stuck UNKNOWN row means the venue outcome is unconfirmed (reconciliation will resolve or pause the symbol).
4. For the P&L side of a close, confirm `strategy_pnl.trade_count` ticked up (or grep `closed-trades.jsonl` for the `ord_id`).

**"Something looks wrong with positions."**
Check **ReconcileAlerts** for `position_drift` rows — journal vs venue quantity
mismatches are detected and alerted (observe-only). Also check `symbol_pauses` in
`control-state.json`: a symbol the resolver paused stops trading silently from the
UI's perspective.

**"Start fresh P&L tracking after retuning a strategy."**
Set `accounting_start_at` to now (§5). Historical trades stay in the ledger but
drop out of the strategy's displayed P&L.

## 7. Reading the health signals

- **Engine - OK / STALE / ERROR** (SummaryCards): the executor-read verdict (`executor_health_summary`). ERROR = the venue readback failed (`executor.error` has the reason); STALE = the last good read is older than the refresh interval. In both cases position/UPnL data on cards is untrustworthy — the ledger panels remain valid.
- **"Updated Ns ago"** (TopBar) going red or growing: the UI can't reach `/api` — server down, auth changed, or network.
- **`ledger_health`** in `/api` (and "N skipped" footers on tables): corrupt/truncated ledger lines encountered by the bounded reader. Occasional torn trailing lines are tolerated; a growing skip count is not.
- **`freshness.stale`** in `/api`: true data age (newest alert / venue read) exceeds the refresh interval — feeds may be down even if the server responds.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard renders as plain, old-style HTML | `dashboard-ui/out/` is missing — the SPA wasn't built or the Docker image omitted `COPY dashboard-ui/out`. Run `npm run build` in `dashboard-ui/`; the server picks it up on next request. |
| 401 on every route | `HERMX_SECRET` unset/blank while auth is on (fail-closed), or the wrong token. In the browser, use the Basic prompt with the secret as the *password*. |
| `Engine - ERROR` | Executor construction or venue readback failed: bad/missing venue credentials, venue outage, or a misresolved venue (historically: empty config made the executor fall back to backend name `"ccxt"` instead of a real venue). See `executor.error` in `/api`. |
| `Engine - STALE` | Background rebuild is slow or wedged (venue API latency). The last good model keeps being served; check receiver/dashboard logs. |
| Mode pill click seems to work, then reverts | The write to `control-state.json` failed silently — in Docker, the dashboard needs a **writable** `HERMX_DATA_DIR` mount even with `read_only: true` on the root fs. |
| Live button locked | Expected: `HERMX_LIVE_TRADING` is not `true`. That's the kill switch doing its job. |
| Cards show FLAT but you know a position is open | Check Engine status first — an errored/stale read must never be trusted as flat. Then check OpenOrdersTable for UNKNOWN rows and ReconcileAlerts for drift. |
| Strategy shows $0 P&L despite closed trades | Trades reconciled from exchange history carry no strategy attribution unless the submit-time map recorded it; also check whether an `accounting_start_at` window excludes them. |
| Dashboard empty / wrong strategies | `HERMX_ROOT` / `HERMX_DATA_DIR` point at the wrong tree — strategies come from `$HERMX_ROOT/strategies/*.json`, ledgers from `$HERMX_ROOT/logs/`. |
