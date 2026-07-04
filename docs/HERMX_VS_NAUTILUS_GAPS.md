# HermX vs NautilusTrader — Gap Analysis & Hardening Opportunities (2026-07-03)

> Compares HermX's execution flow (`docs/HERMX_TRADING_EXECUTION_FLOW.md`, ground-truth as of
> 2026-07-03) against NautilusTrader's (`develop`-branch) execution/risk/accounting internals.
> **Analysis only — no code changes.** Every HermX claim is cited `file:line`; every NT claim
> names the concrete class/enum/method so it can be verified upstream.
>
> Recent fixes deliberately excluded from the gap list (already closed): C1 submit-time
> attribution map, H1 leg threading, H3 TOCTOU `flock` on the ledger append, read-side dedup.

---

## 1. Executive Summary

HermX and NautilusTrader solve different problems: NT is a single-threaded, event-sourced,
backtest-live-parity engine (Cython + Rust) that owns the full order/position/account model
in-process; HermX is a lean webhook→CCXT execution layer that treats the **exchange as the source
of truth** and keeps a durable JSONL audit trail. Most of NT's machinery (MessageBus/Actor
threading, HEDGING OMS, modify/cancel FSM, Redis cache, simulated matching engine) is **irrelevant
to HermX's market-order-only, one-strategy-one-asset, human-in-the-loop design** and adopting it
would be a rewrite with no payoff. The genuine, additive gaps cluster in **one place: HermX has no
pre-trade risk gate** — no notional cap, no order-rate throttle, no balance check, and only a
binary kill switch where NT has a three-state `TradingState`. The second cluster is **P&L fidelity**:
HermX depends on a per-venue `pnl` field (absent for Bybit and others) instead of deriving realized
P&L from its own fills the way NT's `calculate_pnls()` does, and it dedups closes on `ord_id` rather
than `trade_id`, blurring partial fills.

---

## 2. Section-by-Section Gap Table

| # | Flow section | HermX today | NT pattern | Relevant gap? |
|---|---|---|---|---|
| 1 | Inbound alert path | HMAC-SHA256 + shared secret + fsync'd WAL (`webhook_auth.py:127-181`, `raw-webhooks.jsonl`) | Typed immutable events on `MessageBus`; **no crypto signing** | **No — HermX is stronger.** NT trusts an in-process bus; HermX hardens an untrusted HTTP boundary. |
| 2 | Execution readiness / pre-trade | Gate stack: arming, mode, kill switch, symbol pause, idempotency (`service.py:100-183`) | `RiskEngine`: `_check_orders_risk()` notional caps, balance sufficiency, qty bounds, `max_order_submit_rate` throttle, `TradingState` ACTIVE/REDUCING/HALTED | **YES — the biggest gap.** No size cap, no rate limit, no balance check, binary kill only. |
| 3 | Order execution | `ExecutionService` → `CcxtExecutor` (`service.py:224-228`) | `ExecutionEngine` → `ExecutionClient` w/ `_routing_map` per venue | No — architecturally equivalent at HermX scale. |
| 4 | Order state machine | `PLANNED→SUBMITTED→{FILLED,REJECTED,UNKNOWN}`, validated transitions (`:1535-1548`) | `OrderStatus` FSM w/ `InvalidStateTrigger`; ACCEPTED, PARTIALLY_FILLED, PENDING_*, CANCELED, EXPIRED | Partial — no ACCEPTED vs SUBMITTED split, no first-class PARTIALLY_FILLED. Modify/cancel states N/A. |
| 5 | Position management | Live `fetch_positions` each read; **no local model** (`ccxt_adapter.py:340-361`) | `Cache` position tracking derived from fills; `PositionOpened/Changed/Closed` | **YES — no expected-position model → can't detect drift** without a live read succeeding. |
| 6 | Reconciliation | Startup + post-submit + periodic UNKNOWN resolver, order-history polling (`:2269-2650`) | Report-driven: `OrderStatusReport`/`FillReport`/`ExecutionMassStatus`, inferred fills, in-flight watchdog | Partial — HermX polls; no **inferred-fill** recovery for fills that age out of the 100-row window. |
| 7 | P&L / accounting | `pnl_ledger` append, gross from venue `pnl`, per-strategy (`pnl_ledger.py:43-92`) | `Account`/`Portfolio`, `calculate_pnls()` from fills, base-ccy conversion, netting | **YES — venue-`pnl` dependency (Bybit→None); dedup on `ord_id` not `trade_id`.** |
| 8 | Error handling / fault tolerance | try/except + fail-closed state writes + UNKNOWN (`service.py:185-195,269-278`) | Crash-only, event replay, dead-letter, inferred recovery | No — HermX's fail-closed posture is arguably safer; replay gap folds into #6. |
| 9 | Idempotency / dedup | `cl_ord_id` (`mxc…`) journal key + signal dedup (`:718-720,767-822`) | `ClientOrderId`/`VenueOrderId`, `trade_id` fill dedup, `_check_overfill()` | Partial — no `trade_id` fill dedup (folds into #7); overfill guard N/A for market orders. |
| 10 | Observability | Structured JSONL ledgers + dashboard (`§12`) | Typed events + state snapshots + backtest-live parity | No — JSONL ledgers already ARE structured events. Replay harness is deferred (low feasibility). |

---

## 3. Top 10 Hardening Opportunities

Ranked by impact × feasibility **for HermX specifically**. Additive-only; no rewrites.

### 1. Pre-trade notional cap + order-rate throttle  ·  Effort: S  ·  Priority: 10
- **NT pattern:** `RiskEngine._check_orders_risk()` enforces `_max_notional_per_order` (per-instrument;
  `None` disables) and rejects before dispatch; `_order_submit_throttler` enforces a configured
  `max_order_submit_rate` (e.g. `"100/00:00:01"`), denying via `_deny_new_order()` → `OrderDenied`.
- **Our gap:** the readiness builder computes `planned_notional = budget_usd × leverage`
  (`:1450-1451`) and the service gate stack (`service.py:100-183`) has **no upper bound on notional
  and no submit-rate limit**. The only rate control is the HTTP-layer `120 req/60s` per-IP limit
  (`:3541-3545`), which is intake throttling, not per-strategy order throttling.
- **Risk if not fixed:** a fat-fingered `leverage`/`budget_usd` in a strategy file, or a TradingView
  alert storm that survives dedup (distinct `tv_time`s), submits arbitrarily large or arbitrarily
  many **real-money** orders. There is no circuit breaker between "config typo" and "live venue".
- **Adoption path:** add two gates to `ExecutionService.execute` after Gate 3, before the write-ahead
  journal: (a) `planned_notional_usd > HERMX_MAX_NOTIONAL_USD` → `_blocked("notional_cap", …)`;
  (b) a per-`(strategy_id, symbol)` sliding-window counter (reuse the intake rate-limit pattern) →
  `_blocked("submit_rate", …)`. Both read env caps, default generous, fail-closed on parse error.
- **Why top:** highest-consequence gap, smallest change, drops cleanly into the existing single-exit
  `_blocked` gate model.

### 2. Three-state `TradingState` (HALTED / REDUCING / ACTIVE)  ·  Effort: M  ·  Priority: 8
- **NT pattern:** `TradingState{ACTIVE, REDUCING, HALTED}` set via `set_trading_state()`;
  `_execution_gateway()` blocks exposure-increasing orders under `REDUCING` and all orders (except
  cancels) under `HALTED`.
- **Our gap:** HermX has a **binary** global kill switch (`HERMX_LIVE_TRADING`, `hermx_shared.py:67`)
  plus per-symbol pause (`control-state.symbol_pauses`). There is no "close-only / reduce-only"
  global mode — the operator can only run fully or halt fully.
- **Risk if not fixed:** during a market shock the operator's only tools are "keep opening new risk"
  or "hard stop" (which, being binary, also disarms the ability to *open* but the close-bypass keeps
  closes alive). There's no first-class "stop opening, keep managing" posture, so de-risking is done
  by flipping every strategy to `pause` one at a time.
- **Adoption path:** add `control-state.trading_state ∈ {active,reducing,halted}` (default `active`).
  Gate in `ExecutionService`: `halted` → block all non-`close_only`; `reducing` → block `OPEN_*`
  actions, allow closes (mirror the existing `close_only` bypass logic at `service.py:130-158`). No
  new file — reuse `control-state.json` + `save_control_state`.
- **Note:** this generalizes the kill switch HermX already has rather than replacing it; keep
  `HERMX_LIVE_TRADING` as the real-money master.

### 3. Derive realized P&L from fills, not the venue `pnl` field  ·  Effort: M  ·  Priority: 8
- **NT pattern:** `Account.calculate_pnls()` computes realized P&L **from the account's own fills**
  (entry vs exit price × qty, minus commission) — venue-independent, works identically for every
  adapter.
- **Our gap:** `_normalized_realized_pnl()` (`ccxt_adapter.py:43-63`) returns the venue's `pnl`
  field only for `okx`/`hyperliquid`/`binance`; **Bybit and every other venue → `None`**, and
  `_build_entry` then persists `pnl_gross=None` (`pnl_ledger.py:463-473`, honest-unknown). Those
  closes carry no realized figure at all.
- **Risk if not fixed:** any expansion beyond OKX/HL/Binance ships a P&L dashboard that silently
  shows `None`/zero realized for real closed trades — the operator can't see whether a live Bybit
  strategy is winning or losing. Documented as an open gap (flow doc §13).
- **Adoption path:** HermX already tracks avg entry via the position snapshot and each close fill's
  `avg_px`/`filled_qty`. Add a fallback in `_build_entry`: when the venue `pnl` is `None`, compute
  `realized = (exit_px − entry_px) × signed_qty × contract_size` from the running position state the
  reconciler already maintains (`reconcile_from_order_history` tracks signed position per instrument,
  `pnl_ledger.py:548-576`). Keep the venue field as authoritative when present; the computed value is
  the fallback, flagged with a `pnl_source: "derived"` field for auditability.
- **Why high:** directly unblocks multi-venue P&L, which is the stated direction, and removes a
  correctness cliff.

### 4. Balance-sufficiency pre-trade check  ·  Effort: S/M  ·  Priority: 7
- **NT pattern:** `_check_orders_risk_for_account()` verifies free balance covers a buy (and
  available long qty covers a reduce-only sell) **before** the order reaches the client, converting
  quote-quantity worst-case.
- **Our gap:** HermX submits blind. `CcxtExecutor.execute` sizes the order from notional
  (`_amount_from_readiness`, `ccxt_adapter.py:477-491`) and calls `create_order` with no balance
  check; an underfunded account is discovered only when the venue rejects.
- **Risk if not fixed:** an underfunded/misconfigured account produces a venue rejection that maps to
  `submit_failed` → **`REJECTED`** or, on a timeout, **`UNKNOWN`** (`service.py:257-262`), which then
  pauses the symbol and burns the UNKNOWN-resolver budget — noisy churn for a knowable pre-condition.
- **Adoption path:** the adapter already reads balance in `health()`/`get_balance()`. In `execute`,
  before the `create_order` loop, fetch free balance for the settlement currency and skip/soft-block
  an `OPEN_*` leg whose margin requirement (`notional / leverage`) exceeds it, returning a
  `mode="insufficient_balance"` that the service maps to `not_submitted` (a control outcome, not
  UNKNOWN). Guard it behind a flag so a flaky balance read fails **open** (submit anyway) — a
  balance-fetch failure must never block an otherwise-valid trade.

### 5. Local expected-position model + drift alert  ·  Effort: M/L  ·  Priority: 7
- **NT pattern:** the `Cache` holds authoritative positions built from fills (`_open_position`,
  `_update_position`, `PositionChanged`); reconciliation compares venue `PositionStatusReport`s
  against the local model and surfaces divergence.
- **Our gap:** HermX is **stateless with respect to position** — every read is a live `fetch_positions`
  (`ccxt_adapter.py:340-361`). There is no "what HermX believes the position should be" to compare
  against "what the exchange reports", so drift (a fill HermX missed, or a manual exchange-side trade)
  is invisible unless a live read happens to run and a human eyeballs it.
- **Risk if not fixed:** a missed fill or an out-of-band manual close leaves HermX's *implicit* model
  (open order journal) and the venue disagreeing, with no automated detector. The 100-row history
  ageout (`dashboard.py:935-957`) partially covers the P&L side but not position drift.
- **Adoption path:** derive an expected signed position per `(strategy, inst_id)` from terminal
  order-journal fills (HermX already has FILLED transitions and fill sizes). On each dashboard
  live-snapshot, compare expected vs `fetch_positions`; a mismatch beyond `QTY_EPS` emits a
  `POSITION_DRIFT` reconcile alert. Observe-only, consistent with §6's "never auto-trade" posture —
  it's a detector, not a corrector.

### 6. First-class PARTIALLY_FILLED (and ACCEPTED) order states  ·  Effort: M  ·  Priority: 6
- **NT pattern:** `OrderStatus` distinguishes `SUBMITTED` (sent) → `ACCEPTED` (venue ack'd) →
  `PARTIALLY_FILLED` → `FILLED`, with `OrderFilled` events carrying `last_qty`; the FSM rejects
  illegal transitions via `InvalidStateTrigger`.
- **Our gap:** HermX's journal collapses these: an adapter ACK is `SUBMITTED`, and any fill (partial
  or full) that reconciliation confirms becomes `FILLED` (`map_order_outcome`, `:1986-2026`; a
  `partially_filled`/`0<accFill<ordered` row maps straight to `FILLED`). There is no ACCEPTED (venue
  acknowledged but unfilled) and no persistent PARTIALLY_FILLED.
- **Risk if not fixed:** a partially filled order that never completes is recorded as fully FILLED,
  so downstream position math and P&L treat a partial as complete. The flow doc flags this as "M5
  partial-fill fragility" (§13).
- **Adoption path:** add `PARTIALLY_FILLED` to `_ORDER_STATE_TRANSITIONS` as a non-terminal state
  (`SUBMITTED→PARTIALLY_FILLED→FILLED`) and record it when `0 < accFill < ordered`, carrying
  `filled_qty`. ACCEPTED is optional (lower value — HermX rarely sees the gap between submit and ack
  for market orders). Keep terminal-state semantics intact so the UNKNOWN resolver logic is unchanged.

### 7. `trade_id`-level fill dedup for the P&L ledger  ·  Effort: M  ·  Priority: 6
- **NT pattern:** duplicate fills rejected via `order.is_duplicate_fill_c(event)` keyed on `trade_id`;
  reconciliation additionally guards with a `_recent_fills_cache`. One order → many fills, each a
  distinct trade.
- **Our gap:** `append_closed_trades` dedups on composite `(exchange, inst_id, ord_id, mode)`
  (`pnl_ledger.py:117-118`) — **one row per order**, not per trade. An order that fills across
  multiple trades (partial fills at different prices) collapses to a single ledger row with one
  `avg_px`.
- **Risk if not fixed:** for venues that report fills as separate trades, realized P&L and fee
  accounting are approximated by a single averaged row; combined with #6, a multi-fill order can be
  mis-costed. Low frequency at HermX's market-order scale but real for larger sizes.
- **Adoption path:** where the venue exposes fills (`fetch_my_trades`), key the ledger dedup on
  `trade_id` when present, falling back to the current `ord_id` composite when absent. Additive — the
  read-side dedup already tolerates mixed keys (`read_closed_trades`, `:271-285`).

### 8. Inferred-fill reconciliation for aged-out closes  ·  Effort: M/L  ·  Priority: 6
- **NT pattern:** `create_inferred_order_filled_event()` synthesizes a fill when a venue report shows
  filled qty the local order never recorded; `_reconcile_missing_fills()` closes the gap so P&L is
  never silently lost.
- **Our gap:** HermX folds P&L only from the 100-row `fetch_closed_orders` window
  (`dashboard.py:960-965`). The window-ageout **detector** exists (`history_window_ageout` alert,
  `:935-957`) but there is no **recovery** — once a close scrolls past 100 rows before a reconcile
  pass captured it, that realized P&L is never ledgered.
- **Risk if not fixed:** a burst of closes (or a long dashboard downtime) between reconcile passes
  can drop closed-trade rows permanently; the ledger under-reports realized P&L with only an alert as
  evidence.
- **Adoption path:** when `POSITION_DRIFT` (#5) or `history_window_ageout` fires, fetch the deeper
  archive (`get_order_history_archive`, larger `limit`, or `fetch_my_trades` since the ledger
  high-water `max_recorded_closed_at`, `pnl_ledger.py:288-303`) and fold any missing HermX-attributed
  closes. Bounded and observe-only. This is the recovery half of the detector HermX already ships.

### 9. Active in-flight QueryOrder burst on new submits  ·  Effort: S  ·  Priority: 5
- **NT pattern:** `_check_inflight_orders()` runs every `inflight_check_interval_ms`, finds orders
  stuck in SUBMITTED/PENDING past `inflight_check_threshold_ms`, issues `QueryOrder`, and after
  `inflight_check_max_retries` resolves them.
- **Our gap:** with `HERMX_RECONCILE_ENABLED` OFF (the default, `:1974-1983`), a freshly SUBMITTED
  order isn't actively polled until the periodic UNKNOWN resolver's next tick, and it only escalates
  after `UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS = 900s`. The gap between submit and first status
  query can be long.
- **Risk if not fixed:** a submit that silently failed at the venue sits as SUBMITTED for up to ~15
  minutes before the symbol pauses, delaying operator awareness.
- **Adoption path:** a short, bounded burst of `reconcile_order_with_backoff` (which already exists,
  `:2091-2135`) for the just-submitted `cl_ord_id`, gated by a *separate* lightweight flag so it can
  be enabled without turning on full post-submit reconcile. **Deliberately keep HermX's "never
  auto-REJECT from absence" discipline** — this only *queries faster*, it does not synthesize
  terminal state (which the memory-noted NAUTILUS_DESIGN_DOC review correctly rejected).

### 10. Order-journal replay harness (execution-logic parity)  ·  Effort: L  ·  Priority: 4
- **NT pattern:** the same single-threaded kernel (MessageBus + RiskEngine + ExecutionEngine + Cache)
  runs in backtest and live; deterministic event ordering means a replay reproduces live state
  exactly.
- **Our gap:** HermX can replay the **raw-webhooks WAL** into the intake path, but it cannot replay
  the **order-journal** through the execution/reconcile logic to reproduce and verify final state —
  there's no offline "run the FSM over recorded events and assert the same terminal states" harness.
- **Risk if not fixed:** regressions in transition logic are caught only by unit tests, not by
  replaying real recorded histories; a subtle state-machine bug could pass tests yet mis-handle a
  production sequence.
- **Adoption path:** a read-only tool that feeds recorded `order-journal.jsonl` events through
  `order_state_can_transition` + the outcome-mapping functions and asserts the persisted terminal
  states are reproducible. **Lowest priority** — large effort, and HermX doesn't backtest (TradingView
  owns strategy logic), so the parity payoff NT gets does not fully transfer. Listed for completeness,
  not urgency.

---

## 4. What HermX Does BETTER Than NautilusTrader (for this use case)

Honest assessment — NT is a general-purpose institutional engine; HermX is a lean, auditable,
one-operator execution layer. Several HermX choices are genuinely better *here*:

- **Hardened untrusted boundary.** NT has **no cryptographic message signing** — integrity comes from
  typed in-process objects on a trusted bus. HermX's entry point is a public HTTP webhook, and it
  fails closed on a blank secret (`webhook_auth.py:170-172`), does constant-time secret compare, and
  supports HMAC-SHA256 + a replay window (`:127-156`). For an internet-facing receiver this is the
  right posture and NT simply doesn't address it.

- **"UNKNOWN, never REJECTED" discipline beats NT's inferred-state synthesis.** NT will
  `create_inferred_order_filled_event()` and synthesize rejections/missing orders to keep its model
  whole — and this has bitten it (GitHub #3176: persistent-cache + position reconciliation created
  duplicate orders on IBKR restart). HermX refuses to fabricate a terminal state from absence
  (`map_order_outcome`, `:1986-2026`): ambiguity pauses the symbol and alerts a human. For real money
  at small scale, **pause-and-escalate is safer than auto-synthesize.**

- **Durable, greppable, append-only audit trail.** NT's source of truth is an in-memory `Cache`
  (optionally Redis-backed) reconstructed from event history. HermX's is a set of fsync'd JSONL
  ledgers where **every** state transition is one inspectable line — WAL fsync'd *before* the queue
  put, ledger append under `flock`, checkpoint+seal rotation. An operator can `grep` a `cl_ord_id`
  across `raw-webhooks`, `signals`, `order-journal`, and `closed-trades` and see the whole life of a
  trade with no tooling. The closed-trade ledger is **never pruned** (lifetime financial record).

- **Fail-closed on the money path, specifically.** ENOSPC on an order-journal write **re-raises and
  blocks the submit** (`service.py:185-195`); a missing adapter returns `not_submitted`; the adapter
  independently refuses a live venue without `HERMX_LIVE_TRADING` (defense-in-depth,
  `ccxt_adapter.py:307`). NT is fail-fast on *data* errors but has no equivalent money-path-specific
  fail-closed contract.

- **Operator can always flatten.** A close bypasses the kill switch *and* symbol pause
  (`service.py:130-158`) — the operator can flatten a live position during the exact emergency when
  the switch is off. NT's `HALTED` denies everything except cancels; for market-order de-risking
  HermX's explicit close-bypass is more directly useful.

- **No rewrite tax / auditable in an afternoon.** NT is a Cython + Rust hybrid with a MessageBus/Actor
  threading model, `NautilusKernel` orchestration, and an OMS abstraction. HermX is single-language
  readable Python. At one-strategy-one-asset scale, HermX's entire execution path can be audited by
  one person reading five files — a property worth more than NT's generality here.

### NT patterns that explicitly DON'T apply (not padding the list)

- **Modify/Cancel FSM states** (`PENDING_UPDATE`, `PENDING_CANCEL`, `OrderModifyRejected`): HermX
  submits **market orders only** and never modifies or cancels. The entire PENDING_* subtree is dead
  weight here.
- **HEDGING OMS / multiple positions per instrument** (`_determine_hedging_position_id`,
  `_flip_position`): HermX is netting-only, one strategy per asset. NT's OMS abstraction solves a
  problem HermX doesn't have.
- **MessageBus / Actor / thread-local bus**: HermX is a bounded queue + single worker thread. Adopting
  a message bus is a rewrite whose determinism payoff (backtest parity) HermX can't use.
- **Simulated matching engine / backtest kernel**: strategy logic lives in TradingView, not HermX.
  There is nothing to backtest inside HermX.
- **Redis-backed cache / event store**: HermX's JSONL-on-disk is a deliberate simplicity win, not a
  deficiency, at this scale.

---

*End of analysis.*
