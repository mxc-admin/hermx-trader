# HermX Trading Execution Flow (end-to-end, as of 2026-07-03)

> Ground-truth reference for the FULL trading execution path as it exists in the codebase
> today. Every claim is cited to `file:line`. Where behavior could not be established from
> the code, the text says **TBD — verify in code** rather than guessing.
>
> Primary sources read for this document:
> `src/webhook_receiver.py` (3706 lines), `src/execution/service.py`, `src/executors/{base,factory,ccxt_adapter}.py`,
> `src/pnl_ledger.py`, `src/pnl_strategy_map.py`, `src/pnl_cloid_map.py`, `src/hermx_shared.py`,
> `src/dashboard.py`, `src/dashboard_core.py`, `src/security/{credentials,webhook_auth}.py`,
> `src/webhook/config.py`, `schemas/tradingview-alert.schema.json`, `schemas/strategy.schema.json`.

---

## 1. Overview & Invariants

HermX is a Python crypto-trading execution layer. A `ThreadingHTTPServer` webhook receiver
(`src/webhook_receiver.py`) accepts TradingView alerts, authenticates them (shared secret +
optional HMAC), normalizes and dedupes them, matches them to a sanctioned strategy file, and
routes them through a controlled `ExecutionService` (`src/execution/service.py`) that enforces a
gate stack (arming, mode, kill switch, symbol pause, idempotency) above a CCXT adapter
(`src/executors/ccxt_adapter.py`). Order lifecycle is tracked in a durable, checkpointed
write-ahead **order journal**; realized P&L is folded from exchange order history into an
append-only **closed-trade ledger** (`src/pnl_ledger.py`). A separate local Next.js/Python
dashboard (`src/dashboard.py`) renders positions and P&L and is the process that drives most of
the P&L-ledger reconcile writes.

### Core invariants (each cited where enforced)

- **Fail closed on auth.** Blank `HERMX_SECRET` → every webhook 401, nothing submitted
  (`webhook_receiver.py:96-97`, `security/webhook_auth.py:170-172`).
- **Fail closed on execution surface.** If `ExecutionService`/`ExecutorFactory` failed to import
  or no adapter is registered (optional `ccxt` missing), `_execute_authoritative` returns
  `not_submitted/execution_unavailable` — never submits (`webhook_receiver.py:2817-2840`,
  `executors/factory.py:81-82`).
- **Live real-money submission requires the global kill switch.** `HERMX_LIVE_TRADING` must be
  explicitly truthy (`hermx_shared.py:54-72`); enforced at the service gate
  (`execution/service.py:125-148`) AND defense-in-depth in the adapter (`ccxt_adapter.py:300-311`).
- **Ambiguity is UNKNOWN, never REJECTED.** Timeout/exception/partial → `UNKNOWN`; only a
  venue-confirmed canceled-with-zero-fill is `REJECTED` (`webhook_receiver.py:1986-2026`,
  `execution/service.py:253-262`).
- **Write-ahead order journal.** `PLANNED` then `SUBMITTED` are fsync'd BEFORE the adapter is
  called, so restart reconciliation has authoritative `cl_ord_id` keys after a crash
  (`execution/service.py:185-195`, `webhook_receiver.py:1836-1869`).
- **The closed-trade ledger is never pruned.** Lifetime record; append-only, deduped by
  composite key (`pnl_ledger.py:1-6, 410-444`).
- **A close reduces risk, so it bypasses the kill switch + symbol pause** (only those two gates)
  via `close_only` (`execution/service.py:130-158`, `ccxt_adapter.py:300-311`).

### Env flags that gate execution behavior

| Flag | Default | Effect | Read at |
|---|---|---|---|
| `HERMX_LIVE_TRADING` | unset → **disabled** | Global real-money kill switch. Live submit permitted only when `∈ {true,1,yes}`. | `hermx_shared.py:67-72` (service `service.py:140-142`; adapter `ccxt_adapter.py:307`) |
| `HERMX_RECONCILE_ENABLED` | unset → **False** (OFF) | Gates POST-SUBMIT inline reconcile. OFF ⇒ stdout drives tentative terminal state; ON ⇒ venue drives `SUBMITTED→terminal`. | `webhook_receiver.py:1974-1983` |
| `HERMX_EXEC_BACKEND` | `ccxt` | Adapter-class selector; overrides `execution.exchange` identically for submit + reconcile. | `webhook/config.py:10`, `execution/service.py:35-37` |
| `HERMX_CCXT_EXCHANGE` | `okx` | Venue fallback when adapter's `ccxt_exchange`/`exchange` resolve to `""`/`"ccxt"`. | `ccxt_adapter.py:212-216` |
| `HERMX_DATA_DIR` | `HERMX_ROOT` else repo root | Writable dir for mutable state (`control-state.json`, all P&L maps/ledger). | `webhook_receiver.py:137`, `pnl_ledger.py:95-101` |
| `HERMX_ROOT` | repo root (`Path(__file__).parents[1]`) | Repo root; `logs/`, strategy files, `engine-config.json`. | `webhook_receiver.py:131` |
| `HERMX_SECRET` | `""` → **fail closed** | Sole webhook `X-Webhook-Secret` **and** dashboard/`/api/close` token. | `webhook_receiver.py:97`, `dashboard.py:48` |
| `HERMX_REQUIRE_HMAC` | `false` | Require `X-Webhook-Timestamp`/`X-Webhook-Signature` HMAC + replay window. | `webhook_receiver.py:98` |
| `HERMX_WEBHOOK_HMAC_KEY` | `""` | HMAC-SHA256 key; with `REQUIRE_HMAC=true` and blank key ⇒ fail closed. | `webhook_receiver.py:99` |
| `HERMX_BIND_HOST` | `127.0.0.1` | HTTP bind interface; non-loopback + `REQUIRE_HMAC=false` triggers a security warning. | `webhook_receiver.py:93`, `:3627-3643` |
| `HERMX_RECEIVER_PORT` / `SHADOW_PORT` | `8891` | Receiver port. | `webhook_receiver.py:89` |
| `HERMX_REPLAY_ENABLED` | `true` | Startup replay of accepted-but-undequeued intake rows. | `webhook_receiver.py:117` |
| `HERMX_WATCHDOG_STALE_SECONDS` | `120` | Liveness watchdog stale threshold; `<=0` disables + pauses submission on degrade. | `webhook_receiver.py:125`, `:616-652` |
| `HERMX_ADVISOR_ENABLED` | config `advisor.enabled` else `false` | Enables the pre-execution LLM veto overseer (fail-open). | `webhook_receiver.py:303` |
| `HERMX_ALERT_WEBHOOK_URL` | unset | Optional external POST of operator alerts. | `webhook_receiver.py:2153` |
| `HERMX_DASHBOARD_PORT` / `CLEAN_DASHBOARD_PORT` | `8098` | Dashboard port. | `dashboard.py:40` |
| `HERMX_DASH_AUTH` | `true` | Dashboard auth toggle. | `dashboard.py:45` |
| `HERMX_DASH_TZ` | UTC | Dashboard display timezone (IANA name or signed hour offset). | `dashboard_core.py:145` |

**Not env-tunable (module constants, monkeypatchable in tests):** submit timeout `45.0s`
(`webhook_receiver.py:120`, `ccxt_adapter.py:31`), replay window `300s` (`:103`), max body `262144`
(`:104`), rate limit `120/60s` (`:105-106`), queue max `200` (`:107`), dedupe window `86400s`
(`:122`), `UNKNOWN_RESOLVER_INTERVAL_SECONDS=30.0` (`:260`, resolver ON when `>0`, `:2345-2353`),
`UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS=900.0` (`:261`), `PLANNED_ORDER_TIMEOUT_SECONDS=300.0`
(`:267`), worker pool size `1` (`:121`).

---

## 2. Strategy Configuration

### 2.1 Strategy JSON schema (`schemas/strategy.schema.json`)

`oneOf(strategy_v1, strategy_v2)`. **v2 is canonical**; v1 (OKX-coupled `okx_inst_id`) is
transitional/deprecated (schema `description`, `:5`). Representative v2 file
`strategies/btcusdt_duo_base_dev_2h.json`.

**v2 required** (`:110-121`): `schema_version(=2)`, `strategy_id` (`^[a-z0-9]+(?:_[a-z0-9]+)*$`),
`name`, `instrument`, `timeframe` (`enum 30m|1h|2h|3h|4h`), `indicator`, `capital`, `leverage`
(`>0`), `margin_mode` (`isolated|cross`), `execution_mode` (`demo|live`).

**`instrument`** (`:129-149`, required `exchange`, `inst_id`, `type`): `exchange`
(`^[a-z0-9_]+$`) **selects the venue** and drives routing; `inst_id` accepts OKX-native
(`BTC-USDT-SWAP`) or CCXT-unified (`BTC/USDT:USDT`); `type ∈ {swap,spot,future,futures,margin,option}`.

**`capital`**: `{budget_usd(>0), reinvest?}`. **`submit_orders`** (`:173-177`, default `true`):
`false` = Pause (validate + ledger + render, no order to either account). Credentials are
schema-forbidden in a strategy file (`no_inline_credentials`, `:11-31`) — they are resolved by the
adapter from namespaced env only.

### 2.2 Loading at startup

`load_strategy_files()` (`webhook_receiver.py:410-429`): globs `STRATEGIES_DIR/*.json`
(`STRATEGIES_DIR = ROOT/strategies`, `:293`), requires a non-empty `strategy_id`, runs
`normalize_strategy_record()` (`:385-407`, fills `instrument.exchange` default `okx` / `type`
default `swap`, bridges `submit_orders`→`okx_submit_orders`), canonicalizes `timeframe`
(`:422`), and derives `asset` (BASE+QUOTE, e.g. `BTCUSDT`) via `strategy_asset()` (`:352-371`).
Result cached in module global `STRATEGIES` (`:432`). `ALLOWED_SYMBOLS` = the set of loaded
`asset`s (`:484`). **No JSON-Schema validation of strategy files happens at load** — only the
`strategy_id`/instrument shape is trusted; malformed files log a warning and are skipped
(`:427-428`). The strategy *alert* schema (§3) is a different artifact.

### 2.3 `control-state.json` structure

Written by `save_control_state` (atomic tmp→fsync→replace, `:1181-1197`); default shape
`default_control_state()` (`:1164-1178`):

```
{ version, updated_at, mode:"shadow_only", live_trading:"paused", manual_pause, pause_reason,
  symbol_pauses:   { SYMBOL: {paused, paused_at, reason} },        # set by pause_symbol :1239
  strategy_overrides:{ sid: {mode, execution_mode, submit_orders, set_at} },  # :1276
  accounting_windows:{ sid: {accounting_start_at(ms), set_at} },   # :1310
  notes }
```

`load_control_state` (`:1199-1224`) merges defaults, re-attaches the three dict fields
explicitly (a plain `default | state` merge would drop them), and remaps legacy override mode
labels `shadow→pause`, `paper→demo` (`:1218-1221`). `mode`/`live_trading`/`manual_pause` are
legacy fields **not** consulted by the execution path (the live 2-control model is per-strategy
`execution_mode` + global `HERMX_LIVE_TRADING`; see `log_execution_arm_state` `:3589-3621`).

### 2.4 Effective-mode resolution (execution path)

`build_strategy_execution_readiness` (`:1416-1517`) resolves the operative mode per signal:

1. `execution_mode = strategy.execution_mode or "demo"` (`:1423`).
2. `submit_orders = strategy.submit_orders` (default `True`, `:1426`).
3. **Override wins:** `control-state.strategy_overrides[sid]` — if present, its `execution_mode`
   and `submit_orders` replace the file values (`:1430-1435`). Checked live per signal (no
   restart needed).
4. `sandbox = (execution_mode != "live")` → `simulated_trading` (`:1436`).
5. `live_execution_enabled = bool(submit_orders)` (`:1439`) — this is the submission arm.

The **dashboard** has its own read-only mirror `_effective_strategy_mode` (`dashboard.py:1604-1615`):
override `mode` wins → else `submit_orders is False` → `"pause"` → else `execution_mode`.
Per-mode flags are kept in sync by `_STRATEGY_MODE_FLAGS` (`webhook_receiver.py:1269-1273`):
`pause={demo,submit False}`, `demo={demo,submit True}`, `live={live,submit True}`.

---

## 3. Inbound Alert Path (TradingView → Receiver)

### 3.1 HTTP endpoint

`class Handler(BaseHTTPRequestHandler)` (`:3432`). **`POST /webhook`** (alias `/shadow/webhook`)
in `do_POST` (`:3523-3583`). Also `POST /api/close` (`/shadow/api/close`, §10/§5.6) and
read-only `GET /health`, `GET /latest` (`:3443-3456`).

`do_POST` order of operations:
1. Content-Length parse → 400 `invalid_content_length` on failure (`:3531-3537`).
2. `length > HERMX_MAX_BODY_BYTES(262144)` → 413 `payload_too_large` (`:3538-3540`).
3. Rate limit: sliding window `120 req / 60s` keyed by client IP (`:3541-3545`, →429).
4. JSON body parse → 400 `invalid_json` (`:3546-3551`).
5. **Auth** `authenticate_webhook_request` (`:3552-3555`) — see §3.2.
6. `record_raw_webhook("intake", …)` fsync to WAL, then `PROCESS_QUEUE.put_nowait` a
   symbol-ticketed work item; queue-full ⇒ mark `"dropped"` + 503 `queue_full`
   (`:3556-3580`). Success ⇒ 200 `{queued}`.

Processing is **asynchronous**: the HTTP handler returns 200 as soon as the item is queued; a
worker thread (`worker_loop`, `:3294-3329`) later dequeues and runs `build_record`.

### 3.2 Auth: shared secret + optional HMAC

`authenticate_webhook_request` (`security/webhook_auth.py:159-181`): (a) blank secret → 401
`missing_webhook_secret`; (b) constant-time compare of `X-Webhook-Secret` → 401 `forbidden`;
(c) `verify_webhook_hmac`. HMAC (`:127-156`): when `HERMX_REQUIRE_HMAC` — requires
`X-Webhook-Timestamp` + `X-Webhook-Signature`, timestamp within `HERMX_REPLAY_WINDOW_SECONDS(300)`,
`HMAC-SHA256(timestamp‖body, key)` compared with `hmac.compare_digest`. Every failure emits an
`AUTH_FAILURE` operator alert (`:174-179`, `webhook_receiver.py:2174-2179`).

### 3.3 Schema validation (`schemas/tradingview-alert.schema.json`)

**Required (6):** `strategy_id`, `symbol`, `timeframe`, `tv_signal_price`, `tv_time`, `source`
(`:7-14`). Plus **`anyOf: [side] or [action]`** (`:15-18`) — this is the effective 7th
requirement. `side ∈ {buy,sell}`; `action ∈ {buy,sell,close}`; `source` const `"tradingview"`;
`timeframe ∈ {30m,1h,2h,3h,4h}`; `tv_signal_price` = number|non-empty string. `extras` is an
optional object (observe-only debug context, logged to `pipeline.jsonl`).

The schema is validated against the **normalized** alert (`validate_alert_schema`,
`webhook_receiver.py:1146-1161`) and is **fail-open**: unavailable `jsonschema`/schema file ⇒
returns `(True,None)`. Enforcement is gated by `strategy_engine.enforce_alert_schema`
(default **OFF**, `webhook/config.py:62`); armed-but-unenforceable emits a one-time
`ALERT_SCHEMA_ENFORCEMENT_UNAVAILABLE` alert (`:1119-1143`).

### 3.4 Pre-schema gates and their ORDER (`build_record`, `:3095-3271`)

`build_record` runs these gates **in this exact order** — note two hard gates precede the schema:

```
normalize(payload)                                            :3096
1. action/side CONFLICT gate  → 400 action_side_conflict      :3098-3111  (opposing open sides)
2. action == "close"          → _build_close_record (returns) :3116-3117  (bypasses side gate)
3. side ∉ ALLOWED_SIDES{buy,sell} → 400 side_not_allowed      :3121-3122
4. source != "tradingview"    → 202 non_tradingview_source    :3123-3124
5. _alert_schema_enforcement_status(); validate_alert_schema  :3135-3155 (quarantine only if enforce ON)
6. validate_strategy_alert    → 202 quarantine on failure     :3157-3171
7. symbol ∉ ALLOWED_SYMBOLS (only when no strategy) → 400     :3172-3173
8. check_and_mark_signal (dedupe) → 200 duplicate short-circ  :3175-3194
9. strategy matched → build readiness + execute_with_advisor  :3196-3246
   else → observe-only no_strategy_match record               :3252-3271
```

This matches the code-quality note: `side`/`source` are gated **before** the schema, so a test
exercising `enforce_alert_schema` must pick a trigger that survives them.

### 3.5 `normalize()` (`:992-1048`) — normalization + backfills

- `symbol` uppercased, strips `OKX:`, `/`, `-` (`:996-997`).
- `action` is primary intent; `side` derived for back-compat; `close` bars carry no `side`
  (dropped so they don't trip the side enum) (`:998-1011, 1043-1044`).
- `timeframe` canonicalized (`:1012`); `tv_time` falls back to `now_iso()` when absent (`:1013`)
  — **the non-determinism replay guards against** (§3.6).
- **`exchange` backfill:** `str(first(payload, "exchange", default="okx")).lower()` (`:1033`) —
  every normalized alert carries an `exchange`, defaulting to `okx`. (This is the "exchange
  backfill" the task references; it sits in `normalize`, not `build_record`.)
- **`signal_id` derivation** (`:1014-1019`): use payload `signal_id` if present, else
  `sha256(f"{strategy_id}|{symbol}|{action}|{timeframe}|{tv_time}")` — hashed on `action` so a
  `close` gets a deterministic id.

### 3.6 Signal dedup

`check_and_mark_signal` (`:767-822`): dedup on **either** `signal_id` **or**
`dedupe_key = strategy_id|symbol|side|timeframe|tv_time` (`dedupe_key`, `:707-708`), within a
`HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS(86400)` window (`:723-729`). The in-memory index
`_SIGNAL_DEDUPE_INDEX` is rebuilt from `signals.jsonl` on first use (`_load_signal_dedupe_index`,
`:732-764`) and each newly-seen signal is appended to `signals.jsonl` **after** the dedupe check
passes (`:813-820`). This window is deliberately independent of the HMAC replay window (`:723-729`).

### 3.7 `signals.jsonl` vs `raw-webhooks.jsonl`

- **`raw-webhooks.jsonl`** (`RAW_WEBHOOK_LEDGER`, `:148`) — durable WAL of every inbound webhook,
  fsync'd before the queue put. Rows tagged `phase ∈ {intake, webhook, dropped}` (`:836-841`).
  It is the **replay recovery source**. Size-rotated at `HERMX_LEDGER_ROTATE_MAX_BYTES(64 MiB)`,
  keep last 5 sealed (`:167-176, 875-896`).
- **`signals.jsonl`** (`SIGNALS_LEDGER`, `:284`) — the single dedupe authority, written **after
  dequeue** (inside `check_and_mark_signal`). Cleanly partitions "processed" from "queued but not
  dequeued": on replay, an intake row not yet in `signals.jsonl` is re-queued. Not size-rotated as
  a WAL; it is the correctness backstop against double-execution.

---

## 4. Signal Processing & Execution Readiness

### 4.1 `validate_strategy_alert` (`:1051-1072`)

Order: no `strategy_id` → (if `require_strategy_id`) reject `missing_strategy_id_required`, else
duo-base heuristic; `allow_strategy_alerts` False → reject; unknown `strategy_id` →
`unknown_strategy_id`; `strategy.asset != symbol` → `strategy_symbol_mismatch`; timeframe mismatch
→ `strategy_timeframe_mismatch`; else `(True, strategy, None)`.

### 4.2 `build_strategy_execution_readiness` (`:1416-1517`)

Checks/derivations, in order:

1. **Mode resolution** — `execution_mode`, `submit_orders`, override merge (§2.4), `sandbox`,
   `live_execution_enabled` (`:1423-1440`).
2. **Direction** — `long` if `side=="buy"` else `short` (`:1441`).
3. **`cl_ord_id` formation** — `signal_identity = _signal_identity(normalized)` =
   `strategy_id|symbol|side|timeframe|tv_time|signal_id` (`:711-715`). Two legs are minted:
   - `client_order_id_open  = stable_client_order_id(identity, role="open")`
   - `client_order_id_close = stable_client_order_id(identity, role="close")`
   - `client_order_id = client_order_id_open` (the open leg is the journal dedupe key) (`:1447-1449`).
   - **Formula** (`:718-720`): `("mxc" + sha256(f"{identity}|{role}").hexdigest())[:32]` — a
     32-char `mxc…` string.
4. **Notional** — `base_notional = budget_usd × leverage`; `planned_notional = dec_notional(...)`
   (`:1450-1451`).
5. **Instrument / venue** — `strategy_instrument(strategy)` → `{exchange, inst_id, type}`
   (`:326-345`); `ccxt_default_type = resolve_default_type(instrument)` (`:1457`).
6. Assembles `execution_intent` with `actions=["CLOSE_OPPOSITE_IF_ANY", f"OPEN_{DIR}"]`,
   both leg ids, `planned_notional_usd` (`:1490-1501`).

Key readiness fields: `execution_mode`, `simulated_trading`(=sandbox), `exchange`(=EXEC_BACKEND),
`instrument`, `strategy_id`, `symbol`, `inst_id`, `td_mode`(=margin_mode), `target_side`,
`live_execution_enabled`, `block_reason`.

> **Note:** the *budget/position/HERMX_LIVE_TRADING* gates the task lists under "readiness" are
> actually enforced later, at submit time inside `ExecutionService.execute` (§5), not inside the
> readiness builder. Readiness only computes `live_execution_enabled` (arming) + mode; there is
> **no explicit budget-remaining check or position-state pre-check in the readiness builder**
> (double-open prevention is done by the adapter's `already_{side}_no_pyramid` /
> `opposite_position_still_open` action expansion, `ccxt_adapter.py:664-689`, plus the order
> journal `duplicate_cl_ord_id` idempotency gate).

### 4.3 What is written at this stage

Nothing is written to the order journal in the readiness builder. `build_record` writes the full
signal record to `raw-webhooks.jsonl` (`phase=webhook`), `pipeline.jsonl`
(`stage=strategy_match`, then `stage=decision`), and `latest.json`, then calls
`execute_with_advisor` (`:3240-3245`). Order-journal `PLANNED`/`SUBMITTED` writes happen inside
`ExecutionService.execute` (§5).

---

## 5. Order Execution

### 5.1 Entry: `execute_with_advisor` → `execute_if_enabled` → `_execute_authoritative`

`execute_with_advisor` (`:2980-2996`) consults the optional advisor (§10.8); a veto returns
`not_submitted/vetoed_by_advisor`. Otherwise `execute_if_enabled` → `_execute_authoritative`
(`:2812-2840`): fail-closed `not_submitted/execution_unavailable` when the execution surface is
unavailable, else `_execute_via_service` → `_run_execution_service` (`:2661-2697`), which
constructs `ExecutionService` with `config=_effective_execution_config()` and the full money-safety
hook set.

### 5.2 `ExecutionService.execute(record)` (`execution/service.py:77-379`)

Gate stack (each blocked gate is a single-exit `_blocked` that logs which gate fired and
returns `ok:True, mode:"not_submitted"`, `:89-95`):

```
Gate 1  arming + health : readiness.live_execution_enabled ∧ auth_healthy ∧ watchdog_ok   :100-117
Gate 2  execution_mode canonical ∈ {demo,live}                                            :119-123
Gate 3  real-venue kill switch (HERMX_LIVE_TRADING) — bypassed for close_only             :125-148
        · is_live_mode OR non_sandbox → require live_trading_enabled()[0]
        · non_sandbox ∧ ¬live_mode → block non_sandbox_requires_live_mode
        · live_mode ∧ sandbox       → block live_mode_simulated_inconsistent
symbol pause  (bypassed for close_only)                                                   :150-158
idempotency   latest_order_record(cl_ord_id) exists → duplicate_cl_ord_id                 :175-183
── write-ahead ──
record_order_state(cl_ord_id, PLANNED,  prev=None)                                         :185-189
record_order_state(cl_ord_id, SUBMITTED, prev=PLANNED)                                      :191-195
record_submit_strategy(...) for every leg id (best-effort, C1 attribution)                :203-219
executor = factory.create(_execution_config(readiness), root); executor.execute(readiness) :224-228
outcome-state mapping → record tentative outcome / reconcile                               :243-378
```

`resolve_execution_config` (`:15-54`) applies two selectors: `execution.exchange` (adapter class,
env-overridable via `HERMX_EXEC_BACKEND`) and `execution.ccxt_exchange`
(= `readiness.instrument.exchange`, so a v2 strategy picks its own venue), plus
`simulated_trading`/`execution_mode`/`ccxt_default_type` from readiness.

Outcome→state mapping (`:243-262`): ok + (`mode=="filled"` or fill status `filled`) → `FILLED`,
else `SUBMITTED`; not-ok `submit_timeout|submit_exception|submit_partial|not_submitted` → `UNKNOWN`;
any other not-ok → `REJECTED`. A `SUBMITTED` ACK records **no** new transition (write-ahead already
persisted it, `:280-284`).

### 5.3 CCXT executor instantiation, sandbox, credentials

`ExecutorFactory.create` (`executors/factory.py:60-74`) resolves `execution.exchange` (default
`EXEC_BACKEND=ccxt`) through aliases (`okx*→ccxt`) to `CcxtExecutor`. `CcxtExecutor._exchange_id()`
(`ccxt_adapter.py:208-216`): `ccxt_exchange or exchange`, and if `""/"ccxt"` falls back to
`HERMX_CCXT_EXCHANGE or "okx"`.

`_client(close_only)` (`ccxt_adapter.py:218-314`): builds the ccxt client with
`timeout=_submit_timeout_ms()` (45s → RequestTimeout maps to UNKNOWN). `mode = "live"` iff
`simulated_trading` is falsey; `resolve_exchange_credentials(exchange_id, os.environ, mode)`
(`security/credentials.py:40-177`) returns **only** the selected venue's namespaced keys with
demo/live preference ordering (fail-closed for Hyperliquid — both wallet+key required, `:159-175`).
Per-venue kwargs (`:236-285`): OKX/KuCoin/Bitget use apiKey/secret/password; Bybit/Binance/Gate
apiKey/secret; Hyperliquid `walletAddress`+`privateKey`. **Sandbox** (`:289-311`): if
`simulated_trading` → `client.set_sandbox_mode(True)` (raises if the venue lacks sandbox); else
**defense-in-depth** — refuse to connect to a live venue unless `close_only` or
`live_trading_enabled()[0]`.

### 5.4 `execute()` → CCXT call path (`ccxt_adapter.py:551-808`)

- Resolve `symbol` (inst_id→ccxt), `direction`, `close_only` (`:551-579`). Non-close with no
  direction → `submit_failed`.
- `_market_spec` (contract size, step, min amount), `_reference_price`, `_amount_from_readiness`
  (notional→contracts, floored to step), `_position_snapshot` (`:586-592`).
- `_expanded_actions` (`:375-397`) turns intent actions into concrete
  `CLOSE_LONG/CLOSE_SHORT/OPEN_LONG/OPEN_SHORT` given the current side.
- For each action: skip/block guards (`no_x_position_to_close`, `zero_size`,
  `already_x_no_pyramid`, `opposite_position_still_open`), then
  `client.create_order(symbol, order_type, side, amount, price, params)` (`:632, :699`).
  `order_type = execution_cfg.order_type or "market"` (`:599`). `params` from `_order_params`
  (`:418-440`): `clOrdId`+`clientOrderId` (Hyperliquid → hashed `clientOrderId` via
  `_to_hyperliquid_cloid`, a `0x`+sha256[:32] hex, `:88-94`); `tdMode` from `td_mode`;
  `reduceOnly:True` on close legs.
- **Distinct leg ids**: close leg uses `client_order_id_close`, open leg `client_order_id_open`
  (`:558-559`) so a reversal's two orders aren't rejected as duplicate `clOrdId`.
- Result status aggregation (`:731-797`): `submit_partial` (some succeeded, some bad) →
  `ok=False, mode=submit_partial`; all-bad → `submit_failed`; all-good → `submit_enabled`
  (Hyperliquid-only fast-path to `filled` when every leg fully filled, `:747-754`); none → `dry_run`.
  Fill summary carries last order id/avg/filled + `position_after_order`.

### 5.5 Submit-time attribution writes

- **`pnl_strategy_map.record_submit_strategy`** — called from the service right after `SUBMITTED`
  is durably journalled, for every leg id (`open`, `close`, `cl_ord_id`) (`service.py:203-219`).
  Writes `{cl_ord_id, strategy_id, venue, mode, ts_ms}` to
  `HERMX_DATA_DIR/cl-ord-strategy-map.jsonl`, fsync'd, **first-write-wins**
  (`pnl_strategy_map.py:74-107`). Best-effort: wrapped in `try/except Exception: pass` so a
  map-write failure can never block the trade.
- **`pnl_cloid_map.record_cloid_mapping`** — Hyperliquid only, from the adapter after a successful
  `create_order` when the venue echoes a numeric/hex cloid ≠ submitted id
  (`ccxt_adapter.py:399-416`). Writes `{mxc_id, cloid, exchange, ts_ms}` to
  `HERMX_DATA_DIR/cloid-map.jsonl` (`pnl_cloid_map.py:30-41`). Best-effort.

### 5.6 Order journal update post-submit

`_record_tentative_outcome` (`service.py:280-300`) records `SUBMITTED→{FILLED|REJECTED|UNKNOWN}`
only when the outcome is not `SUBMITTED`. If `HERMX_RECONCILE_ENABLED` and a reconcile executor is
available, `reconcile_order_with_backoff` may instead drive the authoritative
`SUBMITTED→terminal` transition (`:322-374`). All writes go through
`record_order_state` → `order-journal.jsonl` (durable). The service also appends the outcome to
`PIPELINE_LEDGER` (stage `execution`) via the `append_jsonl` hook (`:378`,
`webhook_receiver.py:2653-2658`).

### 5.7 Error handling at submit

- **CCXT timeout / network** → `_is_timeout_error` → `mode="submit_timeout"` with any partial
  state preserved in the fill summary (`ccxt_adapter.py:643-654, 710-721, 798-808`) → service
  records `UNKNOWN`.
- **Other adapter exception** → `mode="submit_exception"` (`:798-808`) → `UNKNOWN`.
- **Exception inside the service around `executor.execute`** → caught at `service.py:269-278`,
  `mode="submit_exception"`, `UNKNOWN`, secrets redacted.
- **Partial fill / multi-leg partial** → `submit_partial` → `UNKNOWN` + a
  `RECONCILE_MISMATCH` (`post_submit_partial`) alert (`service.py:308-320`).
- **The signal is never automatically re-submitted.** The read-only status query is retried; the
  order (write) is not (`reconcile_order_with_backoff` docstring, `webhook_receiver.py:2102-2108`).

---

## 6. Position & Order State Reconciliation (Receiver-side)

Three reconciliation paths, all **OBSERVE-ONLY against the exchange** (never submit/cancel/auto-trade),
documented at `webhook_receiver.py:209-239`:

1. **STARTUP** — `reconcile_startup()` (`:2269-2342`) runs once on boot (always, not flag-gated,
   from `main()` `:3666-3669`). Reconciles every still-open order from `load_open_orders()` and
   writes legal terminal transitions.
2. **POST-SUBMIT** — inline in `ExecutionService.execute`, gated by
   `reconcile_post_submit_enabled()` = `HERMX_RECONCILE_ENABLED` (default **OFF**, `:1974-1983`).
3. **PERIODIC** — `unknown_resolver_loop` daemon (`:2621-2650`), gated by
   `unknown_resolver_enabled()` (`UNKNOWN_RESOLVER_INTERVAL_SECONDS>0`, default **ON**).

### 6.1 `HERMX_RECONCILE_ENABLED`

Read only in `reconcile_post_submit_enabled` (`:1974-1983`) via `_reconcile_flag_enabled` (`:1967-1971`).
Default False. OFF ⇒ submit path byte-identical to pre-reconcile (stdout-driven tentative record).

### 6.2 Per-order venue+mode resolution (#20a)

Each order's journal record persists `intent.venue` / `intent.mode` / `intent.simulated_trading`
(`_order_intent_from_readiness`, `:1872-1891`). Reconcile resolves the executor for that specific
account:

- `_effective_execution_config(order_intent)` (`:2205-2225`) — base `{exchange:EXEC_BACKEND,
  ccxt_exchange:okx}`, overridden by the intent's `venue`/`simulated_trading`.
- `_reconciliation_executor(order_intent)` (`:2228-2243`) builds the read-only executor (None
  when factory/config unavailable → observe-only).
- `_executor_for_order(intent, cache, default)` (`:2246-2266`) — legacy orders (no venue/mode) →
  `default_executor` (OKX-demo); otherwise a per-`(venue, simulated)`-cached executor.

Both `reconcile_startup` (`:2301`) and `resolve_unknown_orders_once` (`:2495`) use this so a
Bybit-live order is checked on Bybit-live, not OKX-demo.

### 6.3 Order-state polling & terminal detection

`reconcile_order_once` (`:2047-2088`) runs the fallback chain: `get_order` (by ordId/clOrdId) →
`get_open_orders` → `get_order_history_archive`, then `map_order_outcome` (`:1986-2026`):
- `partially_filled` or `0<accFill<ordered` → `FILLED` (partial)
- `filled` → `FILLED`
- `canceled` with fill>0 → `FILLED`(partial); with zero fill → `REJECTED`
- `not_found`/absent → **`UNKNOWN`** (absence is never auto-rejection)
- `live` → `SUBMITTED` (keep polling); else `UNKNOWN`.

`reconcile_order_with_backoff` (`:2091-2135`): ≤5 attempts, delays `0.5,1,2,4s` capped `8s`,
wall-clock budget `20s`; terminal returns immediately; deadline-exhausted → `UNKNOWN`
`deadline_exhausted:<reason>`.

### 6.4 Periodic resolver & lifecycle backstops

`resolve_unknown_orders_once` (`:2453-2618`): iterates open `PLANNED/SUBMITTED/UNKNOWN` orders
(≤`UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK=50`). A `PLANNED` orphan older than
`PLANNED_ORDER_TIMEOUT_SECONDS(300)` → `_resolve_planned_orphan` (`:2366-2450`): venue absent ⇒
legal `PLANNED→REJECTED` `never_submitted` + `PLANNED_ORDER_ABANDONED`; venue present (anomaly) ⇒
`PLANNED→SUBMITTED` + `PLANNED_ORDER_ON_VENUE`. A `SUBMITTED/UNKNOWN` order older than
`UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS(900)` → **never auto-closed**; instead `pause_symbol` +
`RECONCILE_MISMATCH` + `UNKNOWN_RESOLVER_TIMEOUT` (deduped per symbol/cl_ord_id/state). Age is
measured from `origin_ts` so re-recording never resets the clock (`:2491-2493`).

### 6.5 Journal transitions `pending → filled → closed`

The order journal is a **submission** state machine `PLANNED→SUBMITTED→{FILLED|REJECTED|UNKNOWN}`
(`_ORDER_STATE_TRANSITIONS`, `:1535-1542`; `order_state_can_transition`, `:1545-1548`). There is
**no `closed` order-journal state** — "closed" is a P&L concept: a *close fill* observed in
exchange order history is folded into `closed-trades.jsonl` by the dashboard reconcile path (§7),
independent of the order journal. `record_order_state` (`:1836-1869`) validates the transition and
durably appends; the in-memory index (`_order_index`, checkpoint + live tail) is the O(1)
idempotency/open-orders authority. Segment rotation folds the live segment into a verified
checkpoint every `HERMX_JOURNAL_SEGMENT_MAX_RECORDS(1000)` records (`:1785-1833`).

---

## 7. P&L Ledger Write Path

### 7.1 When `reconcile_from_order_history` runs

`pnl_ledger.reconcile_from_order_history` (`pnl_ledger.py:524-586`) is invoked from the
**dashboard** on model build, NOT from the receiver's submit path:

- `strategy_order_history_snapshot(rep, mode_key)` (`dashboard.py:906-978`) — the Phase-0.5
  per-`(venue,mode)` path, called from `dashboard_model` for each distinct environment
  (`:1575-1580`). Passes the strategy's OWN `venue`, `mode_key` (`:963`).
- `okx_order_history_snapshot(config)` (`dashboard.py:1108-1147`) — legacy path; passes the
  actual `(venue, mode)` read off the executor via `_executor_venue_mode` (`:1133-1134`), mode
  possibly `None`. (`dashboard_model` uses the per-env path; the legacy snapshot is retained for
  legacy callers/tests.)

Both are wrapped in `try/except: pass` so a reconcile failure never fails the read-only snapshot.
The dashboard model is cached `MODEL_CACHE_TTL_SECONDS(10)` (`dashboard.py:80`), so writes occur at
most every ~10s per model rebuild.

### 7.2 Close detection & `_build_entry`

`reconcile_from_order_history` (`:524-586`) tracks a signed running position per instrument across
**all** rows (sorted chronologically by `_row_ts`) and flags a close when `reduceOnly` is truthy
**or** the fill is on the opposite side of a non-zero position (position-delta, works for spot
venues, Decision 3). Only HermX-attributed closes are written: `is_hermx_cl_ord_id` (`:121-144`)
accepts `mxc…`, `operator_close_…`, or a Hyperliquid numeric/hex cloid resolvable via the cloid map.

`_build_entry` (`:460-521`) field-by-field:
- `pnl_gross` = `realized_pnl` else native `pnl` else `None` (honest unknown, logged) (`:463-473`).
- `fee_cost` = `fee`; `cl_ord_id` = `clOrdId|clientOrderId`, Hyperliquid-resolved via cloid map
  (`:474-484`).
- **Attribution resolution:** (a) row `strategy_id` if present; (b) submit map lookup
  `pnl_strategy_map.resolve_strategy(cl_ord_id)` (`:492-495`); (c) `operator_close_…` parse
  `_parse_operator_close_strategy_id` (`:496-499`, `:164-202` — peels symbol via `inst_id`, then
  submit-map probe); (d) else `strategy_id=None` — **the row is still written**, only attribution
  is best-effort.
- `net_realized_pnl = _compute_net_realized(gross, fee, exchange)` (`:57-92`): `gross + signed fee`
  by default (`ORDER_PNL_IS_NET[venue]=False`).
- `closed_at_ms = _row_ts(row)` (uTime→cTime→closed_at_ms, `:447-457`);
  `recorded_at_ms = now ms` (schema v3 local observation time, `:519`).

### 7.3 `append_closed_trades` (TOCTOU-safe)

`append_closed_trades` (`:410-444`): under a thread `_LOCK` and an **exclusive `flock`** taken
BEFORE reading existing keys — the full read-modify-write cycle is atomic across processes
(post-H3 fix; the pre-fix key-read outside the lock allowed two writers to append the same ordId).
Dedup composite key `(exchange, inst_id, ord_id, mode)` (`:117-118`); new lines fsync'd. Read-side
also dedupes by the same key as a backstop (`read_closed_trades`, `:261-268`).

### 7.4 `closed-trades.jsonl` schema (v3, `SCHEMA_VERSION=3`, `:34`)

Fields written by `_build_entry` (`:500-521`): `schema_version`, `exchange`, `inst_id`, `ord_id`,
`mode`, `strategy_id`, `side`, `filled_qty`, `avg_px`, `pnl_gross`, `fee_cost`, `fee_currency`,
`net_realized_pnl`, `closed_at_ms`, `recorded_at_ms`, `cl_ord_id`.

---

## 8. P&L Read & Aggregation Path

### 8.1 `read_closed_trades` (`:205-268`)

Filters: `strategy_id` (exact); `since_ms` and `accounting_start_at` combined by
`effective_since = max(present floors)` (`:221-224`) — rows with `closed_at_ms < effective_since`
dropped. **On read**, v1 rows get `net_realized_pnl` back-filled from stored gross+fee (never
persisted, `:249-254`); v1/v2 rows get `recorded_at_ms=None` (`:258-259`). Corrupt lines skipped.
Read-side dedupe by composite key (last-wins).

### 8.2 `aggregate_strategy_pnl` (`:346-388`)

```
rows = read_closed_trades(strategy_id, accounting_start_at)   # then filter by mode
closed_net      = Σ (net_realized_pnl or 0)
closed_realized = Σ (pnl_gross or 0)           # gross
closed_fees     = Σ (fee_cost or 0)
equity_now_usd  = budget_usd + closed_net + open_upl
```
Also returns `open_upl_usd`, `closed_order_count`(len rows), `last_close_at_ms`(max ts),
`accounting_start_at`. Missing ledger ⇒ all-zero (never raises).

### 8.3 `net_realized_for_strategy` (`:327-343`)

`read_closed_trades(strategy_id, accounting_start_at)`, filter by `mode` when given, return
`Σ(net_realized_pnl or 0.0)` (None nets count as zero).

### 8.4 `ORDER_PNL_IS_NET` (`:43-49`)

Per-venue table, **all `False`** (`okx, hyperliquid, binance, bybit`). `False` ⇒ venue `pnl` is
gross and net = `gross + signed fee`. Flipping a venue to `True` makes net == gross (venue already
netted fees). Ships gross-first; net is not displayed as authoritative until a venue is verified
empirically (`_compute_net_realized` docstring `:71-82`; test-locked by `test_pnl_net.py`).

### 8.5 Schema-v1 backfill on read

See §8.1: `net_realized_pnl` derived from gross+fee for rows lacking it; `recorded_at_ms=None` for
pre-v3 rows. Both derived-on-read, never persisted (keeps the ledger append-only/rollback-safe).

---

## 9. Dashboard Snapshot Path

### 9.1 `dashboard_model()` (`dashboard.py:1537-1601`)

Order: cache check (`MODEL_CACHE_TTL_SECONDS=10`) → clear `LEDGER_READ_STATS` → load events +
strategy files + control state → compute `_any_live` → legacy demo/live snapshots
(`okx_live_by_mode`) → **enumerate distinct environments** → per-env live snapshot + order-history
reconcile → assemble model (executor health, ledger health, freshness).

### 9.2 Environment enumeration

`seen_envs` maps `(venue, mode_key)` → representative strategy (`:1568-1574`), where
`venue=_strategy_venue(s)` (`:768-781`, `instrument.exchange` / `execution.ccxt_exchange`, default
`okx`, treating `"ccxt"` as unset) and `mode_key = live|demo` from `_effective_strategy_mode`. For
each env: `okx_live_by_env["{venue}:{mode}"] = strategy_live_snapshot(rep, mode)` and
`strategy_order_history_snapshot(rep, mode)` (drives the ledger reconcile) (`:1575-1580`).

### 9.3 `strategy_live_snapshot` / `strategy_order_history_snapshot` (`:819-978`)

Both build a per-strategy executor via `_strategy_executor` (`:800-816`, pins the strategy's venue
+ maps mode→`simulated_trading`, delegating to `_dashboard_executor`). Cache key
`snapshot:{venue}:{mode}`; TTLs `OKX_LIVE_CACHE_TTL_SECONDS=5` (positions) /
`OKX_ORDER_HISTORY_CACHE_TTL_SECONDS=15` (history) (`dashboard.py:82-85`). `strategy_live_snapshot`
reads `executor.health()` (positions/balance); `strategy_order_history_snapshot` reads
`get_order_history_raw(inst_ids, limit=100)`, runs the P0-1 age-out detector (saturated 100-row
window whose oldest row post-dates the ledger high-water → `history_window_ageout` alert,
`:935-957`), then folds via `reconcile_from_order_history(rows, venue, mode_key)` (`:960-965`).
**Fail-closed:** a live read with `HERMX_LIVE_TRADING` disarmed degrades to the demo snapshot with
a stderr warning (`:830-836`).

### 9.4 P&L contract calls

`_strategy_pnl_contract(strategy, accounting_start_at, by_env, by_mode)` (`:1618-1679`): resolves
`venue`, `mode_key`, `budget`, open UPnL from `_snapshot_for_env` (`:981-992`), then
`aggregate_strategy_pnl(sid, budget_usd=budget, mode=mode_key, accounting_start_at, open_upl_usd)`
(`:1643-1649`). Returns a superset with Phase-3 `*_usd` keys **and** Phase-4 aliases
(`realized_net`, `realized_gross`, `fees`, `upl`, `total_net`, `trade_count`, `last_close_at_ms`,
`venue`, `mode`). Fail-open to budget+UPnL on any ledger error.

`portfolio_contract(strategy_pnls)` (`:1700-1728`): additive sums of `realized_net/gross/fees/upl`,
`total_net = realized_net + upl`, `trade_count`, and `strategies` = count with any P&L data (a row
OR a live position). Assembled in `api_payload()` (`:1731-1806`), which also attaches per-strategy
`effective_mode`, `venue`, `env_key`, `accounting_start_at`, `strategy_pnl`, plus top-level
`portfolio` and `reconcile_health` (`reconcile_health_stats`, `pnl_ledger.py:289-324`).

### 9.5 Two executor regimes (M4)

- **Legacy `_dashboard_executor(config, simulated_trading)`** (`:740-765`): pins
  `exchange="ccxt"` and, when `ccxt_exchange` absent, **`ccxt_exchange="okx"`** — the OKX fallback
  that remains after shadow-config removal. Feeds `okx_live_snapshot`/`okx_order_history_snapshot`
  and thus `okx_live_by_mode` + `okx_live` (demo) + `okx_executions`.
- **`_strategy_executor(strategy_config, mode)`** (`:800-816`): pins the strategy's own venue and
  `simulated_trading`. Feeds `okx_live_by_env` + the per-env ledger reconcile.

`dashboard_model` drives ledger writes through the **per-env** regime (`:1580`); the legacy regime
still supplies the demo-account `okx_live`/`okx_executions` panels. **M4 gap:** `_dashboard_executor`
hard-defaults the venue to `okx`, so any legacy-regime read/reconcile is OKX-only (see §13).

---

## 10. Exception & Error Flows

| Condition | Response / behavior | Where |
|---|---|---|
| HMAC / secret auth failure | HTTP 401 `{ok:false,error:<reason>}` + `AUTH_FAILURE` alert | `webhook_receiver.py:3552-3555`, `webhook_auth.py:170-181` |
| Schema validation failure | Enforce OFF: logged + counted, processed anyway. Enforce ON: 202 `strategy_alert_quarantine` | `webhook_receiver.py:3136-3155` |
| Strategy unknown / mismatch | 202 quarantine (`unknown_strategy_id`/`strategy_symbol_mismatch`/…) | `:1051-1072`, `:3157-3171` |
| Symbol paused | Service blocks `symbol_paused` gate (`ok:true, not_submitted`); bypassed for close | `execution/service.py:150-158` |
| CCXT exception at placement | `submit_exception`/`submit_timeout` → order journal `UNKNOWN`; secrets redacted; **not re-submitted** | `ccxt_adapter.py:798-808`, `execution/service.py:269-278` |
| Partial multi-leg submit | `submit_partial` → `UNKNOWN` + `RECONCILE_MISMATCH` | `execution/service.py:308-320` |
| Reconcile failure (dashboard) | Swallowed (`try/except: pass`); render **degraded, not failed** | `dashboard.py:960-965`, `:1130-1136` |
| Ledger write failure (P&L) | Best-effort; caller wrapped — **execution NOT blocked** | `execution/service.py:203-219` |
| **State-write failure** (order journal/checkpoint, e.g. ENOSPC) | `_fail_closed_state_write` logs + alerts, caller **re-raises → money path BLOCKED** | `webhook_receiver.py:1402-1413`, `execution/service.py:185-195` |
| `HERMX_LIVE_TRADING` disarmed mid-flight | No effect on an in-flight submit already past the gate; the **next** submit re-evaluates the gate (read live per call). Dashboard live reads fail-closed to demo. | `hermx_shared.py:67`, `dashboard.py:830-836` |
| Queue full | 503 `queue_full` + `dropped` WAL marker + `QUEUE_SATURATION` alert | `webhook_receiver.py:3559-3580` |
| Watchdog degraded | Submission paused (Gate 1 `watchdog`) until recovery | `:616-652`, `execution/service.py:100-117` |

### 10.8 Pre-execution advisor (fail-open veto)

`run_execution_advisor` (`:2947-2977`): disabled by default (`HERMX_ADVISOR_ENABLED`). When
enabled, runs Hermes one-shot (`hermes -z <prompt> --skills hermx-control [-m model]`,
`:2903-2914`), parses strict JSON `{action:proceed|skip,…}`. Any timeout/transport/parse error
**fails OPEN** (proceed). A `skip` is a veto → `not_submitted/vetoed_by_advisor`
(`:2980-2996`). The advisor can never change symbol/side/size/leverage.

---

## 11. Accounting Windows

- **Storage:** `control-state.json → accounting_windows[sid] = {accounting_start_at(ms), set_at}`
  (`webhook_receiver.py:1310-1335`). Set via `set_accounting_start(sid, start_ms)`;
  `start_ms=None` → `clear_accounting_start`.
- **API surface:** the receiver exposes `set_accounting_start`/`clear_accounting_start`/
  `accounting_start_for` (`:1310-1367`); the dashboard mirrors `_set_accounting_start`/
  `_clear_accounting_start`/`_accounting_start_for` (`dashboard.py:266-322`) and surfaces the value
  in `api_payload` (`:1764-1766, 1788`). **The concrete HTTP route/params that call these are
  TBD — verify in code** (they were not located among the read files; likely a dashboard POST
  handler outside the functions inspected).
- **`clear_accounting_start`** (`:1338-1351`) removes the window entry; a missing/None window means
  the whole ledger counts (epoch-0 equivalent — no floor applied).
- **Interaction with `since_ms`:** `read_closed_trades` takes `effective_since = max(since_ms,
  accounting_start_at)` over whichever floors are present (`pnl_ledger.py:221-224`) — the stricter
  (later) floor wins; unset window ⇒ no accounting floor, so the whole ledger (or just the freshness
  floor) applies.

---

## 12. Data Files Reference

Path resolution: **DATA** = `HERMX_DATA_DIR` → `HERMX_ROOT` → repo root; **LOG** = `HERMX_ROOT/logs`
(receiver `:131-137`, ledger `:95-101`).

| File | Path | Purpose | Rotation/pruning | Created by | Read by |
|---|---|---|---|---|---|
| `raw-webhooks.jsonl` | LOG | Durable WAL of every inbound webhook (`intake`/`webhook`/`dropped`) + replay source | Size-rotate 64 MiB, keep 5 sealed (`:875-896`) | `record_raw_webhook` (`:932`) | `replay_intake_webhooks` (`:3332`), dashboard |
| `signals.jsonl` | LOG | Dedupe authority (written post-dequeue) | Not WAL-rotated | `check_and_mark_signal` (`:813`) | `_load_signal_dedupe_index` (`:732`), dashboard |
| `pipeline.jsonl` | LOG | Signal-processing events by `stage` (execution outcomes, decisions, advisor, errors, replay) | Size-rotate 64 MiB, keep 5 (`:929`) | `record_pipeline_event` (`:905`) | dashboard `_pipeline_rows` |
| `order-journal.jsonl` | LOG | Write-ahead order state machine `PLANNED→SUBMITTED→terminal` | Checkpoint+seal every 1000 records, keep 5 sealed (`:1785-1833`) | `record_order_state` (`:1836`) | `_order_index`, `load_open_orders`, dashboard open-orders panel |
| `order-journal.checkpoint.json` | LOG | Verified latest-per-cl_ord_id fold subsuming sealed segments | Overwritten atomically | `_order_checkpoint_and_rotate` (`:1785`) | `_read_order_checkpoint` (`:1732`), dashboard |
| `closed-trades.jsonl` | DATA | Lifetime realized-P&L ledger (schema v3) | **Never pruned** (`:1-6`) | `append_closed_trades` (`:410`) | `read_closed_trades`/`aggregate_strategy_pnl` (`:205,346`) |
| `cl-ord-strategy-map.jsonl` | DATA | Submit-time `cl_ord_id→strategy_id` (C1 attribution) | Append-only, first-write-wins | `record_submit_strategy` (`pnl_strategy_map.py:74`) | `resolve_strategy` (`:67`) |
| `cloid-map.jsonl` | DATA | Hyperliquid `mxc→cloid` submit-time map | Append-only | `record_cloid_mapping` (`pnl_cloid_map.py:30`) | `resolve_cloid` (`:44`) |
| `control-state.json` | DATA | Overrides, symbol pauses, accounting windows | Atomic overwrite | `save_control_state` (`:1181`) | receiver + dashboard |
| `alerts.jsonl` | LOG | Unified operator/reconcile/state alerts (`kind`) | (none observed) | `emit_operator_alert`/`emit_reconcile_alert` (`:2138,2193`) | dashboard alert panels |
| `latest.json` | DATA | Last processed record snapshot | Atomic overwrite | `_atomic_json_dump` (`:3091,3245`) | `GET /latest` |
| `engine-config.json` | ROOT | `strategy_engine` + `advisor` config only | Static | operator | `load_engine_config` (`webhook/config.py:52`) |

---

## 13. Known Gaps & Deferred Items (as of 2026-07-03)

- **Phase 0.5 gap (M4) — `_dashboard_executor` OKX fallback.** `_dashboard_executor`
  (`dashboard.py:740-765`) pins `ccxt_exchange="okx"` when absent. `dashboard_model` routes ledger
  writes through the per-env `_strategy_executor` (correct venue), but the legacy
  `okx_live_snapshot`/`okx_executions` regime it still feeds is OKX-only, so its position/execution
  panels can't represent a non-OKX venue on that legacy surface.
- **Bybit (and others) realized P&L.** `_normalized_realized_pnl` returns `None` for bybit and any
  venue not in {okx, hyperliquid, binance} (`ccxt_adapter.py:43-63`); `_build_entry` then persists
  `pnl_gross=None` (honest unknown), so those closes carry no realized figure until a positions-history
  backfill is added (`pnl_ledger.py:463-473`).
- **`ORDER_PNL_IS_NET` unverified per venue.** All `False` (`pnl_ledger.py:43-49`). Net is computed
  (`gross+signed fee`) but gross stays the displayed value; flipping a venue to `True` requires an
  empirical fee-sign check on a real close first.
- **Position-delta partial-fill fragility (M5).** Close detection uses a running signed-position
  delta with a `QTY_EPS(1e-9)` snap-to-zero (`pnl_ledger.py:53-54, 548-576`); a sign-flip logs a
  warning but is still treated as a close. Partial fills that self-report `remaining==0` are guarded
  only conservatively (`_order_fully_filled`, `ccxt_adapter.py:183-198`).
- **Multi-strategy-per-symbol ambiguity.** Attribution is per `cl_ord_id` via the submit map; the
  operator-close id parse (`_parse_operator_close_strategy_id`, `pnl_ledger.py:164-202`) returns
  `None` when the `{symbol}_{sid}` split stays ambiguous — flagged (row persists unattributed), not
  resolved.
- **React UI not yet wired to `strategy_pnl`/`portfolio` (Phase 4).** The contracts are produced in
  `api_payload` (`dashboard.py:1766-1786`) as additive supersets; adoption by the React UI is
  deferred (docstrings at `dashboard.py:1626-1630`, `:1761-1763`).
- **Accounting-window HTTP route TBD.** The set/clear functions exist and are payload-surfaced, but
  the concrete dashboard POST handler wiring them was not among the files read — **verify in code**.

---

*End of document.*
