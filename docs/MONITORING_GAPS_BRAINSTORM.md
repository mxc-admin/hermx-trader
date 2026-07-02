# HermX Monitoring Gaps & Failure Modes — Brainstorm / Analysis

**Status:** RESEARCH + ANALYSIS (no implementation in this pass)
**Author:** research pass, 2026-07-02
**Companion docs:** `docs/HERMES_CRON_MONITOR_DESIGN.md` (the 5 shipped monitor jobs),
`docs/MONITOR_DAEMON_SPEC.md` (superseded daemon spec — still the source of fingerprint/window logic).
**Audience:** whoever extends the monitoring surface next.

> This document maps every failure mode we could think of for the HermX execution layer to
> (a) what code catches it today, (b) the gap, (c) the live data source, (d) how a cron job /
> gate would catch it, and (e) what the operator alert should look like. Every "X exists" claim
> carries a `file:line` citation. Assumptions are called out explicitly.

---

## 0. TL;DR — the headline gaps

Ranked by danger (money-losing first). Detail in §4, mapping in §5, priority table in §6.

1. **Position drift is UNDETECTED on the live path (Scenario F).** The receiver no longer compares
   strategy-expected direction vs exchange-actual position. The old comparison that wrote
   `reconcile-alerts.jsonl` was **removed**; that file is a fossil. "Strategy thinks flat, exchange
   holds 4.61 long" fires zero alerts today. **Money-losing, silent.**
2. **A rejected order is logged but NOT alerted (Scenarios E, I).** A clean `submit_failed` →
   `REJECTED` (insufficient margin, bad price, bad creds) is a silent terminal journal write. No
   operator alert. The strategy is now un-hedged/un-entered and nobody is paged.
3. **Zero-intake / TradingView-stopped is UNDETECTED (Scenario D).** The watchdog only checks
   worker/resolver heartbeats and queue lag of *already-enqueued* items. A receiver that is up but
   receiving zero webhooks reports perfectly healthy. There is no "time since last webhook" metric
   anywhere. **The biggest observability hole for a money system.**
4. **No strategy-frequency / absence detection (Scenario A).** No gate, field, or code counts
   signals-per-strategy-per-window. A strategy that silently stops firing is invisible.
5. **No inline fill confirmation (Scenario B).** Place-and-forget by default; fill status is
   confirmed only asynchronously by the ~30s resolver, and only if it can reach OKX.
6. **Read-side exchange API failures are swallowed to `[]` (Scenario H).** An OKX outage during
   reconciliation is indistinguishable from "no orders / no positions."
7. **No "paused for N days" elapsed-time detection (Scenario G).** Timestamps exist
   (`set_at`, `paused_at`) but nothing computes elapsed time, and `symbol_pauses` is not even in
   the `/api` payload.

The through-line: **every existing monitor is a *presence* detector** (it wakes when a bad row
appears). The most dangerous gaps are all **absence / silence** conditions (a signal that didn't
arrive, a fill that didn't happen, an alert that was never emitted) — which the current machinery
structurally cannot fingerprint.

---

## 1. Corrections to the documented mental model (read this first)

The research turned up several places where `CLAUDE.md` / code docstrings are stale. These matter
because a monitor built on the wrong file sees nothing forever.

| Claim in docs | Reality (cited) |
|---|---|
| "FastAPI webhook receiver" (`CLAUDE.md`) | Stdlib `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`. `/webhook` is a string match, not a decorator. `webhook_receiver.py:22`, handler class `:3280`, route `:3376`. |
| `reconcile-alerts.jsonl` is a live reconcile sink | **Dead file.** Its `detail` shape (`local_direction`/`exchange_direction`/`exchange_pos`) has **no writer in current source** (`grep` finds only the doc comment `webhook_receiver.py:2148`). Fossil of removed position-drift code. |
| `operator-alerts.jsonl` / `seen-signals.jsonl` are live | **Legacy.** Consolidated: header comment `webhook_receiver.py:148-149`; `seen-signals` superseded by `signals.jsonl` as "the SINGLE dedup authority" `:275-279`. Live alert sink is `logs/alerts.jsonl`. |
| `executions.jsonl` / `execution-plan.jsonl` are live | **Retired.** `webhook_receiver.py:172-176`; asserted gone by `tests/test_characterization_hotfix_ledgers.py:79,96`. Execution outcomes now go to `pipeline.jsonl` stage=`execution`. |
| `main` reconciles OKX positions vs local, emits `RECONCILE_MISMATCH` on divergence (`webhook_receiver.py:3502-3503` docstring) | **Stale docstring.** `reconcile_startup` reconciles **open orders only**; `position_mismatches` is hard-coded empty (`:2148,:2153,:2185`). No live position comparison exists. |
| side/source gates at `:2829/:2831` (`.claude/rules/code-quality.md`) | Drifted to side gate `:2969`, source gate `:2971`. |

**Live data sources (the only files a monitor should trust):**

| File | Written by | One row per | Rotation |
|---|---|---|---|
| `logs/raw-webhooks.jsonl` | `record_raw_webhook("intake",…)` `:3401` (fsync'd, the WAL) | every intake arrival (`phase:"intake"`) | size-rotated, 5 sealed segments `:170-171,925` |
| `logs/pipeline.jsonl` | `record_pipeline_event(stage,…)` `:890` | each signal-lifecycle event | size-rotated, 5 sealed segments `:170-171,914` |
| `logs/signals.jsonl` | `check_and_mark_signal` `:802-809` | each *unique* signal (dedupe) | **NOT rotated — append-only unbounded** |
| `logs/alerts.jsonl` | `emit_operator_alert` `:2057` / `emit_reconcile_alert` `:2106` | each operator/reconcile alert | **NOT rotated — append-only unbounded** |
| `logs/order-journal.jsonl` | `record_order_state` `:1772-1782` | each order state transition | record-count checkpoint+seal `:1678,1703-1734` |
| `control-state.json` | dashboard `_save_control_state` `dashboard.py:213-231`; receiver `save_control_state` `:1163-1178` | snapshot (overwritten) | atomic replace |
| `latest.json` | `_atomic_json_dump(LATEST_FILE,…)` `:2939,3093,3118` | snapshot (overwritten) | atomic replace |

---

## 2. What the intake → execution pipeline actually does

Concise map so the scenarios below have shared vocabulary. All `file:line` in `webhook_receiver.py`
unless noted.

**Intake (`do_POST`, `:3371-3422`):** route match → body-size cap (`HERMX_MAX_BODY_BYTES`, 413) →
rate limit (120/60s, 429) → JSON parse (400) → stamp `intake_received_at = now_iso()` (µs-ISO,
`:3400`) → **WAL write to `raw-webhooks.jsonl` (fsync, before queue put, `:3401`)** → enqueue to
in-memory `PROCESS_QUEUE` (`:3404`; `queue.Full` → `QUEUE_SATURATION` + 503). Returns
`200 {"status":"queued"}` **before any validation** — validation is async in the worker.

**Validation / build_record (async, `:2943`):** `normalize()` (`:977`) computes deterministic
`signal_id` = sha256 of `strategy_id|symbol|action|timeframe|tv_time` (`:1003-1004`), falling back
`tv_time→now_iso()` if absent (`:998`, the non-determinism gotcha). Gate order: action/side conflict
(400 `:2951`) → `action=="close"` routes to `_build_close_record` (`:2964`, bypasses side gate) →
side gate (`side ∉ {buy,sell}` → 400 `:2969`) → source gate (`source != "tradingview"` → 202
`:2971`) → schema gate (**observe-only unless `enforce_alert_schema`** `:2984-3003`) →
`validate_strategy_alert` (unknown strategy → quarantine 202 `:3005`). Dedupe ledger `signals.jsonl`
written after dequeue (`check_and_mark_signal` `:756,802-809`).

**Order state machine (local):** `PLANNED, SUBMITTED, FILLED, REJECTED, UNKNOWN` (`:195-203`).
Terminal = `{FILLED, REJECTED}`. Legal transitions `:1453-1460`; illegal → `ValueError`
(`:1765-1767`). Write-ahead: `PLANNED` before send (`service.py:186`), `SUBMITTED` before
`create_order` (`service.py:192`). An adapter ACK stays **SUBMITTED, not FILLED**
(`service.py:221-228`) — becomes FILLED only if `mode=="filled"` / `fill_summary.status=="filled"`,
normally via reconciliation.

**Venue → local mapping (`map_order_outcome` `:1894-1934`, `_state_from_ccxt`
`ccxt_adapter.py:401-416`):** the money-safety rule maps **any absence/ambiguity → UNKNOWN, never
REJECTED** (`:1906-1911`). **Only `canceled` + zero fill → REJECTED** (`ccxt_adapter.py:412`,
fixture `order_canceled_zero_fill.json`). Partial fill → booked FILLED with a `partial=True` flag.

**Reconcile / resolver (all OBSERVE-ONLY — they journal + alert, never submit/cancel/auto-close):**
- Startup reconcile `reconcile_startup` (`:2140`, called `:3506`) — open orders only.
- Periodic resolver `unknown_resolver_loop` (`:2473`, thread `:3528`, **default ON**
  `HERMX_UNKNOWN_RESOLVER_ENABLED` `:2206`), ticks every **30s** (`:255`).
  `resolve_unknown_orders_once` (`:2313`): candidates in `{PLANNED,SUBMITTED,UNKNOWN}`; age from
  `origin_ts`; **stuck past 900s** (`UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS` `:256`) → pause symbol
  (`:2369`) + `UNKNOWN_RESOLVER_TIMEOUT` (error `:2385`) + `RECONCILE_MISMATCH` (`:2373`); else
  `reconcile_order_with_backoff` (≤5 attempts, ~20s budget `:1999-2043`).
- PLANNED-orphan backstop `_resolve_planned_orphan` (`:2226`): PLANNED older than **300s**
  (`:262`) never sent → `PLANNED→REJECTED` + `PLANNED_ORDER_ABANDONED` (`:2298`).
- Post-submit inline reconcile is separately gated **OFF by default** (`HERMX_RECONCILE_ENABLED`
  `:1882-1891`).

**Alert stream (`alerts.jsonl`):** `kind:"operator"` `{ts,kind,alert,severity,detail}` (`:2049`)
and `kind:"reconcile"` `{ts,kind,alert,detail}` (`:2104`, which **also emits a paired operator
row** `:2109` — every reconcile alert appears twice). Alert names + severities:
`WATCHDOG_DEGRADED`(error)/`WATCHDOG_RECOVERED`(info) `:631,636`; `RECONCILE_MISMATCH`(warning)
`:2373,2457,2187`; `UNKNOWN_RESOLVER_TIMEOUT`(error) `:2385`; `PLANNED_ORDER_ABANDONED`(warning),
`PLANNED_ORDER_ON_VENUE`(error), `PLANNED_RESOLVER_ERROR`/`UNKNOWN_RESOLVER_ERROR`(error)
`:2298,2273,2251,2402`; `AUTH_FAILURE`(error) `:2082`; `QUEUE_SATURATION`(error) `:2093,3407`;
`ALERT_SCHEMA_ENFORCEMENT_UNAVAILABLE`(error) `:1122`. Severity vocabulary =
`{info,warning,error,critical}`.

**Liveness watchdog (`liveness_watchdog_loop` `:607`):** checks only stale **worker** heartbeats,
stale **resolver** heartbeat, and **queue lag of already-enqueued items** (`:616-620`). With zero
intake the queue is empty (`_queue_oldest_age_seconds`→0.0 `:594`) → reports healthy.

---

## 3. What the shipped monitors already cover

Five cron jobs (`docs/HERMES_CRON_MONITOR_DESIGN.md` §6) + three pre-check gate scripts in
`deploy/hermes-scripts/`. All read HermX state read-only via `skills/hermx-ops/lib/hermx_ops.py`
(loopback `/api` + `/health`, tolerant file reads, **UNKNOWN-never-flat** `hermx_ops.py:173-177`).

| Monitor | Cadence | Reads | Catches |
|---|---|---|---|
| `hermx-health-check` (`--no-agent`) | 5m | dashboard `/health` + receiver `/health` reachability, arm/kill-switch (`hermx-health-watch.py:30-51`) | **process down**, kill-switch/disarm. NOT "signals arriving". |
| `hermx-reconcile` (gate+LLM) | 5m | `alerts.jsonl` rows `kind∈{reconcile,state,operator}` sev≥warning within 1800s + `/api` open-orders in `UNKNOWN` (`hermx-reconcile-gate.py:50-90`) | stuck orders, `RECONCILE_MISMATCH`, `UNKNOWN_RESOLVER_TIMEOUT`, watchdog. |
| `hermx-risk-watch` (gate+LLM) | 15m | `control-state.json:risk_index_gate_enabled` (fail-open if absent) then MXC `risk_state` (`hermx-risk-gate.py:28-60`) | transition into `{elevated,high,risk_off}`. |
| `hermx-daily` (LLM) | daily 08:00Z | status + positions + signal-memory | informational digest. |
| `hermx-weekly` (LLM) | Mon 09:00Z | status + positions + signal-memory | informational digest. |

**Shared gate lib (`hermx_gate_lib.py`):** suppression windows `{health:900, reconcile:1800,
risk:3600}` (`:33`), severity rank `info<warning<error<critical` (`:30`), `is_fresh` window +
escalation (`:106-119`), atomic self-healing sidecar (`:74-100`), `wakeAgent` JSON contract
(`:147-155`). Tests: `tests/test_monitor_cron_gates.py` (all presence-driven).

**Structural limitation:** all three gates are **event-presence** detectors. Fingerprint templates
(`MONITOR_DAEMON_SPEC.md:372-383`) all key on a row appearing. **There is no `frequency:*` /
`silent:*` / `absence:*` template** — you cannot fingerprint a row that *didn't* appear with today's
machinery. Closing the absence gaps requires a new gate *shape*, not just a new fingerprint.

---

## 4. Scenario-by-scenario analysis

Each scenario answers: **(1)** what catches it now, **(2)** the gap, **(3)** data source + exact
fields, **(4)** cron approach (new gate / new cron / extend existing), **(5)** proposed alert.

### Scenario A — Strategy frequency anomaly (fires 3–4/week, now 0 for 2 weeks)

1. **Catches now:** Nothing. No field, gate, test, or dashboard panel counts signals-per-strategy.
   The strategy JSON (`strategies/*.json`) declares `strategy_id`, `timeframe`, `instrument`,
   `capital`, `leverage` — **no `expected_frequency`/`signals_per_week`/`cadence`** field exists.
2. **Gap:** Absence detection is missing end-to-end — no per-strategy count-per-window anywhere in
   `src/`, no baseline to compare against, and not even in the `MONITOR_DAEMON_SPEC.md:946-954`
   candidate list.
3. **Data source:** `logs/raw-webhooks.jsonl` — filter `phase=="intake"`, group by
   `payload.strategy_id`, bucket by `received_at` (µs-ISO) into weeks (counts every arrival). Or
   distinct-signal cadence from `logs/signals.jsonl`, group by `dedupe_key.split("|")[0]` (=
   `strategy_id`; note signals.jsonl has **no bare `strategy_id`**), timestamp `first_seen_epoch`.
   *Avoid* `pipeline.jsonl` for counting — one logical signal produces ~5 rows across stages
   (`strategy_match`+`dedup_reject`+`decision`+`execution`); if used, filter to a single stage
   (`strategy_match`).
4. **Approach:** **New pre-check gate** `hermx-frequency-gate.py` reusing `hermx_gate_lib`
   (`evaluate`/`run_gate`/sidecar). New fingerprint `frequency:silent:{strategy_id}`. Because a 4h
   strategy is *normally* quiet for days, use a **per-strategy learned baseline** (trailing observed
   rate), not a fixed threshold. Expected inter-signal interval must be **derived** (from `timeframe`
   + observed history) or **added as a new strategy-file field**. Cadence: 1–6h.
5. **Alert:** severity `warning`; dedup window ~1 day (a silent strategy is a slow condition);
   "Strategy `btcusdt_duo_base_dev_2h` has fired 0 signals in 14d vs baseline ~3.5/wk — is TV
   sending? is it paused? is the market quiet?"

> **Assumption/caveat:** today there is <4 days of history and only `btcusdt_duo_base_dev_2h` has
> been exercised (70/76 pipeline rows). Baselines are **currently un-trainable** for ETH/SOL/XRP.
> A 3–4/week baseline needs ≥3–4 weeks of history before the detector is meaningful.

### Scenario B — Trade execution confirmation (did it fill?)

1. **Catches now:** The adapter reads `filled`/`average` from the *synchronous* `create_order`
   return only (`ccxt_adapter.py:653-656`) — **no `fetch_order` after placing**. An ACK is booked
   **SUBMITTED, not FILLED** (`service.py:221-228`). Async confirmation exists via the resolver
   (default ON, `:2206,2473`) → `reconcile_order_with_backoff` → `get_order` → writes
   `SUBMITTED→FILLED` (`:2409-2426`).
2. **Gap:** **No inline fill poll.** With defaults (`HERMX_RECONCILE_ENABLED` OFF `:1882-1891`), an
   order sits at SUBMITTED until the ~30s resolver picks it up — and only if
   `_reconciliation_executor()` is available (else silent return `:2331`). Nothing alerts on
   "SUBMITTED but never confirmed" until the **900s** lifecycle backstop (`:2356-2395`).
3. **Data source:** `logs/order-journal.jsonl` — flag any `cl_ord_id` whose latest `state ∈
   {SUBMITTED,UNKNOWN}` with `origin_ts` age > threshold (mirrors `load_open_orders`
   `:1808-1832`). Corroborate with `pipeline.jsonl` stage=`execution` (`okx_execution.mode`).
4. **Approach:** **Extend `hermx-reconcile-gate.py`** — it already reads `/api` open-orders in
   UNKNOWN state (`:67-90`); add a "SUBMITTED older than N min without terminal transition"
   condition keyed on `order-journal.jsonl`. No new cron needed.
5. **Alert:** severity `warning`→`error` past 900s; dedup on `cl_ord_id`; "Order `mxc…` for BTCUSDT
   SUBMITTED 18m ago, never confirmed filled — resolver may be blind to OKX."

### Scenario C — Order stuck open / never closed

1. **Catches now:** Resolver catches orders stuck in `PLANNED/SUBMITTED/UNKNOWN` past 900s → pause
   symbol + `UNKNOWN_RESOLVER_TIMEOUT`/`RECONCILE_MISMATCH` (`:2356-2395`). PLANNED-never-submitted
   caught at 300s → `PLANNED_ORDER_ABANDONED` (`:2298`). `hermx-reconcile-gate.py` surfaces these
   from `alerts.jsonl`.
2. **Gap:** Tracks **order lifecycle state, not economic intent.** A limit order that is `live` on
   the venue but never fills lingers as `UNKNOWN` and only alerts on the 900s age budget. There is
   **no notion of "should have filled by now given price,"** and no way to distinguish
   "intentionally resting limit" from "should-have-filled market." All journal-driven — an
   out-of-band order isn't in `load_open_orders` and is invisible.
3. **Data source:** `logs/alerts.jsonl` (`alert ∈ {UNKNOWN_RESOLVER_TIMEOUT, RECONCILE_MISMATCH,
   PLANNED_ORDER_ABANDONED}`) × `logs/order-journal.jsonl` (non-terminal `state`, `origin_ts` age).
4. **Approach:** **Mostly covered by `hermx-reconcile`.** To distinguish "resting limit vs
   should-have-filled" would need order-type in the journal `intent` (not present today) — that's a
   **producer change**, out of scope for a monitor. Leave as-is; document the limitation.
5. **Alert:** existing `UNKNOWN_RESOLVER_TIMEOUT` path already delivers via the reconcile gate.

### Scenario D — Webhook down / TradingView stopped firing (zero-intake)

1. **Catches now:** `hermx-health-check` catches the **receiver process being down** (`:37-42`).
   But if TV stops sending while the process stays up, `/health` returns `ok:true` and the watchdog
   is healthy (empty queue, fresh heartbeats). **Nothing fires.**
2. **Gap:** No "time since last webhook received" metric anywhere; `record_raw_webhook("intake",…)`
   (`:3401`) resets no liveness clock. A healthy-but-starved receiver is invisible. **Biggest
   observability hole for a money system.**
3. **Data source:** `max(received_at)` over `phase=="intake"` rows in `logs/raw-webhooks.jsonl`
   (the earliest, most-direct signal; `pipeline.jsonl` also stops growing but is downstream).
4. **Approach:** **New gate** `hermx-intake-gate.py` (or a second condition in the frequency gate,
   §A). Emit a **global** `frequency:zero_intake:global` fingerprint on "no intake row in the last N
   hours" — a much shorter window than per-strategy, because global silence is higher-severity and
   faster-actionable (when TV dies, *all* strategies drop to 0 at once, so global fires long before
   any single slow 4h baseline). Consider also the **OS-cron backstop** (`HERMES_CRON_MONITOR_DESIGN`
   §4.4) so a gateway-down blind spot doesn't mask a receiver-down + TV-down coincidence.
5. **Alert:** severity `error`; dedup window ~1–2h; "No TradingView webhooks received in 3h20m
   (last: `2026-07-02T14:10Z`). Receiver is up — check TV alert config / network path upstream."

### Scenario E — Order rejected by exchange

1. **Catches now:** Rejection is *detected and logged*: per-leg exception → `{status:"rejected",
   error:…}` (`ccxt_adapter.py:556-562,621-627`) → `submit_failed` (`:664`) → `REJECTED` journal
   transition (`service.py:237-238,262-269`) → `pipeline.jsonl` stage=`execution`
   (`executed_orders[].error`). Live sample: `order-journal.jsonl` seq 2 `state:"REJECTED"`,
   `pipeline` row `error:"okx requires \"apiKey\" credential"`.
2. **Gap:** **Logged but NOT alerted.** No `emit_operator_alert`/`emit_reconcile_alert` on a clean
   rejection — those fire only for `submit_partial` (`service.py:284-296`) or reconcile divergence
   (`:340-350`). The reason string (insufficient margin, invalid price, bad creds) is buried in the
   journal; nobody is paged. The strategy is now un-entered/un-hedged.
3. **Data source:** `logs/order-journal.jsonl` rows `state=="REJECTED"` (esp. `prev_state=="SUBMITTED"`);
   `logs/pipeline.jsonl` stage=`execution` where `okx_execution.mode=="submit_failed"` /
   `payload.executed_orders[].status=="rejected"` with `.error`.
4. **Approach:** **New pre-check gate** `hermx-rejection-gate.py` reusing `hermx_gate_lib`, OR
   **extend `hermx-reconcile-gate.py`** to also tail `order-journal.jsonl` for fresh REJECTED rows
   (it currently only reads `alerts.jsonl` + `/api` open orders). Fingerprint
   `execution:rejected:{cl_ord_id}`. **Cheaper alternative worth flagging: add an
   `emit_operator_alert` on the REJECTED path at the producer** (`service.py:262-269`) so it lands in
   `alerts.jsonl` and the *existing* reconcile gate catches it with zero new gate code — but that is
   a money-path producer change (dev-rules: shared-lib change needs confirmation).
5. **Alert:** severity `error`; dedup on `cl_ord_id`; "Order `mxc…` BTCUSDT REJECTED by OKX:
   `insufficient margin` — no position opened. Manual check required."

### Scenario F — Position drift / unexpected flat

1. **Catches now:** **Nothing on the live path.** `reconcile_startup` runs once at boot and
   reconciles **orders only**; `position_mismatches` is hard-coded empty (`:2148,2153,2185`). The
   resolver keys off journal order states, not positions (`:2335-2339`), and never calls
   `get_positions` on the real path. The position-vs-venue comparison that once wrote
   `reconcile-alerts.jsonl` (`local_direction`/`exchange_direction`/`exchange_pos`) was **removed** —
   that file is a fossil with no current writer.
2. **Gap:** No periodic strategy-expected-vs-exchange-actual position comparison. "Strategy expects
   flat, exchange holds 4.61 long" — or the inverse — is undetected. **This is the most dangerous
   silent gap: a real, money-losing divergence with zero alerting.**
3. **Data source:** Join **strategy-expected direction** (latest `pipeline.jsonl` stage=
   `decision`/`execution` → `execution_intent.target_direction`, reconciled with the journal
   terminal state per `cl_ord_id`) against a **live `/api` `okx_live.positions[SYM].side`**
   (FLAT/LONG/SHORT, `dashboard.py:746-758`). `/api` already exposes live positions (≤5s cache,
   `dashboard.py:82,719-771`); the *join* is what no code performs.
4. **Approach:** **New pre-check gate** `hermx-drift-gate.py`. Reads `/api` `okx_live.positions`
   (via `hermx_ops`) + derives expected direction per symbol from the journal/pipeline, wakes on
   mismatch. Fingerprint `reconcile:position_drift:{symbol}:{expected}:{actual}`. Cadence 5–15m.
   Must honor UNKNOWN-never-flat: if the `okx_live` read is `degraded`, positions are `UNKNOWN`
   (`hermx_ops.py:173-177`) → **do not** alert "flat" (fail-open, no false drift).
5. **Alert:** severity `critical` (real money divergence); dedup on `(symbol,expected,actual)`;
   "DRIFT BTCUSDT: strategy expects FLAT, OKX holds LONG 4.61 (uPnL -$83). Investigate before next
   signal."

### Scenario G — Silent strategy pause (paused and forgotten)

1. **Catches now:** Nothing raises it. The pause *is* persisted with a timestamp —
   `symbol_pauses[SYM].paused_at` (`:1230`) or `strategy_overrides[id].set_at`
   (`dashboard.py:243`) — and enforced as a hard block, but no monitor computes elapsed time.
2. **Gap:** No "paused for N days" derivation anywhere. Worse: `symbol_pauses` is **not in the
   `/api` payload at all** (only `strategy_overrides` is, `dashboard.py:1259`), and
   `hermx_ops.list_strategies()` drops `paused_at`, keeping a bare boolean (`hermx_ops.py:310-319`).
   A forgotten *symbol* pause is invisible to every read surface.
3. **Data source:** `control-state.json` → `symbol_pauses[SYM].paused_at` and
   `strategy_overrides[id].set_at`; compare against `now`. (A monitor can read the file directly via
   `safe_json_load`; it should **not** rely on `/api` for `symbol_pauses` until that's surfaced.)
4. **Approach:** **New pre-check gate** `hermx-stale-pause-gate.py` (or a condition in the daily
   digest). Fingerprint `control:stale_pause:{strategy_or_symbol}`. Cadence: daily is enough.
   Also recommend a **producer fix**: include `symbol_pauses` (with `paused_at`) in
   `api_payload()` (`dashboard.py:1254-1274`) so it's observable.
5. **Alert:** severity `info`→`warning` after N days; dedup window ~1 day; "SOLUSDT paused 9 days
   (since 2026-06-23, reason: 'stuck order') — still intended?"

### Scenario H — Exchange API issues (OKX errors)

1. **Catches now:** Partially. Submit **timeouts/network** → `submit_timeout` → **UNKNOWN**
   (`ccxt_adapter.py:80-93,545-555`; `service.py:233`), bounded by the 45s client timeout (`:167`).
   The dashboard captures the error string: `okx_live.error` (`dashboard.py:727,732,768`),
   collapsed into `executor.{degraded,stale,error,age_seconds,generated_at}`
   (`dashboard.py:1108-1135`); `degraded` drives the banner and forces `positions=UNKNOWN` in
   `hermx_ops` (`:174`). `hermx-reconcile` and the health check see `degraded`.
2. **Gap:** **Read-side query failures are swallowed to `[]`** — `get_order`/`get_open_orders`/
   `get_order_history_raw`/`get_positions`/`get_balance` all `except Exception: return []`
   (`ccxt_adapter.py:732-736,743-744,788-789,822-823,845-846`); `health()` → `{ok:false,error}`
   (`:893`). An OKX outage during reconciliation is indistinguishable from "no orders/positions," and
   `map_order_outcome(None)` calls it UNKNOWN not_found (`:1913-1914`). Only the *current* read is
   represented — no consecutive-failure count, no "N min since last success," no persisted
   exchange-error history (it vanishes after the 5–10s cache), no circuit-breaker escalation.
3. **Data source:** `/api` → `executor.{degraded,age_seconds}` + `okx_live.{error,generated_at}`
   (all already in `hermx_ops.read_state()` `:169,196`). To detect *sustained* outage, persist
   successive `generated_at`/`error` samples (nothing does today) or write exchange-error rows to
   `alerts.jsonl`.
4. **Approach:** **Extend `hermx-reconcile-gate.py`** (or the health gate) with an
   `executor.degraded` condition + a sidecar-tracked consecutive-degraded counter for escalation.
   Fingerprint `health:executor_degraded`. Recommend a **producer fix**: emit an
   `emit_operator_alert("EXCHANGE_DEGRADED",…)` after K consecutive failed reads so it's durable in
   `alerts.jsonl`.
5. **Alert:** severity `warning`, escalate to `error` after sustained (e.g. >10m) degradation; dedup
   window 900s; "OKX executor degraded for 12m (last good read 2026-07-02T14:02Z) — reconcile is
   blind, fills unconfirmed."

### Scenario I — Wallet / margin issues (insufficient margin)

1. **Catches now:** **Nothing pre-trade.** Sizing uses only notional÷price÷contract-size
   (`ccxt_adapter.py:373-399`); the gate stack (`service.py:100-158`) has **no balance/margin gate**.
   `fetch_balance` is observe-only (`get_balance` `:825-846`, `health` `:848-894`). Insufficient
   margin is discovered **only reactively** when OKX rejects `create_order` → the silent REJECTED
   path (same as Scenario E).
2. **Gap:** (a) never caught before the order; (b) when the venue rejects it, not alerted — the
   reason ("insufficient margin") is buried in `order-journal.jsonl` `detail` / `pipeline.jsonl`
   `executed_orders[].error`. Also note `control-state.json:risk_limits.max_daily_loss_usd` (150.0)
   is **read by nothing** — no drawdown enforcement exists (`MONITOR_DAEMON_SPEC.md:946` lists
   `drawdown` as a *candidate*, unbuilt), and that key is silently **dropped** on the next
   receiver-side `save_control_state` (`webhook_receiver.py:1189` filters to default keys).
3. **Data source:** Preventive: `get_balance` (`ccxt_adapter.py:825-846`, fields `eq`/`avail`) or
   OKX `balance.json` fixture (`availBal`/`frozenBal`/`adjEq`) — neither gates a trade today.
   Post-hoc: REJECTED rows whose `error` matches margin phrases (folds into Scenario E's gate).
4. **Approach:** Detection folds into **Scenario E's rejection gate** (classify `error` text). A
   *preventive* margin gate is a **money-path producer change** (`service.py` gate stack), not a
   monitor — out of scope here; flag as a design recommendation. A **drawdown cron**
   (`MONITOR_DAEMON_SPEC.md:946`) computing daily realized+unrealized PnL from
   `okx_live.positions[*].{realized_pnl,upl}` vs `max_daily_loss_usd` is a natural new gate — but
   first the dropped-key bug must be fixed so `max_daily_loss_usd` survives.
5. **Alert:** severity `error`; "Order REJECTED: insufficient margin (avail $X < required $Y) —
   check wallet before next signal."

---

## 5. Master mapping — "if X goes wrong, which monitor catches it?"

| Failure mode | Caught today by | Live? | Gap / proposed monitor |
|---|---|---|---|
| Receiver process down | `hermx-health-check` (`:37-42`) | ✅ | — |
| Dashboard down | `hermx-health-check` (`:33-36`) | ✅ | — |
| Kill switch engaged / disarmed | `hermx-health-check` (`:44-51`) | ✅ | — |
| Queue saturation (enqueued backlog) | watchdog `QUEUE_SATURATION` (`:2093,3407`) → reconcile gate | ✅ | — |
| Worker / resolver heartbeat stale | watchdog `WATCHDOG_DEGRADED` (`:631`) → reconcile gate | ✅ | — |
| Order stuck SUBMITTED/UNKNOWN >900s | resolver `UNKNOWN_RESOLVER_TIMEOUT` (`:2385`) → reconcile gate | ✅ | economic "should-have-filled" not modeled (C) |
| PLANNED never submitted >300s | `PLANNED_ORDER_ABANDONED` (`:2298`) → reconcile gate | ✅ | — |
| MXC risk elevated | `hermx-risk-watch` (`:56-60`) | ✅ | gated on unimplemented `risk_index_gate_enabled` |
| **Strategy stopped firing** | — | ❌ | **new `hermx-frequency-gate` (A)** |
| **TV stopped / zero intake** | — | ❌ | **new `hermx-intake-gate` / global freq (D)** |
| **Order fill never confirmed inline** | resolver (async, ≥30s, if OKX reachable) | ⚠️ | extend reconcile gate on `order-journal` SUBMITTED-age (B) |
| **Order REJECTED by exchange** | logged to journal/pipeline only | ❌ alert | **new/extended rejection gate (E)**; or producer `emit_operator_alert` |
| **Position drift (expected≠actual)** | — (removed) | ❌ | **new `hermx-drift-gate` (F)** |
| **Strategy paused & forgotten** | — | ❌ | **new stale-pause gate (G)**; surface `symbol_pauses` in `/api` |
| **OKX read API sustained outage** | dashboard `executor.degraded` (current-read only) | ⚠️ | extend gate w/ consecutive-fail counter (H) |
| **Insufficient margin** | reactive REJECTED only, unalerted | ❌ | folds into rejection gate (E/I); preventive gate = producer change |
| **Daily drawdown breach** | — (`max_daily_loss_usd` read by nothing) | ❌ | **new drawdown cron** (`SPEC §10.1`); fix dropped-key bug first |
| **Partial fill (< intended size)** | booked FILLED + `partial=True` flag only | ⚠️ | no monitor surfaces "filled less than intended" |
| **Advisor repeatedly vetoing** | — | ❌ | candidate `advisor_veto` gate (`SPEC §10.1`) from `pipeline` stage=`advisor` |
| **Schema-error spike** | `ALERT_SCHEMA_ENFORCEMENT_UNAVAILABLE` only when enforcing | ⚠️ | candidate `schema_error_spike` gate (`SPEC §10.1`) |

Legend: ✅ covered · ⚠️ partial/async/current-read-only · ❌ not caught.

---

## 6. Priority ranking (money-losing → informational)

**P0 — money-losing, silent (fix first):**
- **F: Position drift undetected.** Real divergence, zero alert, was actively removed. New drift
  gate. *Highest priority.*
- **E/I: Rejected order unalerted.** Strategy silently fails to enter/hedge; margin failures hide
  here. Cheapest high-value fix (add `emit_operator_alert` on REJECTED, or a rejection gate).
- **D: Zero-intake / TV down.** The system can go completely blind and report healthy. New intake
  gate + OS-cron backstop.

**P1 — money-adjacent, delayed detection:**
- **B: Inline fill confirmation.** Depends on the resolver reaching OKX; extend reconcile gate.
- **H: Sustained OKX read outage.** Reconcile goes blind; add consecutive-fail escalation.
- **Drawdown (I-adjacent).** `max_daily_loss_usd` enforced by nothing; fix dropped-key bug + add
  drawdown cron.

**P2 — operational hygiene / informational:**
- **A: Strategy frequency anomaly.** Valuable but **un-trainable today** (<4 days data). Build the
  gate now, let baselines mature. Also the natural home for D's global condition.
- **G: Stale pause.** Low-severity nag; also surface `symbol_pauses` in `/api`.
- **C: resting-limit vs should-have-filled.** Needs order-type in the journal (producer change);
  document, defer.
- Partial-fill visibility, `advisor_veto`, `schema_error_spike` — candidate gates, low urgency.

---

## 7. New gate vs new cron vs extend existing — classification

| Gap | Verdict | Rationale |
|---|---|---|
| F position drift | **New pre-check gate** (`hermx-drift-gate.py`) + its own cron, or fold into reconcile job | needs a live-position join no existing gate does |
| E/I rejection | **Extend `hermx-reconcile-gate.py`** to tail `order-journal.jsonl` REJECTED; *or* producer `emit_operator_alert` (reuses existing gate) | rejection data already exists; cheapest path is a producer 1-liner |
| D zero-intake | **New gate** (`hermx-intake-gate.py`) or a 2nd condition in the frequency gate; **+ OS-cron backstop** | absence detection — new gate *shape* |
| B fill confirm | **Extend `hermx-reconcile-gate.py`** (SUBMITTED-age condition) | reconcile job already reads `/api` open orders |
| H exchange outage | **Extend reconcile/health gate** + consecutive-fail sidecar; producer `EXCHANGE_DEGRADED` alert for durability | current-read data exists; needs history |
| A frequency | **New gate** (`hermx-frequency-gate.py`) | absence detection; needs baseline state |
| G stale pause | **New gate** (`hermx-stale-pause-gate.py`) or daily-digest line; + `/api` surface fix | pure `control-state.json` timestamp math |
| Drawdown | **New cron** + producer fix (dropped `max_daily_loss_usd`) | computes from `okx_live.positions` PnL |

**Recurring theme:** the **absence** gaps (A, D, and to a degree F/B) need a new gate *shape* —
one that reasons about "expected row that didn't appear" — because a fingerprint only fires on
rows that *do* appear. That shape is: read a rolling window, compute a count/recency per key,
compare to a baseline/threshold, and emit a **synthetic** condition when the count is anomalously
**low**. Crucially this is a new gate *script* pattern, **not** a new `hermx_gate_lib` primitive:
the synthetic condition flows through the existing `run_gate`/`evaluate`/sidecar machinery
unchanged — the lib already dedupes/suppresses whatever conditions a gate hands it, whether they
came from a present row or from an absence computation. `hermx-intake-gate.py` (D) is the first
instance: it computes `max(received_at)` over the WAL and synthesizes one condition when that is
too old, reusing the lib as-is (the only lib touch is adding an `"intake"` suppression window).

---

## 8. Data-retention considerations

- **Frequency baselining (A/D) is retention-bound.** `pipeline.jsonl` and `raw-webhooks.jsonl` are
  **size-rotated** — sealed at 64 MiB (`HERMX_LEDGER_ROTATE_MAX_BYTES` `:170`), **only 5 sealed
  segments kept** (`HERMX_LEDGER_ROTATE_RETENTION` `:171`, prune `:840`). So history is bounded by
  *volume, not time* — no guaranteed calendar horizon. For a 3–4/week baseline over a 2-week gap
  you need ≥3–4 weeks retained. At current demo volumes that's fine, but **a frequency gate must
  read across sealed segments (`<stem>.<n>.jsonl`), not just the live file**, and should tolerate
  the oldest window being pruned.
- **`signals.jsonl` and `alerts.jsonl` are NOT rotated** — append-only unbounded today. `signals.jsonl`
  is therefore the **best long-horizon source for distinct-signal cadence** (one row per unique
  signal), at the cost of being de-duplicated (won't count re-sends). `alerts.jsonl` growing
  unbounded is itself a latent operational risk worth a follow-up.
- **`order-journal.jsonl`** uses record-count checkpoint+seal (`:1678,1703-1734`) with a checkpoint
  index the dashboard merges (`dashboard.py:1652-1715`) — gates reading it for stuck/rejected orders
  should use the same checkpoint semantics, not raw tail.
- **`latest.json` / `control-state.json` are snapshots (overwritten)** — point-in-time only; no
  history. Elapsed-pause math (G) must derive from the embedded `paused_at`/`set_at`, since there's
  no historical series.
- **Freshness axis:** count arrivals over `received_at`/`ts` (wall clock), but judge signal
  *freshness* on `tv_time` (bar time) per the CLAUDE.md invariant. Don't conflate — after an outage
  the server clock is current but the bar is stale.

---

## 9. Assumptions & open questions

- **Env defaults assumed at in-code values:** `HERMX_RECONCILE_ENABLED` OFF (`:1882-1891`),
  `HERMX_UNKNOWN_RESOLVER_ENABLED` ON (`:2206`), resolver 30s / stuck 900s / PLANNED 300s /
  replay-lookback 300s / tv-age 120s (`:255-262,3230,3258`). An operator `.env` can override any of
  these; a monitor should not hard-code them.
- **`risk_index_gate_enabled` does not exist in code** (grepped across dashboard + receiver). The
  shipped `hermx-risk-watch` gates on it and **fail-opens (never alerts) because it's always
  absent** — the risk monitor is effectively inert until this flag is implemented in
  `control-state.json` and written by the dashboard.
- **`max_daily_loss_usd` is read by nothing** and is silently dropped on the next receiver-side
  `save_control_state` (`:1189` filters to `default_control_state` keys `:1149-1160`, which omit
  `risk_limits`/`allowed_assets`/`allowed_policies`). Any drawdown monitor must fix this first.
- **Only one strategy has real history** (`btcusdt_duo_base_dev_2h`, 70/76 pipeline rows); ETH/SOL/XRP
  baselines are un-trainable today.
- **Partial fills** are booked as terminal FILLED with a `partial=True` flag riding in the reconcile
  detail only (`:1917-1928`); "filled less than intended size" has no monitor and no clean field.
- **No single 'trade completed / round-trip closed' event exists** — completion of a position must be
  reconstructed from paired `OPEN_*`/`CLOSE_*` executions + journal terminal states, joined on
  `received_at` (intake↔pipeline) and `cl_ord_id` (execution↔journal).
- **Producer vs monitor boundary:** several cheapest fixes (E/H `emit_operator_alert`, G `/api`
  surface, the dropped-key bug, a preventive margin gate) are **money-path producer changes**, not
  monitor additions. Per `dev-rules.md`, those need explicit confirmation and touch shared code —
  they are flagged as recommendations, not folded silently into the monitoring layer.

---

## Appendix — primary file references

- `src/webhook_receiver.py` — intake `:3371-3422`, `build_record` `:2943`, `normalize` `:977`,
  dedupe `:756,802-809`, state machine `:195-203,1453-1466`, order journal `:1772-1782`, reconcile
  `:1894-2043`, resolver `:2313-2470,2473`, PLANNED backstop `:2226-2298`, alert emitters
  `:2049,2104`, watchdog `:607`, rotation `:840,914,925`, retention `:170-171`.
- `src/execution/service.py` — gate stack `:100-158`, write-ahead `:186-192`, outcome mapping
  `:212-238`, tentative outcome `:256-269`, reconcile-divergence alert `:340-350`.
- `src/executors/ccxt_adapter.py` — `execute` `:534-666`, venue-state map `:401-416`, error
  handling `:80-93,545-562,614-627,683-693`, swallowed reads `:732-736,743-744,788-789,822-823,845-846`,
  sizing `:373-399`, `health` `:848-894`.
- `src/dashboard.py` — `/api` `:1230-1274`, `okx_live_snapshot` `:719-771`, `/health` `:1277-1308`,
  control-state `:195-263`, strategy-mode flags `:171-179`, effective mode `:1216-1227`,
  executor health `:1108-1135`.
- `skills/hermx-ops/lib/hermx_ops.py` — `read_state` `:119-178`, UNKNOWN-never-flat `:173-177`,
  bases `:30-32`, list_strategies `:310-319`.
- `deploy/hermes-scripts/` — `hermx_gate_lib.py` (windows `:33`, `is_fresh` `:106-119`),
  `hermx-reconcile-gate.py:50-90`, `hermx-risk-gate.py:28-60`, `hermx-health-watch.py:30-51`.
- `docs/MONITOR_DAEMON_SPEC.md` — fingerprints §4.2 (`:372-383`), windows §4.3 (`:398-405`),
  candidate sources §10.1 (`:946-954`).
- Tests — `tests/test_monitor_cron_gates.py`, `tests/test_action_close_intake.py`,
  `tests/test_unknown_resolver_controls.py`, `tests/test_characterization_hotfix_ledgers.py`,
  `tests/test_okx_query_interface.py`, `tests/fixtures/okx_query/*.json`.
