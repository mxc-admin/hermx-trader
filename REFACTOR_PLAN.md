# HermX Refactor Execution Plan

> Consolidated, phased, risk-aware refactor plan for the HermX strategy-driven trading bot.
> Scope: `webhook_receiver.py`, `dashboard.py` / `dashboard_core.py`, the executor layer,
> strategy/alert schemas, configuration, and the surrounding test/CI/ops process.
> Status target: a **live-ish** demo bot (OKX sandbox today, real-money capable later).

---

## 0. Context & Operating Assumptions

### 0.1 What the system is today

```
TradingView alert ──HTTP──▶ webhook_receiver.py (127.0.0.1:8891, single thread)
                                │  validate secret, parse, enqueue, return 200
                                ▼
                         PROCESS_QUEUE (unbounded)
                                │
                                ▼
                     ONE daemon worker thread
                                │  normalize → match strategy → read indicators
                                │  → paper-trade sim → build readiness → execute
                                ▼
                     subprocess: okx_demo_executor.py execute   (OKX REST, HMAC)
                                │
                          logs/*.jsonl + *.json state
                                ▲
                                │ reads (separate process)
                      dashboard.py (127.0.0.1:8098)
```

- **Concurrency model (verified):** `HTTPServer` is single-threaded (`webhook_receiver.py:2335`); exactly **one** worker thread consumes the queue (`:2334`, `worker_loop` `:2278`). The HTTP handler only enqueues and returns immediately (`:2325-2326`). Therefore in-memory/state mutation is **serialized today**.
- **This materially changes risk triage.** The "CRITICAL race conditions on paper state" reported by static analysis are **latent, not active** — they become real only if the worker pool grows or the server becomes threading. They are tracked here as hardening, not P0 bugs. The genuine P0s are crash recovery, exchange reconciliation, auth, unbounded queue, and head-of-line blocking.
- **Two executor systems coexist.** Top-level `src/okx_demo_executor.py` (751 lines, battle-tested, the only one actually invoked) + an unused `src/executors/` package (factory + adapters) + a dead third base class `src/executors/base_executor.py`. `ExecutorFactory` is imported but never called (`webhook_receiver.py:201`).
- **Everything is OKX-coupled** despite an exchange-agnostic ambition documented in `ARCHITECTURE.md`.

### 0.2 Guiding constraints

1. **The bot may be running.** Every phase must be deployable without an extended outage and must be revertible. Prefer additive, flag-gated changes.
2. **Money safety beats elegance.** No refactor that touches order submission ships without a kill switch and a dry-run proof.
3. **Tests before behavior changes.** The execution and PnL math currently have ~no automated tests; we add characterization tests *before* refactoring the code they cover.
4. **Demo first, live never by inference.** Live execution must remain gated by explicit runtime profile + operator confirmation (already a stated principle in `ARCHITECTURE.md`).

### 0.3 Severity legend

`P0` ship-blocker / money-or-safety risk · `P1` high · `P2` medium · `P3` cleanup.

---

## 1. Findings Inventory (consolidated)

Grouped by theme; each maps to a phase below. Line numbers are current as of analysis.

### 1.1 Execution engine & state (Phase 1, 3)
| ID | Sev | Location | Issue |
|----|-----|----------|-------|
| E0 | P0 | `webhook_receiver.py:1962, 2020, 2040, 2084` | Undefined constants `OKX_EXECUTION_PLAN_LEDGER` / `OKX_EXECUTION_LEDGER` trigger `NameError` during readiness/execution logging. Since exceptions are swallowed by async processing, webhook HTTP returns success while downstream decision/execution records fail to persist (silent processing failure). |
| E1 | P0 | `webhook_receiver.py:1534-1552`, `1877-1886` | Paper/position state is read-modify-write of a JSON blob in memory; a crash between load and `save_paper_state()` **loses the open position**. On restart a later close finds nothing and silently no-ops → real market exposure invisible to the system. |
| E2 | P0 | `:2024-2085` | **No exchange reconciliation.** Order result is parsed from subprocess stdout only; on subprocess crash/timeout/invalid-JSON there is no "did it actually fill?" query to OKX, and nothing on restart reconciles open exchange positions against local state → orphaned orders / liquidation risk. |
| E3 | P0 | `:2049` (`timeout=45`) + single worker `:2278` | **Head-of-line blocking.** One slow/timed-out OKX call stalls *every* queued alert for up to 45s. Four strategies firing on the same bar close can serialize into minutes of latency. `subprocess.TimeoutExpired` may also leave an order in flight. |
| E4 | P1 | `:306-308` (`append_jsonl`) | JSONL appends have no `flush()`/`fsync()`; a crash mid-write leaves a truncated final line that breaks every reader of that ledger. |
| E5 | P1 | `:1534-1546` | Corrupt `paper-state.json` is silently swallowed and replaced with empty state → **position blindness**, same end-effect as E1. |
| E6 | P2 | `:1757-1766` | PnL/notional float math accumulates rounding; no `Decimal` for money. Minor now, compounds in `compound_policies`. |
| E7 | P2 | dedupe `:254-303`, cap 5000 `:269` | Dedup state is in a file capped at 5000 entries and **reset semantics on restart** allow replay of an old captured alert after a restart/state-clear. |
| E8 | P1 (latent) | `:1702-1827`, `:266-273` | In-memory state races + tmp-file TOCTOU — **dormant** under the single-worker model; must be fixed *before* any concurrency increase. |

### 1.2 Security (Phase 2)
| ID | Sev | Location | Issue |
|----|-----|----------|-------|
| S1 | P0 | `:2313-2315`, `:25` | Webhook auth is a **non-constant-time string compare** and the secret is also accepted via **query string** (`?secret=`), which lands in proxy/access logs and browser history. |
| S2 | P0 | `:2313` | **No HMAC body signature and no timestamp/replay window.** Anyone who learns the shared secret can inject arbitrary alerts; a captured request can be replayed. |
| S3 | P1 | unbounded `PROCESS_QUEUE` `:178`, no rate limit | A flood of (even unauthenticated-rejected, but auth happens after `Content-Length` read) requests grows the queue/memory without bound → DoS. |
| S4 | P1 | `:2043-2051`, `:2064-2065` | Full parent env (all secrets) copied into the executor subprocess; on failure, subprocess `stdout/stderr[-2000:]` (may contain key fragments / tracebacks) is written into the execution ledger. |
| S5 | P1 | `dashboard.py:1827`, `:1798-1820`, `/api` | Dashboard binds localhost (good) but has **no auth**; behind the Cloudflare tunnel it would expose live positions, budgets, PnL, and signals to anyone with the URL. |
| S6 | P2 | secrets via env, no rotation | OKX keys live in `.env` / process env, readable via `/proc/<pid>/environ`; no documented rotation. |
| S7 | P3 | `dashboard.py:177` (`esc`) | Output is escaped today; keep it that way if any user-influenced field is ever surfaced. |

### 1.3 Dashboard reliability (Phase 4)
| ID | Sev | Location | Issue |
|----|-----|----------|-------|
| D1 | P1 | `dashboard_core.py:18-29`, callers `dashboard.py:126,335,671` | JSONL reader loads the **whole file** into memory then slices `[-limit:]`; on a multi-hundred-MB ledger this is slow and OOM-prone, and silently drops corrupt lines with no signal. |
| D2 | P1 | `dashboard_core.py:22` | Reads files the worker is concurrently appending to → intermittent truncated-tail parse loss (cross-process). |
| D3 | P1 | `dashboard.py:461,529` | OKX subprocess JSON parse failure renders an empty "flat" view with no visible error → operator believes bot is flat when executor actually crashed. |
| D4 | P1 | `dashboard.py:39` `POLICIES = ()` | Policy simulation loop never runs; legacy policy tabs/`/api` policies are silently empty. |
| D5 | P1 | `dashboard_core.py:14`, `dashboard.py:98,1233,1356,1521` | **Assets are hardcoded** (`SYMBOLS`, sort map) — directly violates `ARCHITECTURE.md`'s "dashboard must be strategy-file-driven, one card per active strategy." Adding/removing a strategy needs code edits. |
| D6 | P2 | `dashboard_core.py:56-69` | `parse_dt` returns `None` for naive timestamps → all times render `-`; tz hardcoded to `-05:00`. |
| D7 | P2 | `dashboard.py:41-46`, `:976-995`, `:1672` | Cache TTL vs client refresh interval mismatch; "Updated" label shows cache-expiry, not data age → silently stale. |
| D8 | P2 | `:132-153` vs `dashboard.py:73-82` | `canonical_timeframe` duplicated in receiver and dashboard; drift breaks matching. |

### 1.4 Executor architecture (Phase 5)
| ID | Sev | Location | Issue |
|----|-----|----------|-------|
| X1 | P1 | `src/executors/base_executor.py` | Dead third base class; only importer is the orphaned `src/kucoin_paper_executor.py:14` (wrong import path). |
| X2 | P1 | `webhook_receiver.py:201`, `dashboard.py:442,507` | Execution & dashboard hardcode the OKX script path; `ExecutorFactory` is imported but never used. Two execution models (OKX out-of-process, KuCoin in-process). |
| X3 | P2 | `okx_demo_executor.py` whole file | OKX REST/signing/contract-math all inline; adding Bybit/Binance means duplicating ~1k lines unless the adapter contract is adopted. |
| X4 | P2 | `base.py:39-103` | The clean `BaseExecutor` contract exists but `readiness` schema is undocumented and field naming is inconsistent (`inst_id` vs `okx_inst_id`). |

### 1.5 Schema / exchange-agnosticism (Phase 6)
| ID | Sev | Location | Issue |
|----|-----|----------|-------|
| M1 | P2 | `schemas/strategy.schema.json:37,64`, every `strategies/*.json` | Strategy schema bakes in OKX: required `okx_inst_id`, `okx_submit_orders`, `asset` regex pinned to `*USDT`. Not exchange-agnostic. |
| M2 | P2 | `schemas/tradingview-alert.schema.json:48-51` | Alert `exchange` enum is `["okx"]` only. |
| M3 | P2 | `webhook_receiver.py:1920-1997` | Readiness payload uses OKX-named keys (`okx_inst_id`, `td_mode`); the generic instruction shape in `ARCHITECTURE.md` (`asset/target_side/target_notional_usd/margin_mode/leverage`) is not the wire format. |

### 1.6 Cross-cutting (Phase 0, 7)
- No automated tests anywhere; `scripts/validate_package.py` is the only check.
- Not a git repository (`git log` failed) → **no version control safety net**.
- Two 1.8k–2.3k-line god files; high change-impact radius.

---

## 2. Phased Execution Plan

Phases are ordered so that **safety nets precede risky edits**, **bug fixes precede architecture**, and **the bot stays runnable throughout**. Each phase is independently shippable and revertible.

```
P0  Foundation & Safety Net        (git, tests, kill switch)        ~0.5–1 wk
P1  Critical Bug Fixes             (state durability, reconcile)    ~1–2 wk   ← highest money-risk
P2  Security Hardening             (auth, HMAC, queue bounds)       ~1 wk
P3  Execution Engine Stabilization (idempotency, timeouts, atomic)  ~1–2 wk
P4  Dashboard Reliability          (safe reads, dynamic cards)      ~1 wk
P5  Executor Consolidation         (one factory, delete dead code)  ~1 wk
P6  Schema Modernization           (exchange-agnostic)              ~1 wk
P7  Test Strategy & CI Hardening   (coverage gates, pipeline)       ongoing
```

> Sequencing note: P2 (security) is placed before P3 because the bot is reachable via a public tunnel and the auth gap (S1/S2) is exploitable *today*; it is cheap and isolated. P1 is first because it is the only class of bug that can lose money silently.

### Immediate Hotfix Track — 24h stabilization (must land first)

**Goal:** Stop silent processing failure and force safe execution posture before broader refactor phases.

**Tasks**
1. Fix undefined constant references in `webhook_receiver.py`:
   - `OKX_EXECUTION_PLAN_LEDGER` -> `EXECUTION_PLAN_LEDGER`
   - `OKX_EXECUTION_LEDGER` -> `EXECUTION_LEDGER`
2. Add a regression test to fail on unresolved `*_LEDGER` symbols in `src/webhook_receiver.py`.
3. Temporarily enforce dry-run while refactor is in progress:
   - `shadow-config.json`: `execution.submit_orders=false`
   - `shadow-config.json`: `risk.allow_live_execution=false`
4. Add startup self-check log that prints effective execution arm state (submit enabled, live allowed, simulated mode).

**Acceptance criteria**
- [ ] No `NameError` for execution ledgers in `shadow-processing-errors.jsonl` after synthetic alert run.
- [ ] `execution-plan.jsonl` and `executions.jsonl` receive entries for processed alerts.
- [ ] Regression test fails if undefined ledger constants are reintroduced.
- [ ] Execution remains explicitly dry-run during refactor rollout.

---

### Phase 0 — Foundation & Safety Net

**Goal:** Make change safe before changing anything. No behavior change.

**Why first:** There is no git history and no tests. Refactoring money code without either is reckless.

**Tasks**
1. `git init`, commit the current tree as the baseline, add a remote (private). Confirm `.gitignore` already excludes `.env`, state files, and `logs/` (it does) — **verify no secrets were ever committed**. Run secret scan **before first push**.
2. Pin dependencies: `requirements.txt` currently only lists `kucoin-python`. Add explicit versions for everything actually imported (stdlib is fine, but pin `kucoin-python`, add `pytest`, `jsonschema`, and a lint/format toolchain to a `requirements-dev.txt`).
3. Stand up a test harness (`pytest`) with a `tests/` dir and fixtures that point `SHADOW_ROOT` at a temp dir so tests never touch real logs/state.
4. **Capture a fixture corpus (prerequisite for all later test work).** Before any characterization test is written, record a versioned corpus of **real, anonymized inputs** under `tests/fixtures/`: representative TradingView alert payloads (at least one per active strategy, covering buy / sell / reverse plus one malformed and one duplicate), the matching `strategies/*.json`, captured `okx_demo_executor` stdout for **fill / partial-fill / reject / timeout** outcomes, a seed `paper-state`/journal snapshot, and recorded OKX REST payload fixtures for reconciliation paths (`trade/order`, `orders-pending`, `orders-history-archive`, `account/positions`, `account/balance`) including not-found/aged-out variants. Check the corpus in and hash-stamp it so it is a stable, shared oracle rather than ad-hoc per-test inputs.
   - Add `tests/fixtures/MANIFEST.sha256` plus a verifier test that fails on drift.
   - Corpus must pass the same secret redaction/scan gate used in P2/S4 before commit.
   - Immediate Hotfix synthetic alert test remains allowed before this corpus exists; backfill it to the corpus in this phase.
   - **The same corpus is consumed by P1 (crash-recovery, partial-fill reconciliation), P3 (`clOrdId` idempotency, `Decimal` math), P5 (factory↔legacy shadow comparison), and P7 (mock-OKX component tests)** — no later phase invents its own inputs.
5. **Characterization tests** (lock current behavior before touching it): replay the fixture corpus through `process_alert`/`apply_paper_trading` and snapshot the resulting ledger rows + paper state. These are the regression oracle for P1/P3.
6. Add a global **kill switch**: a single env/flag (`HERMX_SUBMIT_ENABLED=false`) checked at the top of `execute_okx_if_enabled` that hard-blocks all order submission regardless of config. Document it in `skills/emergency-stop.md`.
7. Write a `make`/script target to run the receiver + dashboard against the demo profile locally for manual smoke testing.

**Risk:** Minimal (no runtime behavior change except the new, default-safe kill switch).

**Rollback:** Revert commit; kill switch defaults to "allow" only if explicitly set, so its absence is inert.

**Acceptance criteria**
- [ ] Repo under git; baseline tagged `v0-baseline`; secret scan clean.
- [ ] Fixture corpus committed under `tests/fixtures/` (per-strategy alerts incl. malformed/duplicate, matching strategy files, executor outputs incl. partial-fill + timeout, reconciliation REST fixtures incl. not-found/aged-out, seed state/journal), hash-stamped and referenced by the characterization suite.
- [ ] `tests/fixtures/MANIFEST.sha256` verifier test exists and fails on fixture drift; corpus passes secret scan.
- [ ] `pytest` runs green with ≥1 characterization test covering a buy→sell paper round-trip and ≥1 covering strategy-alert matching.
- [ ] Toggling `HERMX_SUBMIT_ENABLED=false` provably prevents any OKX subprocess invocation (asserted by test + manual log check).
- [ ] Demo profile boots both services via the documented script.

---

### Phase 1 — Critical Bug Fixes (money-safety)

**Goal:** Eliminate silent position loss and untracked exchange exposure. Fixes E1, E2, E5 (and E4 as enabler).

**Tasks**
1. **Durable, append-only position journal (E1, E5).** Replace the "load whole blob → mutate → overwrite" pattern with an event-sourced ledger: every state transition (OPEN/CLOSE/ADJUST) is appended *before* it is acted on, then the in-memory snapshot is derived from the journal. On startup, rebuild state by replaying the journal. Keep `paper-state.json` only as a cache/snapshot, never the source of truth.
   - Corrupt/truncated snapshot must **rebuild from journal**, not silently reset to empty (kills E5).
   - Use strict write-ahead ordering for submissions: persist (`fsync`) the `PLANNED`/`SUBMITTED` journal record (intent + ids) **before** invoking the order submission call, so restart reconciliation has authoritative keys.
   - Add `schema_version` to each journal record and define replay compatibility rules (forward/backward handling + migration path) before introducing new record shapes (e.g., `Decimal` serialization, idempotency metadata).
2. **Atomic, durable writes (E4).** Wrap all ledger appends with `flush()` + `os.fsync()`; on startup, detect and quarantine a trailing partial line rather than crashing the reader.
3. **Expose a queryable OKX interface used by reconciliation.** Implement a supported query path (either importable client module or explicit CLI verbs) for `trade/order`, `orders-pending`, `orders-history-archive`, `account/positions`, and `account/balance` so reconciliation does not depend on parsing `execute` stdout.
4. **Exchange reconciliation on startup and post-submit (E2).** Reconciliation is an explicit contract, not an ad-hoc call:
   - **Endpoints (OKX v5, via the existing HMAC signer in `okx_demo_executor.py`):** order status — `GET /api/v5/trade/order` with required `instId` + (`ordId` preferred, `clOrdId` fallback); if absent there, fallback to `GET /api/v5/trade/orders-pending`, then `GET /api/v5/trade/orders-history-archive` if the order has aged out of the live set. Position truth — `GET /api/v5/account/positions`; balance — `GET /api/v5/account/balance`.
   - **Checks:** (a) order `state` ∈ {`live`, `partially_filled`, `filled`, `canceled`}; (b) `accFillSz`/`avgPx` reconciled against the intended `target_notional_usd` using the instrument contract multiplier / unit conversion; (c) net `pos`/`posSide` from `account/positions` against the journal-derived expected position for the symbol with explicit `posMode` mapping and tolerance bands.
   - **Partial-fill mapping:** `state=partially_filled` (or `0 < accFillSz < ordered`) → outcome `FILLED` with `partial=true`, and the journal records the **actual** `accFillSz`/`avgPx`, never the intended size. `accFillSz=0` with a terminal `state` (`canceled`) → `REJECTED`. A non-terminal `state` past the polling deadline → `UNKNOWN`.
   - **Not-found mapping:** if submit failed before acknowledgement and order lookup returns not-found (`order does not exist`) → `REJECTED`; if submission path indicated possible send but lookup chain is inconclusive → `UNKNOWN`.
   - **Retry bounds:** poll order status with bounded exponential backoff — **max 5 attempts**, base 500 ms, cap ~8 s, total wall-clock ≤ ~20 s. When the bound is exhausted the outcome is `UNKNOWN` and `RECONCILE_MISMATCH` is emitted. No unbounded polling, and the *submission* itself is never silently retried.
   - **Post-submit:** after `okx_demo_executor execute`, always reconcile by `clOrdId` per the above and record the *confirmed* fill — never trust stdout alone.
   - **Startup:** a reconciliation pass compares OKX `account/positions` against the local journal and emits a loud `RECONCILE_MISMATCH` alert if they diverge (does not auto-trade; operator decides). New submission remains disarmed until startup reconcile completes.
5. **Define a "submission outcome" state machine:** `PLANNED → SUBMITTED → (FILLED | REJECTED | UNKNOWN)`. `UNKNOWN` (timeout/crash) is a first-class state that triggers reconciliation rather than being treated as failure.
6. **UNKNOWN resolver + operator controls.** Add a periodic resolver loop that re-reconciles all non-terminal `UNKNOWN`/`SUBMITTED` records until terminal or timeout budget expiry. Add an explicit per-symbol pause artifact (persisted in control state) and a concrete alert transport (not just JSONL) for `RECONCILE_MISMATCH` / queue saturation / auth failures.
7. **Journal lifecycle (compaction/checkpoint policy).** Add periodic verified checkpoints so startup replay cost stays bounded and disk usage does not grow without limit:
   - Write a checkpoint snapshot (`paper-state` cache + last applied journal offset/hash), fsync it, verify replay equivalence, then rotate journal segments.
   - Startup replay begins from the latest verified checkpoint, then replays only newer segments.
   - Define retention/rotation policy and disk-full behavior (fail closed to no-submit, emit operator alert).

**Risk:** HIGH — touches the core money path. Mitigations: all work gated behind the P0 kill switch and characterization tests; ship reconciliation in **observe-only** mode first (log mismatches, take no action) for a soak period, then enable enforcement.

**Rollback:** Feature-flag the journal (`HERMX_STATE_BACKEND=journal|legacy`). The journal and the legacy `paper-state.json` snapshot are written in parallel during the soak, but they are **not** symmetric sources of truth — under `journal` mode the journal is authoritative and the snapshot may lag by one transition. So rollback is an explicit, ordered procedure, not a bare flag flip:
1. Set `HERMX_SUBMIT_ENABLED=false` to halt submission.
2. **Rebuild the legacy blob from the journal**: replay the journal to the last applied transition using the *same* replay routine that derives the in-memory snapshot at startup, then write the result through the legacy writer. The legacy blob is never trusted as-is on rollback — it is regenerated.
3. Verify the rebuilt blob matches the journal-derived snapshot (open-position set + counts), then set `HERMX_STATE_BACKEND=legacy` and re-enable submission.

This resolves the apparent contradiction between "the journal is the only source of truth" and "revert to the legacy blob": rollback first **rebuilds** legacy state from the journal, and the journal is retained afterward for forensic replay.

**Acceptance criteria**
- [ ] Kill -9 the worker mid-trade in a test; on restart the open position is recovered from the journal and a subsequent close executes correctly (automated crash-recovery test).
- [ ] Injected truncated ledger line → reader quarantines it and continues; no unhandled exception.
- [ ] Forced subprocess timeout → outcome recorded as `UNKNOWN`, reconciliation runs, `RECONCILE_MISMATCH` emitted when local≠exchange.
- [ ] Startup reconciliation runs against OKX demo and reports a clean match for a known-flat account.
- [ ] Reconciliation query interface is available independently of `execute` and integration-tested against fixture responses for success/partial/not-found/aged-out paths.
- [ ] `UNKNOWN` records are periodically re-reconciled and either converge to terminal state or remain explicitly tracked with alerting and per-symbol pause.
- [ ] Checkpoint + journal-segment rotation keeps replay bounded (startup replay time no longer grows linearly with full history) and preserves exact state equivalence.
- [ ] All P0 characterization tests still pass (no PnL/ledger regressions).

---

### Phase 2 — Security Hardening

**Goal:** Close the externally reachable auth gaps. Fixes S1–S5.

**Tasks**
1. **Constant-time secret check + drop query-string auth + fail closed (S1).** Use `hmac.compare_digest`; accept the secret only via header, never `?secret=`. Reject (and don't log the body of) unauthenticated requests *before* heavy parsing. **Fail closed on missing config:** if the webhook secret env/config is unset, empty, or whitespace-only, the receiver must refuse to accept alerts — either abort startup with a fatal log line or reject every request with `401` — and must **never** fall back to an "auth disabled / accept-all" path. The same rule applies to the HMAC signing key once HMAC is required (next task): a missing/blank key fails closed, it does not silently disable verification.
2. **HMAC body signature + replay window (S2).** Adopt the standard pattern: TradingView (or the alert relay) signs `timestamp + body` with a shared key; receiver verifies `compare_digest` and rejects timestamps outside a ±N-second window. Combine with the existing dedupe for idempotency. (If TradingView can't sign, terminate at a thin signing relay/Cloudflare Worker and document that boundary.)
   - Define clock authority and skew handling: the relay/receiver timestamp is canonical, hosts run NTP, and the allowed skew window is explicit/configurable (with tests at boundary and just-outside-boundary values).
   - Add explicit relay delivery tasks: authenticated ingress, key storage/rotation, health checks, and failure behavior (`fail-closed` at receiver when relay signature is absent/invalid).
3. **Bound the queue + basic rate limiting (S3).** Cap `PROCESS_QUEUE` (`maxsize`), return `503` when full, and add a per-source request rate limit. Behind Cloudflare Tunnel, rate-limit by authenticated key id or `CF-Connecting-IP` (not tunnel source IP). Read `Content-Length` with a hard max body size before buffering.
   - **Sequencing note (maxsize stays permissive until P3 lands):** in P2 the queue is still drained by the **single serial worker** — head-of-line blocking is not removed until **P3**. A tight `maxsize` here would shed *legitimate* alerts during a transient backlog (one slow 45 s OKX call can back up four strategies firing on the same bar close). So ship P2 with a **deliberately generous `maxsize`**, sized to absorb a worst-case serial drain rather than to throttle, and rely on the **hard body-size cap + per-source rate limit** as the real DoS bound. **Tighten `maxsize` to its true enforcing value only after P3's bounded per-symbol worker pool lands**, when a full queue signals genuine overload rather than single-worker latency. Until then the bound is a safety ceiling, not an alert-dropping floor (matches the `queue maxsize default permissive` rollback default below).
   - Request-path order is explicit: `(1) Content-Length/max-body check -> 413`, `(2) rate-limit`, `(3) auth/HMAC verify`, `(4) enqueue or 503`. Every `503` emits an operator alert because it represents a dropped trading signal.
4. **Subprocess least-privilege (S4).** Pass only the explicit vars the executor needs (`env={...}`), not `os.environ.copy()`. Redact/scrub captured `stdout/stderr` before writing to the ledger (strip anything matching key/secret patterns).
5. **Dashboard auth (S5).** Require a token (or HTTP basic) on `dashboard.py` and `/api`; never serve PnL/positions unauthenticated even on localhost-behind-proxy. Keep the localhost bind. Fail closed when dashboard auth token/config is missing.
6. **Execution gate precedence table.** Document and implement explicit precedence: live submission requires **all** gates affirmative (`HERMX_SUBMIT_ENABLED=true` AND `execution.submit_orders=true` AND `risk.allow_live_execution=true` AND auth healthy). Any unset/ambiguous state defaults to dry-run/no-submit.
7. **Secrets hygiene (S6).** Document a rotation procedure in `setup/`; confirm `.env` is git-ignored (it is) and add a pre-commit secret scan.

**Risk:** MEDIUM. The HMAC change requires coordinating the alert sender; ship header-based constant-time auth + replay window first (no sender change), then layer HMAC once the relay is ready. Keep the old secret path behind a deprecation flag for one release.

**Rollback:** Each control is independently flag-gated (`HERMX_REQUIRE_HMAC`, `HERMX_DASH_AUTH`, queue maxsize default permissive). Disable individually without redeploying the others.

**Acceptance criteria**
- [ ] Timing-attack test shows constant-time comparison; query-string secret is rejected.
- [ ] With the webhook secret unset/empty/whitespace, the receiver fails closed (refuses startup or rejects every request with `401`); it never accepts an unauthenticated alert.
- [ ] Replayed request outside the time window is rejected; within-window duplicate is deduped.
- [ ] Replay-window checks are stable under expected clock skew: in-window signed requests pass, just-outside-window requests fail, and skew threshold is explicitly configured/documented.
- [ ] Signing relay (if used) is deployed with authenticated ingress + key-rotation procedure + health checks, and receiver fails closed on missing/invalid relay signature.
- [ ] Queue saturation returns `503`, memory stays bounded under a flood test.
- [ ] Queue `maxsize` is configured from measured burst/serial-latency capacity (`peak arrival × worst-case serial drain`) and only tightened after P3 concurrency rollout.
- [ ] Executor subprocess env contains only whitelisted keys (asserted by test); ledger entries contain no secret-shaped strings.
- [ ] Unauthenticated dashboard/`/api` request returns `401`.
- [ ] Execution gate-precedence matrix is documented and covered by tests (any unset/ambiguous gate => no-submit).

---

### Phase 3 — Execution Engine Stabilization

**Goal:** Make the engine robust under load and partial failure. Fixes E3, E6, E7, E8; builds on P1's journal.

**Tasks**
1. **Decouple slow I/O from the queue (E3).** Move the OKX submission off the single serial worker: either a small bounded worker pool keyed by symbol (so symbols don't block each other while preserving per-symbol ordering) or an async submission step with status polling. Per-symbol serialization preserves correctness; cross-symbol parallelism removes head-of-line blocking.
2. **Tighten subprocess handling (E3).** Explicitly catch `TimeoutExpired`, hard-kill and reap the timed-out child process, mark `UNKNOWN`, and ensure a timed-out submission is reconciled (P1) rather than silently retried. Make timeouts configurable per call.
3. **Idempotent submission (E7).** A deterministic `client_order_id` is *already* generated and passed to OKX as `clOrdId` (`:1980`) — the gap is **stability and semantics of the value**, not wiring it through.
   - Generate a stable **32-char alphanumeric** `clOrdId` from durable signal identity plus semantic order role (e.g. `strategy_id + asset + signal/bar timestamp + role{close|open|reduce}`), with no wall-clock entropy and no positional counters. Same signal+role => same id across retries/restarts; distinct roles => distinct ids.
   - Validate id constraints pre-submit (≤32 chars, alphanumeric) and reject invalid ids before API call.
   - Treat durable journal dedupe as the **primary** idempotency guard (`check-before-submit`); treat exchange-side `clOrdId` dedupe as secondary in-flight protection only.
   - Persist dedupe in the durable journal (not a 5000-cap file) and key replay protection on signal identity + time window (ties into S2).
4. **Money math (E6).** Introduce `Decimal` for notional/fee/PnL in the paper engine and any value compared against exchange fills; centralize rounding rules.
   - Serialize `Decimal` as canonical strings in ledgers/journal to avoid float reintroduction on round-trip.
   - Re-baseline characterization snapshots for money fields at this phase using an explicit tolerance/normalization rule; structural/event-order assertions from P0 remain strict.
5. **Concurrency-safe state (E8).** With a worker pool now real, add the locking/atomic-state discipline that was previously only latent: per-symbol locks around journal-derived state, atomic snapshot writes with unique temp names. (This is now load-bearing, not theoretical.)
6. **Active liveness/watchdog controls.** Add worker heartbeat + queue-lag watchdog + resolver heartbeat with explicit alerting/auto-pause behavior:
   - If worker heartbeat or resolver heartbeat is stale past threshold, raise operator alert and pause submission.
   - If queue lag breaches SLO, emit alert and mark system degraded.

**Risk:** HIGH — concurrency + order idempotency. Mitigation: introduce the pool with `maxsize=1` (behaviorally identical to today) behind a flag, prove correctness, then raise concurrency. Idempotency verified against OKX demo by deliberately double-submitting.

**Rollback:** `HERMX_WORKER_POOL_SIZE=1` reverts to serial behavior; `clOrdId` idempotency is safe to keep on regardless.

**Acceptance criteria**
- [ ] Burst of 4 simultaneous alerts (4 symbols) processes concurrently; one stalled symbol does not delay the others (latency test).
- [ ] The same signal+role regenerates a stable 32-char alphanumeric `clOrdId` across process restarts (unit test), while close/open roles for a reversal produce distinct ids.
- [ ] Replay-after-fill is blocked by durable journal dedupe even when exchange-side `clOrdId` dedupe no longer applies (integration test).
- [ ] PnL computed with `Decimal` matches a hand-checked fixture to the cent.
- [ ] Characterization suite policy is explicit: money-value snapshots are tolerance-normalized/re-baselined at P3; non-money structural assertions remain strict and unchanged.
- [ ] Per-symbol ordering preserved under load (open-before-close invariant holds in a fuzz test).
- [ ] Heartbeat/watchdog tests prove stale worker/resolver states trigger alert + submission pause, and recovery clears degraded state safely.
- [ ] All P0/P1 tests still green.

---

### Phase 4 — Dashboard Reliability

**Goal:** Dashboard is trustworthy and strategy-driven. Fixes D1–D8.

**Tasks**
1. **Safe, bounded ledger reads (D1, D2).** Replace whole-file read+slice with tail-reading (seek from end / reverse-read N lines); count and surface skipped/corrupt lines instead of hiding them. Read snapshot copies or tolerate a truncated final line gracefully (the writer's fsync from P1 helps).
2. **Surface executor failures (D3).** When the OKX subprocess returns non-JSON/non-zero, render an explicit "EXECUTOR ERROR / data stale" banner — never a silent flat view.
3. **Dynamic, strategy-file-driven cards (D5).** Remove hardcoded `SYMBOLS` and the sort map; iterate `load_strategy_files()` and render one card per active strategy, honoring `status`/`execution_mode`. This realizes the `ARCHITECTURE.md` requirement.
4. **Fix or remove dead policy path (D4).** Either populate `POLICIES` from config or delete the legacy policy tabs and the empty `/api` policy payload; don't ship a silently-empty feature.
5. **Datetime + freshness (D6, D7).** Make `parse_dt` tolerate naive timestamps (assume UTC), make the display timezone configurable, and label the "Updated" timestamp with true data age + a stale indicator when the cache is older than the refresh interval.
6. **De-duplicate shared logic (D8).** Extract `canonical_timeframe` (and other shared parsing) into a single module imported by both receiver and dashboard.

**Risk:** LOW–MEDIUM (read-only consumer; no order path). Main risk is display regressions — covered by snapshot tests of rendered model/`/api`.

**Rollback:** Dashboard is a separate process; revert its commit independently of the receiver.

**Acceptance criteria**
- [ ] Corrupt/huge ledger → dashboard renders within budget, shows "N lines skipped," no OOM (load test with a synthetic large ledger).
- [ ] Executor crash → visible error banner, not a flat view.
- [ ] Adding a new `strategies/*.json` with `status: active_demo` makes a new card appear with **no code change**; disabling one removes it.
- [ ] `/api` contains no silently-empty contractual fields.
- [ ] Naive-timestamp alert renders a real time; stale cache shows a stale badge.

---

### Phase 5 — Executor Architecture Consolidation

**Goal:** One execution path, one abstraction, no dead code. Fixes X1–X4. **Recommendation: activate the existing `src/executors/` factory (composition over the proven OKX script); do not rewrite the OKX REST client.**

**Tasks**
1. **Delete the dead system (X1).** Remove `src/executors/base_executor.py` and the orphaned `src/kucoin_paper_executor.py`; or, if KuCoin paper is wanted, repoint it to `src/executors/base.BaseExecutor`.
2. **Wire the factory in (X2).** Replace the hardcoded subprocess call in `webhook_receiver.py:2024-2085` and the dashboard's direct OKX calls (`dashboard.py:442,507`) with `ExecutorFactory.create(CONFIG, ROOT)`. The OKX adapter keeps shelling out to the proven `okx_demo_executor.py` — no reimplementation.
3. **Formalize the contract (X4).** Document the `readiness`/instruction schema and the normalized result envelope on `BaseExecutor.execute`; unify `inst_id` vs `okx_inst_id` naming behind one accessor.
4. **Keep the OKX REST client as-is (X3)** but expose it only through the adapter, so a future Bybit/Binance adapter is ~100 lines (compose another CLI/REST client) rather than a fork of the engine.

**Risk:** MEDIUM — the factory becomes the live order path. Mitigation: behind `HERMX_EXECUTOR_BACKEND=factory|legacy`; run factory in shadow (plan-only, compare its **normalized** readiness/result against the legacy direct call) before switching live submission to it.

**Rollback:** Flip `HERMX_EXECUTOR_BACKEND=legacy`.

**Acceptance criteria**
- [ ] `src/executors/base_executor.py` removed; no remaining importers; `grep` clean.
- [ ] Both receiver and dashboard obtain executors only via `ExecutorFactory`; no hardcoded script paths remain.
- [ ] Factory path produces **normalized-equivalent** readiness and equivalent fills vs legacy in shadow comparison over a soak window — compared after canonicalization (sorted keys, normalized number formatting / `Decimal` quantization, whitespace) and after dropping legitimately non-deterministic fields (timestamps, `clOrdId` nonces, request ids), **not** by byte-for-byte diff. Any residual non-normalized difference is investigated and resolved before cutover.
- [ ] A stub `bybit` adapter can be registered and selected via config without touching receiver/dashboard code (proof-of-extensibility test).

---

### Phase 6 — Schema Modernization (Exchange-Agnostic)

**Goal:** Strategies and alerts stop hardcoding OKX. Fixes M1–M3. Depends on P5's adapter contract.

**Tasks**
1. **Strategy schema v2 (M1).** Introduce `schema_version: 2`: replace `okx_inst_id` with a generic `instrument` block (e.g. `{ "exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap" }`), replace `okx_submit_orders` with `submit_orders`, relax the `asset`/quote regex beyond `*USDT`. Keep a **v1→v2 loader shim** so existing `strategies/*.json` keep working until migrated.
2. **Alert schema (M2).** Widen the `exchange` enum; keep `okx` valid. Validate alerts against the schema at intake (currently the schema file exists but enforcement should be explicit).
3. **Generic instruction wire format (M3).** Make the readiness/instruction the exchange-agnostic shape from `ARCHITECTURE.md` (`strategy_id, asset, target_side, target_notional_usd, margin_mode, leverage`); let the adapter translate to OKX `inst_id`/`td_mode`. OKX-specific fields move *inside* the OKX adapter.
4. **Migrate the four live strategy files** to v2 in a single reviewed change once the shim is proven.

**Risk:** MEDIUM — schema changes can break matching/validation. Mitigation: dual-version loader, validate both schemas in CI, migrate strategy files last and one at a time on the demo account.

**Rollback:** Loader accepts v1 and v2 simultaneously; revert individual strategy files to v1 if needed.

**Acceptance criteria**
- [ ] v1 and v2 strategy files both load and execute correctly (parametrized test over both).
- [ ] A non-OKX (e.g. stub Bybit) strategy validates and routes to the right adapter end-to-end in dry-run.
- [ ] Alerts are schema-validated at intake; invalid alerts are quarantined (per existing `quarantine_invalid_strategy_alerts` config).
- [ ] All four production strategies migrated to v2 with identical resulting orders vs v1 (diff test).

---

### Phase 7 — Test Strategy & CI Hardening (ongoing, lands incrementally each phase)

**Goal:** Make every above guarantee continuously enforced.

**Test pyramid**
- **Unit:** PnL/fee/`Decimal` math, `canonical_timeframe`, schema validation, dedupe/idempotency keys, HMAC/replay logic, executor result normalization.
- **Component:** `process_alert` → journal → readiness, with a **mock OKX adapter** (no network) asserting orders/ledger/state.
- **Crash/recovery:** kill-mid-trade, truncated ledger, reconciliation mismatch (from P1/P3).
- **Contract:** strategy + alert JSON schema validation in CI; `scripts/validate_package.py` folded into the suite.
- **Integration (gated, manual/scheduled):** against OKX **demo** only, behind a CI flag and the kill switch, asserting idempotency and reconciliation. Never runs against live keys in CI.
- **Dashboard snapshot:** rendered model/`/api` golden files.

**CI pipeline**
1. Lint + format + type-check (add `ruff`/`mypy` incrementally; the two god files can be excluded initially and tightened as they're split).
2. `pytest` unit + component + crash suites on every push; coverage gate that ratchets upward (start at the level achieved after P0–P1, never decreases).
3. Schema validation job.
4. Secret-scan (pre-commit + CI).
5. Demo-integration job: opt-in, scheduled, runs only against **OKX demo**.
   - **Demo-credential handling:** credentials are stored as masked CI secrets scoped to this job only and **not exposed to PR builds from forks**; they are demo/sandbox keys with no withdrawal scope and are *never* the live keys. The job aborts up front if it detects a non-demo endpoint/profile, and logs are scrubbed of key-shaped strings (reuses the P2/S4 redaction) so nothing leaks into CI output.
   - **Kill-switch assertion:** the job first runs with `HERMX_SUBMIT_ENABLED=false` and asserts **zero** OKX submissions occur (proving the global stop holds in CI), then re-runs the idempotency/reconciliation checks with submission armed against demo only. A failed kill-switch assertion fails the whole job.

**Acceptance criteria**
- [ ] CI green required to merge; coverage gate enforced and monotonic.
- [ ] Mock-OKX component tests cover open/close/reverse/reject/timeout paths.
- [ ] Secret scan blocks commits containing key-shaped strings.
- [ ] Demo-integration job runs on schedule and reports idempotency + reconciliation status.
- [ ] Demo-integration job uses masked **demo-only** credentials (asserted non-live, fork PRs excluded) and asserts the kill switch (`HERMX_SUBMIT_ENABLED=false` → zero submissions) before any armed test runs.

---

## 3. Cross-Phase Migration & Rollback Strategy

**Principles**
- **Everything risky is flag-gated**, default to current behavior, flip per-environment. Master switches: `HERMX_SUBMIT_ENABLED`, `HERMX_STATE_BACKEND`, `HERMX_REQUIRE_HMAC`, `HERMX_DASH_AUTH`, `HERMX_WORKER_POOL_SIZE`, `HERMX_EXECUTOR_BACKEND`, strategy `schema_version`.
- **Soak in shadow before switching.** New state backend, new executor backend, and reconciliation each run in parallel/observe-only against the demo account before they take authority.
- **Forward-compatible data.** Journal + legacy snapshot written together during P1 soak; v1+v2 schemas loadable together during P6. No destructive migration until the new path is proven.
- **Gate precedence is explicit and test-backed.** Any gate unset/ambiguous => no-submit/dry-run; no implicit live arming.

**Deployment cadence (live-ish bot)**
1. Deploy to demo VPS, enable new flag in observe/shadow mode.
2. Soak ≥ a meaningful number of real alert cycles (cover all four strategies firing).
3. Compare shadow vs legacy outputs; investigate any diff.
4. Flip the flag to enforce; keep the old path one release for instant rollback.
5. Remove the legacy path only after a clean release with no rollbacks.

**Rollback playbook**
- Any phase: `git revert` the phase commit (enabled by P0).
- Order path regression: set `HERMX_SUBMIT_ENABLED=false` (instant global stop), then revert the offending flag.
- State corruption suspicion: halt submission, **rebuild the legacy blob from the journal** (Phase 1 rollback procedure), verify it, then switch `HERMX_STATE_BACKEND=legacy`; the journal remains for forensic replay.
- Reconciliation mismatch in production: bot does **not** auto-correct — it alerts and pauses submission for the affected symbol; operator resolves manually (documented in `skills/emergency-stop.md` / `HEALTH_AND_RECOVERY.md`).

---

## 4. Risk Register (top items)

| Risk | Phase | Likelihood | Impact | Mitigation |
|------|-------|-----------|--------|-----------|
| Refactor introduces a money bug in the order path | 1,3,5 | Med | Critical | Kill switch + characterization tests + shadow soak + flag rollback |
| Concurrency change causes double orders | 3 | Med | High | `clOrdId` idempotency at exchange; pool size starts at 1; demo double-submit test |
| HMAC rollout breaks alert intake (sender can't sign) | 2 | Med | High | Ship constant-time header auth + replay window first; HMAC via relay later, flag-gated |
| Signing relay outage/misconfiguration blocks alert verification | 2 | Med | High | Relay health checks + authenticated ingress + key rotation + receiver fail-closed behavior with runbook |
| Schema migration breaks strategy matching | 6 | Med | Med | Dual-version loader; migrate files last, one at a time, diff-tested |
| Reconciliation false-positives spook the operator | 1 | Med | Med | Observe-only first; tune before enforcing; clear mismatch reporting |
| Dashboard refactor hides a real state | 4 | Low | Med | Snapshot tests; explicit error banners replace silent empties |
| No git history → lost work / unclear blame | 0 | High (today) | Med | Phase 0 establishes git before any edits |

---

## 5. Sequencing Summary (the one-paragraph version)

Put the project under **git and tests first (P0)** so nothing else is done blind. Then fix the only bugs that can **lose money silently** — non-durable position state and the absence of exchange reconciliation (**P1**). Close the **externally exploitable auth gaps (P2)** since the bot is reachable through the tunnel right now. With state durable and access controlled, **stabilize the engine (P3)**: remove head-of-line blocking, make submission idempotent at the exchange, and use `Decimal` for money. Make the **dashboard trustworthy and strategy-driven (P4)**. Only then do the **architecture cleanups (P5 executor consolidation, P6 exchange-agnostic schemas)** that the earlier phases made safe to perform. **CI/test hardening (P7)** lands continuously throughout, ratcheting coverage up and gating every merge. Every behavior change is flag-gated, soaked in shadow on the demo account, and revertible in one step.
