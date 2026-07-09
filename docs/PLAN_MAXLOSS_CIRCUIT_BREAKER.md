# Max-Loss Circuit Breaker — Design Proposal

> Status: **Planning only — not yet implemented.** Produced via CC2 codebase analysis, 2026-07-09.

## 0. Key discovery that shapes everything below

HermX **already has a working circuit breaker**: the **Gate 6 equity stop** at `src/execution/service.py:225-247`. It reads `readiness.equity_usd` (= seed budget + realized net P&L, computed in `src/strategy/readiness.py:114-136` via `net_realized_for_strategy`), blocks new opens when equity `<= 0`, and **bypasses `close_only`**. The max-loss circuit breaker is a *thresholded, stateful, manual-resume* generalization of Gate 6 — not a greenfield feature.

The critical difference: Gate 6 is **stateless auto-recovery** (`service.py:234`: "Recovery is stateless: raise budget_usd, reset the accounting window, or realize a profit and the next signal re-arms"). This feature explicitly forbids auto-resume, so it needs **durable paused state** — which Gate 6 does not have.

**The single most important finding for the design:** the existing per-strategy **"pause" override is NOT close-safe.** `strategy_overrides` pause sets `submit_orders=False` (`control_state.py:150-154`), which makes `readiness.live_execution_enabled=False` (`readiness.py:87-93`), which trips **Gate 1 `strategy_submit_flag`** at `service.py:138-155`. **Gate 1 does not bypass `close_only`** — unlike Gates 3/5/6. So pausing a strategy via the existing `strategy_overrides` mechanism would **block its closes and violate the never-block-a-close invariant.** This forces a new, distinct, close-safe state (answers Q3 decisively).

---

## Q1. Where the max-loss CHECK should live

**Recommendation: (c) both — a synchronous post-record check as the primary trigger, plus the existing Hermes cron reconcile gate extended as a slower backstop.**

### How closes are recorded today (determines feasibility)

`src/execution/service.py` contains **no** `pnl_ledger` append — confirmed by grep; a close in the execution service is just a `close_only` readiness leg (`service.py:299-303`), delegated to the adapter at `service.py:318`. The `closed-trades.jsonl` row is written **downstream** by `pnl_ledger.append_closed_trades` (`pnl_ledger.py:574-608`), driven by **reconciliation** (`reconcile_from_order_history`, `pnl_ledger.py:708-799`) invoked from the receiver / dashboard model / `reconcile/unknown_resolver.py` — **not synchronously at close time**. There is no "trade just closed" callback in the hot path.

This matters: a close's P&L is only known *after* the venue fills and reconcile folds it in. So a "synchronous post-close hook" cannot live in `service.execute()` (the P&L isn't computed there). It must attach to the **one place the P&L number is actually produced**: inside `append_closed_trades` (or immediately after it, in `reconcile_from_order_history`).

### Tradeoffs

| Option | Latency | Complexity | Reliability |
|---|---|---|---|
| **(a) Post-record hook** in `append_closed_trades` (`pnl_ledger.py:574-608`) | Fires the moment a loss is durably recorded — before the *next* signal's opens are evaluated | Low: one call after the fsync'd write, inside the existing `_LOCK`/`flock` section. Runs in whatever process reconciles (receiver or dashboard) | High for the primary path, but only fires when reconcile runs. If reconcile is lagging, the pause lags too |
| **(b) Cron-only** new `hermx-maxloss-gate.py` reading `closed-trades.jsonl` on a schedule | Bounded by cron cadence (5–15 min, like `hermx-reconcile-gate.py`) | Medium: new stdlib gate script, but the pattern is proven | Robust/independent, but a fast bleed can open several more losers inside one cron window |
| **(c) Both** | Fast local pause + cron backstop | Low+Medium | Best: cron catches anything the hook missed (reconcile lag, crash between record and hook) |

**Justification for (c):** the money invariant here is "never let a bleeding strategy open the *next* trade." The synchronous hook is what actually enforces that latency-wise, because the paused state is read by Gate 4.5 (see Q3) on the very next signal. But `append_closed_trades` runs inside reconciliation, which can lag (there's literally a `hermx-reconcile-lag-gate.py` monitoring that lag) — so a cron backstop that recomputes the streak/drawdown directly from `closed-trades.jsonl` and sets the same paused flag closes the reliability gap. The cron gate reuses the exact structure of `hermx-reconcile-gate.py:198-199` (already reads `closed-trades.jsonl` via `hermx_ops._iter_jsonl`).

**Do NOT** put the check in `service.execute()` — the P&L is not available there, and it would be re-implementing the ledger read on the hot path.

---

## Q2. Exchange-native hard stop-loss at submission — feasibility per venue

**Recommendation: worth doing as defense-in-depth, but as a *separate, later* phase — it is NOT required for the circuit breaker and is a shared-library change requiring explicit confirmation per `dev-rules.md` #4.**

### Current state

There is **no exchange-native stop-loss, take-profit, trigger, or conditional-order support anywhere** in the execution layer. Repo-wide grep for `stopLoss|takeProfit|triggerPrice|stopPrice|conditional` returns zero submission hits. The only close/reduce mechanism wired is CCXT unified **`reduceOnly`** (`ccxt_adapter.py:573-574`, set on close legs only).

`_order_params()` (`ccxt_adapter.py:554-576`) emits exactly: `clOrdId`/`clientOrderId`, `tdMode`, `reduceOnly`. Order type (`ccxt_adapter.py:833-834`) recognizes `market`/`limit`/`stop`/`stop_limit` but **only decides whether to attach a price — it never sets a trigger price**, so `stop`/`stop_limit` are non-functional as conditionals today.

### Per-venue feasibility (single `CcxtExecutor` handles all venues — `factory.py:18-21`)

Supported venues (`ccxt_adapter.py:348-406`): **okx, kucoin, bybit, hyperliquid, binance, bitget, gate/gateio, coinbase**.

| Venue | CCXT unified stop support | Feasibility |
|---|---|---|
| okx | `params={"stopLossPrice"}` / `slTriggerPx` on `create_order` | Feasible |
| bybit | `stopLossPrice`/`triggerPrice` | Feasible |
| binance (futures) | `stopLossPrice`/`STOP_MARKET` | Feasible |
| bitget | `stopLossPrice`/`triggerPrice` | Feasible |
| gate/gateio (futures) | `stopLossPrice` | Feasible |
| kucoin (futures) | `stopLossPrice` (kucoinfutures) | Feasible; spot kucoin no |
| hyperliquid | trigger orders via `params={"triggerPrice","reduceOnly"}` | Feasible but bespoke (hashed cloid handling, `ccxt_adapter.py:88-94`) |
| coinbase | limited/none for advanced-trade perps | Not reliably |

**What it takes:** add trigger params to `_order_params()` (`ccxt_adapter.py:554`) gated by a **per-venue capability table** exactly analogous to the existing `_leverage_params()` table (`ccxt_adapter.py:578-604`), because param keys differ per venue. This is a shared-library change (`ccxt_adapter.py` is a Key File) → requires explicit confirmation.

**Why it's phase-2, not phase-1:** the internal circuit breaker protects *across* trades (streak, cumulative drawdown, next-open blocking), which an exchange-native SL cannot do — an exchange SL only bounds a *single* position's excursion. They're complementary. Native SL also introduces failure modes HermX currently avoids (orphaned triggers after a manual close, venue-side SL rejection, trigger param drift per CCXT version). Ship the internal breaker first (pure HermX state, fully testable offline), then add native SL as belt-and-suspenders.

---

## Q3. Integration with `trading_state` — new distinct state required

**Recommendation: a NEW per-strategy `strategy_circuit` store in `control-state.json` — NOT a reuse of `reducing`, and NOT a reuse of `strategy_overrides` "pause".**

### Why `reducing` is insufficient

`trading_state` is **global** (`control_state.py:255`, valid `{active, reducing}`) and enforced at **Gate 5** (`service.py:217-223`), which correctly bypasses `close_only`. But:
1. It's system-wide — one bleeding strategy would halt *all* opens.
2. It's not machine-distinguishable from an operator-initiated risk-off. The codebase deliberately collapsed to one extra state (`control_state.py:251-254`), so overloading it would erase the "why" and conflate an automated max-loss trip with a human decision.

### Why `strategy_overrides` "pause" is UNSAFE (the decisive finding)

Reusing per-strategy pause would **block closes**: pause → `submit_orders=False` (`control_state.py:150-154`) → `live_execution_enabled=False` (`readiness.py:87-93`) → **Gate 1 `strategy_submit_flag`** blocks the order (`service.py:138-155`). **Gate 1 does not bypass `close_only`** (unlike Gates 3/5/6). This violates the never-block-a-close invariant. Rejected.

### Proposed new state

Add a per-strategy dict-valued key `strategy_circuit` to `default_control_state()`:

```
strategy_circuit: {
  "<strategy_id>": {
    "state": "halted_manual_review",     # machine-distinguishable
    "reason": "single_trade_loss" | "consecutive_losses",
    "detail": "loss_pct=6.2 threshold=5.0" | "streak=3 threshold=3",
    "tripped_at": "<iso>",
    "tripped_by": "hook" | "cron",
    "mode": "demo" | "live",             # scope the halt to the account mode that bled
    "trigger_close_ids": [...]           # audit trail
  }
}
```

Enforced by a **new Gate 4.5** in `service.py`, placed right after Gate 4 (notional) and before Gate 5, mirroring Gate 6's structure exactly:

```python
# Gate 4.5 -- per-strategy max-loss circuit breaker. Blocks OPENS for a strategy
# that tripped a single-trade or consecutive-loss limit. Requires MANUAL resume.
# close_only ALWAYS passes -- same invariant as kill switch / symbol pause / equity stop.
sid = readiness.get("strategy_id")
if sid and not readiness.get("close_only"):
    circ = self._h("strategy_circuit_state")(sid)   # reads control-state.json
    if circ and circ.get("state") == "halted_manual_review":
        return _blocked(f"circuit_halted:{circ.get('reason')}", "max_loss_circuit")
```

This inherits the proven "`... and not readiness.get('close_only')`" guard from Gates 3/5/6, so closes pass by construction.

### The merge-filter gotcha (already known)

`strategy_circuit` is **dict-valued**, so `load_control_state()`'s merge filter at `control_state.py:88` (`{k: v ... if k in default}`) will **drop it** unless it is (a) added to `default_control_state()` (`control_state.py:37-57`) **and** (b) **explicitly re-attached** from raw `state` after the merge, exactly as `symbol_pauses` (line 89), `strategy_overrides` (line 90), and `accounting_windows` (line 94) already are. This is the exact class of bug the codebase memory flags. Missing this = the pause silently vanishes on the next state load — catastrophic for a safety feature.

---

## Q4. Exact trigger logic

### "X% of capital" — per-strategy `capital.budget_usd`, NOT global

Compute against the **strategy's own seed budget** via `strategy_budget_usd(strategy)` (`strategy/records.py:80-88`, reads `capital.budget_usd` with flat fallback), which is exactly what Gate 6 / reinvest sizing already use (`readiness.py:112`). Global account equity (`_account_equity_estimate`, `pnl_ledger.py:495-552`) is a *best-effort synthetic* and mixes strategies — wrong granularity for a per-strategy pause. Per-strategy budget is the authoritative, operator-set denominator.

- **Single-trade trip:** `abs(min(row.net_realized_pnl, 0)) / budget_usd >= X%` for any single close row.
- **Consecutive-loss trip:** N consecutive closes (ordered by `closed_at_ms`) each with `net_realized_pnl < 0`.

Optionally also a **cumulative-drawdown** trip: `sum(net_realized_pnl over accounting window) <= -X% * budget_usd` — this is the natural stateful extension of Gate 6 and reuses `aggregate_strategy_pnl` (`pnl_ledger.py:423-465`, returns `closed_net_pnl_usd`, `equity_now_usd`).

### Streak tracking & reset

**No streak state exists today** (confirmed — `pnl_ledger.py` has only summation, no consecutive/streak logic). Do NOT persist a mutable counter (it would need its own locking and could desync from the ledger). Instead **derive the streak from the ledger on demand** — the ledger is the source of truth:

```
rows = read_closed_trades(strategy_id=sid, mode=mode, accounting_start_at=win)   # pnl_ledger.py:258
rows.sort(key=lambda r: r["closed_at_ms"])
streak = count trailing rows where net_realized_pnl < 0, stopping at first >= 0
```

Reset is implicit: **any winning (or break-even) close truncates the trailing-loss count to zero.** This is stateless, matches the "recomputed per signal" philosophy at `readiness.py:108-109`, and can't drift from the ledger. The synchronous hook (Q1a) just recomputes this over the last N+1 rows for the strategy after each append — O(N), trivial.

### Field sufficiency — confirmed, no new instrumentation needed

Every field required is already in the `closed-trades.jsonl` row (`_build_entry`, `pnl_ledger.py:680-705`):

| Need | Field | Line |
|---|---|---|
| realized P&L (net of fees) | `net_realized_pnl` | 697 |
| realized P&L (gross, if `ORDER_PNL_IS_NET` semantics matter) | `pnl_gross` | 692 |
| strategy attribution | `strategy_id` | 688 |
| ordering / recency | `closed_at_ms` (venue), `recorded_at_ms` (local) | 698, 699 |
| account scope | `mode` (`demo`/`live`) | 685 |
| dedup identity | `exchange`, `inst_id`, `ord_id`, `cl_ord_id` | 682-684, 700 |

**One caveat:** `strategy_id` is `None` when reconcile couldn't resolve it via the submit map (`pnl_ledger.py:674-679`; documented failure mode in codebase memory). Rows with `strategy_id=None` **must be excluded** from streak/drawdown math (they can't be attributed), and `read_closed_trades(strategy_id=sid)` already filters by exact match (`pnl_ledger.py:297`), so this is handled — but it means the breaker is only as complete as submit-map attribution. Note this as a known coverage limit.

---

## Q5. Notification / surfacing — all three

A safety pause needs redundant surfacing (a silently-tripped breaker that no operator sees is as bad as an inert monitor — a pattern the codebase already warns about):

1. **Log line** (always): the Gate 4.5 block logs a `logging.warning(...)` exactly like the equity stop (`service.py:243-247`), and the trip event logs a `WARNING` at the point of trip (in the hook and in the cron gate).
2. **Dashboard banner** (`src/dashboard/render.py`): add a `banner(..., "danger")` in `status_banners()` (`render.py:894-903`, next to the existing EXECUTOR ERROR / ledger-skip banners), sourced from a new `circuit_halts` field in `api_payload()` (`model.py:709-725`) so the React SPA also renders it. Surface it on the per-strategy card (the mode pill area, `model.py:535` `_strategy_pnl_contract`) as a "HALTED — REVIEW" state.
3. **Hermes cron alert** (`deploy/hermes-scripts/`): the new `hermx-maxloss-gate.py` (the Q1 backstop) emits the standard `wakeAgent` JSON / plain-text stdout (`hermx_gate_lib.py:148-156`) → Hermes → Telegram, deduped via the suppression sidecar (`hermx_gate_lib.py:85-101`). This is the push notification to the operator when they're not looking at the dashboard.

---

## Q6. Manual resume mechanism

### control_state.py functions (mirror the existing trading-state helpers)

Add, next to `set_trading_state`/`clear_trading_state` (`control_state.py:258-282`):

```python
def trip_strategy_circuit(strategy_id, *, reason, detail, mode, tripped_by): ...   # writes strategy_circuit[sid]
def clear_strategy_circuit(strategy_id): ...                                        # removes the entry (manual resume)
def strategy_circuit_state(strategy_id) -> dict: ...                                # read for Gate 4.5
```

`clear_strategy_circuit` is the **only** resume path — no auto-clear anywhere (the hook and cron only ever *trip*, never clear). All three go through `load_control_state`/`save_control_state` (`control_state.py:60-105`) with the re-attach fix from Q3.

### Dashboard/API endpoint (mirror the trading-state route)

Model it on the existing operator-action pattern (`server.py:185-206`, auth-gated via `_dashboard_auth_ok`):

```
Trip status (read):     GET  /api/control/strategy/{id}/circuit
                        200 -> {"state":"halted_manual_review","reason":"consecutive_losses","detail":"streak=3","tripped_at":"...","mode":"live"}

Manual resume (clear):  POST /api/control/strategy/{id}/circuit/resume
  Headers: X-Dashboard-Token: <HERMX_SECRET>          # existing auth
  Body:    {"acknowledge": true, "note": "reviewed - restarting"}   # note persisted for audit
  200 ->   {"ok": true, "strategy_id": "...", "state": "active", "resumed_at": "..."}
  409 ->   {"ok": false, "error": "not_halted"}       # nothing to resume
```

Use a distinct verb path (`/circuit/resume`), **not** `DELETE`, to force an explicit acknowledgement payload — a manual review should not be a bare idempotent delete. Reuse the `do_POST` auth/parse/dispatch scaffolding at `server.py:261-278`. The dashboard is otherwise read-only w.r.t. orders (`render.py:811`), but this only mutates `control-state.json` — same class of write as the existing mode/state toggles, so it fits the security posture.

---

## Q7. Implementation options, ranked

### Option A — Cron-only backstop (lowest risk, higher latency) ⭐ *ship first*
New `hermx-maxloss-gate.py` reads `closed-trades.jsonl` (`hermx_ops._iter_jsonl`, like `hermx-reconcile-gate.py:198-199`), computes per-strategy streak/drawdown, and on trip calls `trip_strategy_circuit`. Gate 4.5 + control_state helpers + dashboard banner ship alongside.
- **Risk:** lowest — no hot-path change; the cron script is stdlib, testable, and the only src change is the read-only Gate 4.5 + control_state store (well-trodden merge pattern).
- **Latency:** cron cadence (5–15 min). A fast bleed can open 1–2 more losers within a window.
- **Files:** `control_state.py`, `service.py` (Gate 4.5, read-only), new cron script, dashboard render/model. Borderline on the 3-file rule → break into: (1) control_state store + Gate 4.5, (2) cron gate + tests, (3) dashboard surfacing.

### Option B — Synchronous hook + cron backstop (recommended overall) ⭐⭐
Option A **plus** a post-record hook in `append_closed_trades` (`pnl_ledger.py:574-608`) that recomputes the streak/drawdown for the just-closed strategy and trips immediately.
- **Risk:** medium — touches `pnl_ledger.py` (a Key/money file). Mitigate: hook is **best-effort try/except** (never let a breaker-eval error corrupt a ledger write — same log-and-continue posture as the fee-mismatch handling), runs *after* the fsync, inside the existing lock.
- **Latency:** effectively immediate — the pause is visible to the *next* signal's Gate 4.5.
- **Files:** Option A + `pnl_ledger.py`. Sequence the phases so the money-file change lands last, after the state store and gate are tested.

### Option C — Option B + exchange-native stop-loss (defense-in-depth, later)
Adds Q2's `_order_params()` trigger params with a per-venue capability table (`ccxt_adapter.py:554`, gated like `_leverage_params` at `ccxt_adapter.py:578-604`).
- **Risk:** highest — modifies live order submission across venues, per-venue CCXT param drift, orphaned-trigger cleanup.
- **Latency:** venue-side (fastest possible for single-position excursion), but doesn't cover cross-trade streaks.
- **Verdict:** a separate milestone after B is empirically validated on a real close.

**Recommendation: Ship Option A, then extend to B once the state/gate/UI seam is proven.** Rationale: the internal breaker with the cron backstop already fully satisfies the requirement (pause on single-trade % or N-streak, manual-only resume, closes never blocked) with zero money-hot-path risk. The synchronous hook (B) is a latency optimization on an already-correct system, and belongs in its own PR touching the money file. C is genuinely separate defense-in-depth.

---

## Q8. Edge cases

1. **Partial fills → multiple ledger rows for one logical trade.** `closed-trades.jsonl` is per-`ord_id` (`_composite_key = (exchange, inst_id, ord_id, mode)`, `pnl_ledger.py:151-152`), and `reconcile_from_order_history` only converts **terminal** rows (`pnl_ledger.py:708-799`; codebase rule "skip non-terminal venue rows in reconcile"). A logical trade that fills in fragments yields several small rows. **Consequence:** a single-trade % trip could either miss a big loss split across rows, or a streak could over-count fragments as separate "losing trades." **Mitigation:** for the single-trade check, evaluate at the *position-delta close* granularity that `reconcile_from_order_history` already produces (one logical close), not raw sub-fills; for streaks, prefer the **cumulative-drawdown-over-window** trip (via `aggregate_strategy_pnl`) which is fragmentation-invariant, and treat raw-row streaks as a secondary signal.

2. **Capital changes (deposit/withdraw) between loss calc and trigger.** `budget_usd` is the operator-set seed in the strategy JSON (`strategy/records.py:80-88`), recomputed per signal (`readiness.py:108-109`) — it does **not** move with real deposits. So the % denominator is stable and won't be moved by an account transfer mid-eval. But note the known landmine (`service.py:14-17`): a % expressed against `budget_usd × leverage` is tautological. Keep the trip **absolute-anchored**: `X% * budget_usd` where `budget_usd` is the independent seed, mirroring how the notional ceiling uses `min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD_ENV)` rather than a derived product.

3. **Demo vs live.** `mode` is a first-class ledger field (`pnl_ledger.py:685`) and `read_closed_trades` scopes by it. **Scope the breaker per (strategy_id, mode)** and store `mode` in the `strategy_circuit` entry (Q3). A demo bleed must **not** halt live opens and vice-versa — they're different accounts. Gate 4.5 reads `readiness.execution_mode` (`service.py:159`) and matches it against the halt's `mode`. Demo trips are still surfaced (useful signal) but operators may choose to auto-configure demo halts as advisory-only.

4. **Interaction with the pre-trade notional ceiling (Gate 4).** Independent and complementary: Gate 4 (`service.py:204`) is a *forward* fat-finger cap on a single order's size; the breaker is a *backward* loss cap across realized outcomes. Order them Gate 4 → Gate 4.5 → Gate 5 → Gate 6. All are `close_only`-bypassed except Gate 4 (which is naturally close-safe because closes carry `planned_notional_usd=None/0`, `service.py:32-34`) and Gate 1 (the one gate that is *not* close-safe — hence why the breaker must be its own state and not `submit_orders=False`).

5. **`strategy_id=None` reconciled rows** (from Q4): excluded from the calc — the breaker's completeness is bounded by submit-map attribution coverage (`pnl_strategy_map`). Flag this so it's not mistaken for "everything is attributed."

6. **Streak reset semantics on replay/restart:** because streak is derived from the ledger (not a persisted counter), a systemd restart (`Restart=always`, per project rules) recomputes it identically — no lost/duplicated streak state. The **paused flag itself** is durable in `control-state.json`, so a restart cannot silently un-pause a halted strategy (the exact property we want).

---

### Suggested test cases (per `dev-rules.md` #5)
- Gate 4.5 **blocks an open** but **passes a `close_only`** when `strategy_circuit[sid].state == halted_manual_review` (the core invariant — assert against the *production* `service.execute` path, not a re-implementation, per the codebase anti-pattern).
- `load_control_state` **round-trips** `strategy_circuit` (merge-filter re-attach at `control_state.py:88` — a test that writes the key, reloads, and asserts it survives; this is the gotcha most likely to silently regress).
- Streak computation: 3 losses → trip at N=3; a win at position 2 resets; break-even (`net_realized_pnl == 0`) treated as non-loss.
- Single-trade %: loss ≥ X%·budget trips; loss < X% does not; fragmented fills across rows evaluated at logical-close granularity.
- Demo trip does not halt live opens (mode scoping).
- Manual resume clears the flag; no code path auto-clears (assert the hook/cron only ever call `trip_`, never `clear_`).
- End-to-end: `reconcile_from_order_history(hermx_rows)` → append → hook trips → next `service.execute` open blocked (the round-trip test the codebase memory says is the one most often missing).

---

**Note on scope (per `dev-rules.md` #3/#4):** this feature touches >3 files and modifies shared money files (`pnl_ledger.py`, `service.py`, `control_state.py`). Recommend breaking into the three sequenced PRs in Option A/B and getting explicit confirmation before the `pnl_ledger.py` hook (Option B) lands. This is a design proposal only — nothing was written or edited when this doc was produced.
