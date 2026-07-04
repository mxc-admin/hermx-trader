# HermX vs Nautilus Trader — Execution Architecture Comparison (2026-07-04)

> Component-by-component comparison of HermX's order-execution architecture against
> Nautilus Trader's execution engine (`nautechsystems/nautilus_trader`, docs + master-branch
> source as researched 2026-07-04: `nautilustrader.io/docs/latest/concepts/{orders,execution,
> cache,portfolio,live,architecture,overview,data,message_bus}`, `nautilus_trader/execution/
> engine.pyx`, `risk/engine.pyx`, `execution/client.pyx`, `common/factories.pyx`,
> `crates/model/src/{identifiers,types,orders}`). Every HermX claim is cited `file:line`
> against the current tree (post `02a7fc8f` money-path fixes).
>
> Related prior analyses: `docs/NAUTILUS_GAP_ANALYSIS.md` and `docs/HERMX_VS_NAUTILUS_GAPS.md`
> (both 2026-07-03 — several of their top gaps have since been closed and are re-scored here),
> `docs/NAUTILUS_DESIGN_DOC.md`, `docs/HERMX_TRADING_EXECUTION_FLOW.md`.

## 1. Purpose & scope

Nautilus Trader is a general-purpose backtesting **and** live algorithmic-trading framework:
a deterministic event-driven kernel (Rust core, Python control plane) whose strategies, data
engine, risk engine, and execution engine run identically in research and production. HermX is
a webhook-driven multi-venue crypto **execution and risk layer**: TradingView owns strategy
logic; HermX authenticates alerts, gates them, submits market orders through CCXT, and keeps a
durable audit/P&L trail. The comparison below is therefore **architectural, not feature-parity**
— several Nautilus subsystems (backtest kernel, tick-level order emulation, execution
algorithms) solve problems HermX has deliberately outsourced to TradingView or the venue, and a
coverage score of 0 on those is not a defect. Importance is judged against HermX's actual use
case, not Nautilus's.

## 2. Scoring rubric

**Coverage (0–5)** — how much of the Nautilus approach's *function* HermX implements
(mechanism may differ; JSONL vs Redis is a mechanism difference, not a coverage gap):

| Score | Meaning |
|---|---|
| 0 | Absent — no equivalent functionality |
| 1 | Trace — a fragment exists but doesn't cover the core function |
| 2 | Partial — core function exists but with material holes vs the Nautilus approach |
| 3 | Substantial — the function is covered for HermX's current operating envelope; known edges uncovered |
| 4 | Near parity — functionally equivalent for this use case; minor or deliberate divergences |
| 5 | Full parity — matches (or, for this use case, exceeds) the Nautilus approach |

**Importance** — judged against HermX specifically (webhook-driven, market-order-only,
~1 strategy/symbol, a handful of CCXT venues, operator-in-the-loop, real money):

- **Critical** — a hole here can lose or misaccount real money, or double-submit.
- **High** — materially degrades correctness, observability, or operator control.
- **Medium** — correctness headroom; matters as volume/venues grow.
- **Low** — legitimately out of scope for HermX's design (even if Nautilus scores it core).

## 3. Component comparison

| # | Component | Nautilus approach (brief) | HermX current state (brief) | Coverage | Importance | Notes |
|---|---|---|---|---|---|---|
| 1 | Order construction (OrderFactory) | `OrderFactory` attached to every strategy; 10+ order types; `ClientOrderId` = `O-{datetime}-{trader}-{strategy}-{count}` (monotonic counter, cache-checked); `init_id`, `ts_init` stamped | `stable_client_order_id` = `mxc` + `sha256(identity\|role)[:32]` — **deterministic per signal** (`webhook_receiver.py:731-733`); distinct close/open leg ids (`:1510-1512`); `execution_intent` record with policy/actions/notional (`:1553-1564`) | 3 | Medium | HermX's hash-of-identity id is idempotent by construction — *stronger* than Nautilus's counter for webhook replay/restart. Missing only the order-type breadth (market/limit only), which is by design. |
| 2 | ExecutionEngine (routing, message bus) | Central hub on a MessageBus; `ExecEngine.execute`/`.process` endpoints; routes SubmitOrder → Emulator/ExecAlgorithm/RiskEngine → ExecutionClient; publishes typed order/position/fill events per topic | `ExecutionService.execute()` — synchronous single entry, hook-injected (~30 hooks, `webhook_receiver.py:2710-2749`), gate chain → write-ahead journal → `ExecutorFactory.create().execute()` exactly once (`service.py:115-463`); intake `PROCESS_QUEUE` + worker threads with per-symbol ticket ordering (`webhook_receiver.py:3350-3385`) | 3 | Medium | Architecturally equivalent at HermX scale. A message bus would add indirection with no reliability gain at 1 worker / ~1 strategy-per-symbol (already rejected in `NAUTILUS_GAP_ANALYSIS.md` §B). |
| 3 | ExecutionClient (venue adapter) | Abstract per-venue client: `submit/modify/cancel_order`, `query_order` + concrete `generate_order_*` event helpers; live subclass must implement typed report coroutines (`OrderStatusReport`/`FillReport`/`PositionStatusReport`) | `BaseExecutor` ABC: `execute()` required; observe-only `get_order/get_open_orders/get_order_history_*/get_positions/get_balance` (`executors/base.py:85-147`); normalized envelopes (`empty_fill_summary`, `normalized_result`); one `CcxtExecutor` covers 8 venues, per-venue credential shapes (`ccxt_adapter.py:330-429`) | 3 | High | The gap is the **typed report seam**: reconcile/ledger code parses raw CCXT dicts inline per venue (`reduceOnly` string quirks, per-venue `pnl` field names) — the recurring source of the "Bybit realized P&L is None" bug class. |
| 4 | Cache (in-memory state) | In-memory DB of orders/positions/accounts/instruments; bidirectional `ClientOrderId↔VenueOrderId` index; optional Redis/Postgres persistence; automatic purging | Order journal + verified checkpoint + in-memory index (`_order_index`, `webhook_receiver.py:1709-1713`); `load_open_orders`/`latest_order_record` O(1) idempotency authority (`:1963-1997`); **no position/account/instrument store** — positions read live via `fetch_positions` per query (`ccxt_adapter.py:459+`) | 2 | Medium | Order-side caching is solid (event-sourced, crash-safe). Missing half is positions/accounts — scored under Portfolio (#6), where the consequence lives. JSONL-instead-of-Redis is deliberate and correct at this volume. |
| 5 | RiskEngine (pre-trade checks + TradingState) | `_check_price/_check_quantity` precision & bounds; instrument `min/max_notional` + operator `max_notional_per_order`; free-balance/margin sufficiency; submit/modify rate throttlers; `TradingState` ACTIVE/REDUCING/HALTED; typed `OrderDenied` | `_check_pretrade_risk`: **independent-absolute notional ceiling** `min(capital.max_notional_usd, HERMX_MAX_NOTIONAL_USD)` (`service.py:23-50`, Gate 4 `:198-215`); `trading_state` `active`/`reducing` gate — reducing blocks non-close (`:221-223`, `webhook_receiver.py:1389-1420`); plus kill switch, symbol pause, idempotency gates (`docs/EXECUTION_GATES.md`) | 3 | Critical | Big improvement since the 2026-07-03 analyses (ceiling + trading_state now shipped). `halted` deliberately omitted — a state that blocks closes violates the never-block-a-close invariant; kill switch covers hard stop. Remaining holes: **no balance-sufficiency check, no per-strategy submit-rate throttle, no instrument min/max-notional pre-validation** (sub-min orders silently floor to qty 0 at `ccxt_adapter.py:626-628`). |
| 6 | Portfolio (positions, PnL, account) | Central position/PnL hub: realized/unrealized/total PnL, `net_exposure`, `equity()` anchored to venue `AccountState` events; `Position` is a persisted aggregate folded from fills (flip-split, per-fill realized PnL) | `pnl_ledger.py`: append-only `closed-trades.jsonl`, `aggregate_strategy_pnl` (`:415-457`); `equity_now_usd = budget + closed_net + open_upl` — **synthetic, budget-anchored**; position recomputed per dashboard build by replaying ~100 order-history rows (`reconcile_from_order_history:612-703`); realized PnL taken from the venue `pnl` field — `None` for Bybit and most venues (gross-first, `ORDER_PNL_IS_NET` all False `:43-49`) | 2 | High | Three holes: (a) no persisted position aggregate — window-recompute is fragile and 100-row bounded; (b) equity never cross-checked against real venue balance (primitive exists, see #9); (c) realized PnL not derivable from own fills when the venue omits `pnl`. Honest-`None` posture (never fabricate zero) is correct but leaves multi-venue P&L blind. |
| 7 | Order state machine + events | 14 `OrderStatus` values incl. ACCEPTED, PARTIALLY_FILLED, PENDING_UPDATE/CANCEL, TRIGGERED, EMULATED/RELEASED; explicit `_ORDER_STATE_TABLE`, `InvalidStateTrigger`; orders reconstructible from event replay (`from_events`) | 5 states `PLANNED→SUBMITTED→{FILLED,REJECTED,UNKNOWN}` with strict fail-closed transition table (`_ORDER_STATE_TRANSITIONS`, `webhook_receiver.py:1598-1611`); `record_order_state` raises on illegal transition, appends durable event row (`:1899-1932`); write-ahead PLANNED/SUBMITTED fsync'd before venue call (`service.py:185-195` region) | 3 | High | Same pattern (explicit table + fail-closed guard + event-sourced journal), smaller state set. PENDING_*/TRIGGERED are N/A (no modify/cancel/trigger path). The real miss is **PARTIALLY_FILLED**: a partial that never completes collapses to FILLED, corrupting position/P&L math. `UNKNOWN` (no Nautilus equivalent) is a genuine HermX addition — see §5. |
| 8 | Identifiers & idempotency | Typed ids (ClientOrderId, VenueOrderId, TradeId, PositionId, AccountId, StrategyId…); deterministic generation + cache duplicate-check; bidirectional venue-id mapping; EXTERNAL strategy for unclaimed orders | `mxc<sha256>` idempotent cl_ord_id (journal dedupe key, Gate `duplicate_cl_ord_id` `service.py:240-248`); `operator_close_{sym}_{sid}_{YYYYMMDD}` parser (`pnl_ledger.py:198-247`); submit-time maps `cl-ord-strategy-map.jsonl` (`pnl_strategy_map.py:74-107`, written at SUBMITTED `service.py:268-284`) + HL `cloid-map.jsonl` (`pnl_cloid_map.py`, `ccxt_adapter.py:533-550`); `received_at` µs-ISO join key; `signal_id` dedupe ledger | 4 | Critical | Strong. Submit-time attribution maps solve the id→strategy inversion Nautilus gets for free from its Cache — the right architecture given non-invertible hashed ids. Missing only **`trade_id`-level fill identity** (ledger dedupes on `(exchange,inst_id,ord_id,mode)` — one row per order, not per fill). |
| 9 | Reconciliation (startup/reconnect) | Live-only mass status via typed reports; missing orders → external orders + **inferred fills** synthesized; `generate_missing_orders` default on; max-lookback advised; `trade_id` pre-filtering | Three observe-only paths — startup (always), post-submit (default OFF), periodic UNKNOWN resolver ~30 s (`webhook_receiver.py:2670-2681`) — all reconcile **HermX's own journal** against the order's own venue+mode (`_effective_execution_config`, `:2254-2306`); intake WAL replay at startup (`replay_intake_webhooks:3388-3480`); external fills opt-in `HERMX_LEDGER_EXTERNAL_FILLS` (`pnl_ledger.py:694-698`); position/balance drift detectors **built + tested but unscheduled** (`reconcile_position_drift` `webhook_receiver.py:2226-2250`, `check_balance_drift` `ccxt_adapter.py:256-311` — no production caller) | 3 | Critical | HermX deliberately refuses Nautilus's inferred-state synthesis (absence → UNKNOWN + pause + page a human, never auto-REJECT/auto-fill) — safer at this scale; Nautilus's own #3176 duplicate-order incident validates the caution. The two real holes: **drift detectors are inert** (the "inert monitor" anti-pattern), and closes that age past the 100-row history window are detected (`history_window_ageout`) but never **recovered**. |
| 10 | In-flight order monitoring | `inflight_check_interval/threshold/retries` — unconfirmed submits resolve to REJECTED after retries; continuous open-order checks with per-order query rate limits | PLANNED-orphan timeout 300 s (`_resolve_planned_orphan`), UNKNOWN resolver 30 s tick, 900 s lifecycle backstop → alert + symbol pause, never auto-close (`webhook_receiver.py:2415+, 2502+, 2553+`) | 4 | High | Same concern, deliberately different resolution: HermX pauses-and-pages instead of synthesizing REJECTED (an order that "vanished" may have filled). Correct for operator-in-loop money. Gap folded into #9: no fast post-submit query burst when post-submit reconcile is OFF (up to ~15 min to detect a silently-dead submit). |
| 11 | OrderEmulator (conditional orders) | Client-side emulation of STOP/IF_TOUCHED/TRAILING via per-instrument `MatchingCore` on live ticks; `OrderEmulated`/`OrderReleased` events | None. Market/limit only (`ccxt_adapter.py:752-753`); no stop/trigger params anywhere; alert `action` enum is `buy\|sell\|close` (`schemas/tradingview-alert.schema.json:40`) | 0 | Low | By design: TradingView owns triggers/strategy logic; HermX holds no market-data feed to emulate against. Not a gap to close. |
| 12 | ExecAlgorithm (TWAP etc.) | Primary orders routed to algorithms pre-risk; TWAP slices with `spawn_*` child orders (`{id}-E{seq}`), leaves_qty accounting | None (grep-clean). Single-shot sizing; only "split" is the reversal close+open two-leg (`ccxt_adapter.py:704-935`) | 0 | Low | Order sizes at HermX scale don't need slicing. Revisit only if notionals grow to market-impact territory. |
| 13 | Value types (Price/Quantity/Money) | Fixed-point scaled integers (i64/i128, 9/16 decimals) with instrument-conformant precision; RiskEngine denies precision violations | `Decimal` in the sizing path with explicit quantums, ROUND_HALF_UP (`src/webhook/money.py:18-42`); adapter floors qty to venue step via `Decimal` ROUND_DOWN (`_decimal_floor`, `ccxt_adapter.py:79-86`); floats elsewhere (ledger arithmetic, PnL) | 2 | Medium | The money-critical spots (sizing, step-floor) already use Decimal; float drift in P&L aggregation is display-grade risk, not order-grade. Doesn't use ccxt's `amount_to_precision` — the hand-rolled floor is deliberate (conservative ROUND_DOWN). |
| 14 | Live/backtest parity | One `NautilusKernel`; identical engines in backtest/sandbox/live; only clients + clock differ; "no code changes" research→production | None — HermX is live-only; demo mode is the venue's real sandbox account via `set_sandbox_mode` (`ccxt_adapter.py:404-414`), i.e. a *deployment* mode, not a simulation engine | 0 | Low | Nothing to backtest inside HermX — strategy logic lives in TradingView. Demo-as-real-sandbox arguably gives *better* fidelity than simulated matching for testing the execution layer itself. |
| 15 | Persistence / event sourcing / replay | ParquetDataCatalog for market data; Redis/Postgres cache DB; MessageBus external streaming; orders reconstructible from event streams | Fsync'd `raw-webhooks.jsonl` WAL (before queue put) + startup replay; checkpointed, segment-rotated order journal (`webhook_receiver.py:1750-1768`); never-pruned `closed-trades.jsonl`; atomic `control-state.json`; size-based ledger rotation for WAL/signals only (`:888-903`) | 3 | Medium | Order journal *is* event-sourced and replayable in principle; what's missing is only a replay **harness** (feed recorded journal through the FSM and assert terminal states) — ranked lowest-priority in prior analysis. Market-data catalog is N/A (no market data held). |

## 4. Top gaps (low coverage × Critical/High importance)

Ranked. One minimal recommendation each — no over-engineering. The 2026-07-03 analyses' two
top gaps (notional ceiling, trading_state) are **closed** and excluded.

### Gap 1 — Pre-trade balance-sufficiency and instrument-bounds checks (component #5, Critical)
Nautilus's RiskEngine denies `NOTIONAL_EXCEEDS_FREE_BALANCE` and instrument `min/max_notional`
violations before the client. HermX submits blind: an underfunded account is discovered as a
venue reject → REJECTED/UNKNOWN churn + symbol pause; a sub-minimum order silently floors to
qty 0 in the adapter (`ccxt_adapter.py:626-628`) with no operator-visible reason.
**Minimal fix:** in `CcxtExecutor.execute`, before `create_order`: (a) compare margin
requirement (`notional/leverage`) against free settlement-currency balance (fetch already
exists — `get_balance_summary`) and return `mode="insufficient_balance"` → `not_submitted`;
(b) surface the qty-0 sub-min case as an explicit `below_instrument_min` control outcome.
Fail **open** on a flaky balance read — a balance-fetch failure must never block a valid trade.

### Gap 2 — Wire the drift detectors into a scheduled loop (component #9, Critical)
`reconcile_position_drift` (journal vs venue positions) and `check_balance_drift` (synthetic
equity vs real venue balance) are implemented, tested (`tests/test_phase_b_robustness.py`), and
observe-only — but **nothing calls them in production**. Per the project's own rule, an inert
monitor is worse than no monitor: it reads as coverage that doesn't exist. This is Nautilus's
`generate_position_status_reports` / `Portfolio.equity()` reconciliation, already built, minus
the last wire.
**Minimal fix:** call both from the existing `unknown_resolver_loop` cadence (or a Hermes cron
gate) per active `(venue, mode)`; alerts already route through `emit_reconcile_alert`. No new
primitives needed.

### Gap 3 — Realized-PnL fallback derived from own fills (component #6, High)
`_normalized_realized_pnl` trusts the venue `pnl` field, which exists for OKX/Hyperliquid/
Binance and is `None` elsewhere (Bybit et al). Honest-`None` beats a fabricated zero, but any
venue expansion ships a P&L view that can't say whether a strategy is winning. Nautilus derives
realized PnL from its own fills (`calculate_pnls()`), venue-independently.
**Minimal fix:** in `_build_entry`, when venue `pnl` is `None`, compute
`(exit_px − entry_px) × signed_qty × contract_size` from the running position state
`reconcile_from_order_history` already maintains; tag `pnl_source:"derived"` and keep the venue
field authoritative when present.

### Gap 4 — Recovery for closes that age out of the 100-row history window (component #9, High)
The `history_window_ageout` **detector** exists, but once a close scrolls past the 100-row
`fetch_closed_orders` window before a reconcile pass captures it, that realized P&L is never
ledgered — permanent silent under-reporting with only an alert as evidence. Nautilus closes
this class with inferred fills; HermX doesn't need synthesis, just a deeper read.
**Minimal fix:** when the ageout alert fires, do a one-shot deeper fetch
(`get_order_history_archive` with a larger limit, or `fetch_my_trades` since the ledger
high-water `max_recorded_closed_at`) and fold the missing HermX-attributed closes. Bounded,
observe-only, reuses existing plumbing.

### Gap 5 — First-class `PARTIALLY_FILLED` order state (component #7, High)
A market order that partially fills and rests (thin book) is either skipped by the ledger's
non-terminal filter or eventually collapsed into `FILLED`, so downstream position/P&L math
treats a partial as complete. Nautilus models it as a real state with a self-loop.
**Minimal fix:** add `PARTIALLY_FILLED` to `_ORDER_STATE_TRANSITIONS` as non-terminal
(`SUBMITTED→PARTIALLY_FILLED→{FILLED,REJECTED,UNKNOWN}`), map `0 < accFill < ordered` there in
`map_order_outcome`, carry `filled_qty`. Skip `ACCEPTED` until limit orders matter.

**Honorable mention (Medium, not top-5):** a thin typed `FillReport`-style normalization seam at
the adapter boundary (component #3) — wrap the existing `_normalized_realized_pnl` /
`_normalize_reduce_only` helpers into one per-row struct so reconcile/ledger code stops parsing
raw CCXT dicts per venue. It's the structural fix for the bug class Gap 3 patches point-wise.

## 5. What HermX does that Nautilus does NOT emphasize

This comparison is not one-directional. Several HermX properties have no Nautilus counterpart
and are the *right* calls for an internet-facing, operator-in-the-loop money system:

- **Hardened untrusted boundary.** Nautilus trusts an in-process message bus; it has no message
  authentication story. HermX's entry point is a public webhook: fail-closed blank-secret 401,
  constant-time compare, optional HMAC-SHA256 + replay window, security-freshness deliberately
  independent of business idempotency (`security/webhook_auth.py`, `EXECUTION_GATES.md`).
- **Durability before acknowledgment.** The raw-webhook WAL is fsync'd *before* the queue put,
  making the WAL — not memory — the recovery source, with startup replay
  (`replay_intake_webhooks`). Nautilus persistence is optional and cache-shaped, not
  intake-WAL-shaped.
- **Idempotent-by-construction client order ids.** `sha256(signal identity)` means a replayed
  or double-delivered alert *cannot* mint a second order id. Nautilus's counter-based generator
  is deterministic per session but not content-derived; HermX's scheme is inherently
  restart-safe, which matters when restarts are routine (`Restart=always`).
- **Append-only, never-pruned money ledger with read-side dedup.** `closed-trades.jsonl` is a
  lifetime financial record: full read-modify-write under `fcntl.flock`, composite-key
  last-wins dedup *also* at read time (closing the TOCTOU window), malformed rows preserved
  rather than dropped. Nautilus's PnL lives in a purgeable in-memory/Redis cache.
- **Submit-time attribution maps.** `{cl_ord_id → strategy_id}` and `{mxc_id → HL cloid}`
  recorded at the moment both are known (`pnl_strategy_map.py`, `pnl_cloid_map.py`) — solving
  the hash-not-invertible attribution problem without a resident stateful engine.
- **UNKNOWN-never-REJECTED discipline.** Nautilus synthesizes inferred fills and resolves stuck
  in-flight orders to REJECTED after retries; that machinery has produced duplicate orders in
  the wild (upstream #3176). HermX refuses to fabricate terminal state from absence: ambiguity
  → UNKNOWN → symbol pause → page a human. At operator-in-loop scale this is strictly safer.
- **Never-block-a-close invariant.** A close bypasses the kill switch and symbol pause
  (`service.py` Gate 3/pause bypass), and `halted` was deliberately rejected in favor of
  `reducing` so emergency flatten works exactly when new risk is disabled. Nautilus's HALTED
  denies everything except cancels — less useful for market-order de-risking.
- **Log-and-continue on the observability/money path.** Fee-currency mismatches and `None`
  realized-PnL are warned and persisted with the anomaly recorded — never coerced to a
  fabricated zero, never dropped. Observability failures cannot block or corrupt the money path.
- **Greppable audit trail.** One `cl_ord_id` can be traced across `raw-webhooks` → `signals` →
  `order-journal` → `closed-trades` with no tooling. Nautilus state reconstruction requires the
  cache database or replaying the event stream through its runtime.
