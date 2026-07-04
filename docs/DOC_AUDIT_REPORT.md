# Documentation Audit Report
Generated: 2026-07-02

> Method: ground truth extracted directly from source (`src/`, `scripts/`, `skills/`,
> `docker-compose*.yml`) and cross-referenced against every operator-facing doc
> (`README.md`, `ARCHITECTURE.md`, `INSTALL.md`, `docs/*.md`, `skills/**/*.md`).
> Every `HERMX_*` literal, HTTP route, slash command, script verb, compose service, and
> config key was enumerated by grep against code, then checked for a documenting mention.
> This audit deliberately over-reports: an element is flagged unless it is explicitly
> documented for operators.

## Summary
- **Total code elements discovered: ~150**
  - Slash commands: 11 · HTTP endpoints: 6 (11 counting method/path variants) · `HERMX_*` env vars in code: 57 · Other operational env vars: ~8 · Config keys (engine-config/control-state/strategy): ~35 · Operator scripts: 10 · Compose services: 3 · Compose volumes/mounts: 7
- **Fully documented (PRESENT): ~95**
- **Missing from docs (MISSING): ~39**
  - 2 slash commands absent from `README.md` · 17 `HERMX_*` env vars in no doc · 6 scripts undocumented · several config keys & compose volumes unexplained
- **Stale / partial: ~24**
  - 10 documented env vars that no longer exist in code · ~8 documented "endpoints" that are not real HTTP routes · the "FastAPI" transport claim · a retired `monitor_daemon.py` · dead `shadow-config.json` · `ARCHITECTURE.md` endpoint list omits the entire dashboard/close API

### Headline findings
1. **`README.md` omits `/hx-exchange` and `/hx-tv-alerts`** — the user's original complaint, plus a second missing command. README lists 10 of the 11 slash commands.
2. **`ARCHITECTURE.md` documents only `/health`, `/latest`, `/webhook`** — it omits the entire dashboard API (`/api/signals`, `/api/control/strategy`) and the operator close endpoint (`/api/close`).
3. **17 `HERMX_*` env vars are consumed by code but appear in no doc** — mostly durability/replay, journal-rotation, dashboard-TZ, and dev/CI knobs.
4. **10 `HERMX_*` env vars are documented but do not exist in code** — stale references (`HERMX_EXEC_API`, `HERMX_EXEC_SHADOW`, `HERMX_CRON_*`, etc.).
5. **The transport layer is stdlib `http.server`, not FastAPI** — `CLAUDE.md` / `.claude/CLAUDE.md` (and README's summary) call it a "FastAPI webhook receiver"; there is no `fastapi`/`uvicorn` import anywhere in `src/`.
6. **6 operator scripts are undocumented in `INSTALL.md`** (`docker-rebuild.sh`, `docker-update.sh`, `security-audit.py`, `smoke_run.sh`, `start_dashboard.ps1`, `start_webhook.ps1`).

---

## Missing Elements (by category)

### Slash Commands
Canonical set = 11 (`skills/hermx-help/SKILL.md` self-asserts "eleven commands"). All 11 exist in code + are documented in `docs/hermx-slash-commands.md`. The gap is **`README.md` coverage** (the operator entry point).

| Command | Where it exists | Where documented | Status |
|---|---|---|---|
| `/hx-status` | skills/hermx-status | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-positions` | skills/hermx-positions | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-strategy-list` | skills/hermx-strategy-list | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-strategy-mode` | skills/hermx-strategy-mode | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-trace` | skills/hermx-trace | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-close` | skills/hermx-close | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-emergency-stop` | skills/emergency-stop.md | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-restart` | skills/hermx-restart | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-upgrade` | skills/hermx-upgrade | README, docs/hermx-slash-commands.md | PRESENT |
| `/hx-help` | skills/hermx-help | README, docs/hermx-slash-commands.md | PRESENT |
| **`/hx-exchange`** | skills/hermx-exchange (+ scripts/exchange.sh) | docs/hermx-slash-commands.md, ARCHITECTURE.md — **NOT in README** | **MISSING (from README)** |
| **`/hx-tv-alerts`** | skills/hermx-tv-alerts | docs/hermx-slash-commands.md — **NOT in README** | **MISSING (from README)** |

### Env Vars
Ground truth = 57 distinct `HERMX_*` literals read across `src/`, `scripts/`, `skills/hermx-ops/lib/`. Below are only the **MISSING** ones (consumed by code, documented nowhere). "Used in" cites the primary reader.

| Var | Used in | Where documented | Status |
|---|---|---|---|
| `HERMX_REPLAY_ENABLED` | webhook_receiver.py:116 | — | MISSING |
| `HERMX_REPLAY_LOOKBACK_SECONDS` | webhook_receiver.py:117 | — | MISSING |
| `HERMX_REPLAY_MAX_TV_AGE_SECONDS` | webhook_receiver.py:118 | — | MISSING |
| `HERMX_REQUEST_TIMEOUT_SECONDS` | webhook_receiver.py:125 | — | MISSING |
| `HERMX_QUEUE_SATURATION_ALERT_DEPTH` | webhook_receiver.py:265 | — | MISSING |
| `HERMX_PLANNED_ORDER_TIMEOUT_SECONDS` | webhook_receiver.py:262 | — | MISSING |
| `HERMX_UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK` | webhook_receiver.py:257 | — | MISSING (sibling resolver vars ARE documented) |
| `HERMX_JOURNAL_SEGMENT_MAX_RECORDS` | webhook_receiver.py:157 | — | MISSING |
| `HERMX_JOURNAL_SEGMENT_RETENTION` | webhook_receiver.py:161 | — | MISSING |
| `HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS` | webhook_receiver.py:2067 | — | MISSING (`HERMX_ALERT_WEBHOOK_URL` is documented) |
| `HERMX_DASH_REFRESH_SECONDS` | dashboard.py:78 | — | MISSING |
| `HERMX_DASH_TZ` | dashboard_core.py:144 | — | MISSING |
| `HERMX_DASH_TZ_OFFSET_HOURS` | dashboard_core.py:152 | — | MISSING |
| `HERMX_DEV_CORS` | dashboard.py:2334 | — | MISSING (dev-only) |
| `HERMX_HTTP_TIMEOUT` | skills/hermx-ops/lib/hermx_ops.py:32 | — | MISSING |
| `HERMX_LOCAL_TAG` | scripts/docker-rebuild.sh:27 | — | MISSING |
| `HERMX_PUSH_REF` | scripts/docker-rebuild.sh:28 | — | MISSING |

Non-`HERMX_` operational env vars also undocumented: `SHADOW_PORT` (receiver port, default 8891), `CLEAN_DASHBOARD_PORT` (dashboard port, default 8098), `SHADOW_ROOT`, `OKX_FORCE_IPV4` (set by install-docker.sh), `HERMX_SUBMIT_ENABLED` (used **only** in `smoke_run.ps1` as the Windows kill switch — see Stale table for the cross-platform inconsistency).

**Documented-and-present (PRESENT)** env vars (not exhaustively tabled): `HERMX_SECRET`, `HERMX_BIND_HOST`, `HERMX_DATA_DIR`, `HERMX_REQUIRE_HMAC`, `HERMX_WEBHOOK_HMAC_KEY`, `HERMX_REPLAY_WINDOW_SECONDS`, `HERMX_MAX_BODY_BYTES`, `HERMX_RATE_LIMIT_*`, `HERMX_QUEUE_MAXSIZE`, `HERMX_QUEUE_LAG_SLO_SECONDS`, `HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS`, `HERMX_SUBMIT_TIMEOUT_SECONDS`, `HERMX_WORKER_POOL_SIZE`, `HERMX_WATCHDOG_*`, `HERMX_UNKNOWN_RESOLVER_ENABLED/INTERVAL/ORDER_TIMEOUT`, `HERMX_RECONCILE_ENABLED`, `HERMX_LEDGER_ROTATE_*`, `HERMX_ADVISOR_*`, `HERMX_DASH_AUTH`, `HERMX_EXEC_BACKEND`, `HERMX_CCXT_EXCHANGE`, `HERMX_EXCHANGE`, `HERMX_ALERT_WEBHOOK_URL`, `HERMX_LIVE_TRADING`, `HERMX_IMAGE`, `HERMX_INSTALL_DIR`, `HERMX_DASHBOARD_BASE`, `HERMX_RECEIVER_BASE`.

### API Endpoints
Transport is stdlib `http.server` (manual path dispatch), not a framework router. All routes also have a `/shadow`-prefixed alias.

| Endpoint | File | Where documented | Status |
|---|---|---|---|
| `POST /webhook` | webhook_receiver.py:3377 | README, ARCHITECTURE.md, INSTALL.md, docs | PRESENT |
| `GET /health` (receiver + dashboard) | webhook_receiver.py:3299 · dashboard.py:2466 | README, ARCHITECTURE.md, INSTALL.md, docs | PRESENT |
| `GET /latest` | webhook_receiver.py:3301 | ARCHITECTURE.md, docs | PRESENT |
| `POST /api/close` | webhook_receiver.py:3379 | docs/hermx-slash-commands.md only — **absent from ARCHITECTURE.md** | PARTIAL |
| `POST/DELETE /api/control/strategy/{id}` | dashboard.py:2522 / 2546 | docs/DOCKER_PACKAGE_PLAN.md, docs/hermx-slash-commands.md — **absent from ARCHITECTURE.md** | PARTIAL |
| `GET /api/signals` | dashboard.py:2445 | skills/hermx-ops/references/api-contract.md, skills/signal-memory — **absent from README/ARCHITECTURE/INSTALL** | PARTIAL |
| `GET /api` / `/dashboard/api` (full dashboard model) | dashboard.py:2463 | ARCHITECTURE.md mentions `/api` loosely; shape only in skills/hermx-ops api-contract.md | PARTIAL |

**Phantom endpoints (documented but not real HTTP routes):** docs reference `/reconcile`, `/reconcile-alerts`, `/positions`, `/positions/signal`, `/ledger/freshness`, `/webhook/config`, and `/status` as if they were endpoints (ARCHITECTURE.md, MONITORING_GAPS_BRAINSTORM.md, INSTALL.md, hermes-execution.md). The code exposes **no** such routes — the only HTTP routes are the 6 above. These are either JSON fields inside the `/api` payload or aspirational/planned surfaces presented as live. See Stale Documentation. `/api/monitor/alerts` (HERMES_CRON_MONITOR_DESIGN.md) is likewise not a real route.

### Config Keys
From `engine-config.json` (`src/webhook/config.py`, `webhook_receiver.py`) and `control-state.json`.

| Key | File | Where documented | Status |
|---|---|---|---|
| `strategy_engine.enforce_alert_schema` | config.py:62 | docs (ALERT_CONTRACT/others) | PRESENT |
| `strategy_engine.require_strategy_id` | config.py:60 | docs | PRESENT |
| `strategy_engine.allow_strategy_alerts` | config.py:59 | docs | PARTIAL |
| `strategy_engine.strategies_dir` | config.py:57 | docs (1 mention) | PARTIAL |
| `strategy_engine.default_status` (`trial_candidate`) | config.py:58 | — | MISSING |
| `strategy_engine.quarantine_invalid_strategy_alerts` | config.py:61 | — | MISSING |
| `advisor.enabled/command/skills/model/timeout_seconds` | config.py:23-27 | docs (HERMES_AGENT_DESIGN) via env equivalents | PARTIAL |
| `execution.exchange` / `ccxt_exchange` / `ccxt_default_type` / `route` / `account` | config.py:15-19 | EXCHANGE_ADAPTERS.md (partial) | PARTIAL |
| `readiness.close_only` | service.py:139,154 | EXECUTION_GATES.md | PRESENT |
| `readiness.live_execution_enabled` | service.py:100,111 | docs | PRESENT |
| `readiness.simulated_trading` | service.py:49 | docs | PRESENT |
| `control-state.json: strategy_overrides` | webhook_receiver.py:1352 | DASHBOARD_MODEL.md, slash-commands | PRESENT |
| `control-state.json: symbol_pauses` | webhook_receiver.py:1161 | partial (emergency-stop) | PARTIAL |

### Scripts / CLI Tools
`INSTALL.md` documents `install-docker.sh` and `validate_package.py`; `ARCHITECTURE.md`+slash-commands document `exchange.sh`. The rest are undocumented for operators.

| Script | Purpose | Where documented | Status |
|---|---|---|---|
| `scripts/install-docker.sh` | Repo-less installer (`curl \| bash`), seeds `/opt/hermx`, writes `.env` | INSTALL.md, docs/DOCKER_PACKAGE_PLAN.md | PRESENT |
| `scripts/exchange.sh` | Exchange-credential manager (backs `/hx-exchange`); verbs `list/status/add/update/remove` | ARCHITECTURE.md, docs/hermx-slash-commands.md | PRESENT |
| `scripts/validate_package.py` | Release/CI package sanity check | INSTALL.md | PRESENT |
| `scripts/docker-rebuild.sh` | Build image from local source + redeploy (`--push`, `--host`, `--no-cache`, …) | — | MISSING |
| `scripts/docker-update.sh` | Pull latest GHCR image + restart (`--reseed`, `--host`, …) | — | MISSING |
| `scripts/security-audit.py` | Static threat scanner (backs `/security-audit`); `--fail-on`, `--only`, `--fast` | — | MISSING |
| `scripts/smoke_run.sh` | Dry-run launcher (forces `submit_orders=false` + `HERMX_LIVE_TRADING=false`) | — | MISSING |
| `scripts/smoke_run.ps1` | Windows dry-run launcher (uses `HERMX_SUBMIT_ENABLED=false`) | — | MISSING |
| `scripts/start_dashboard.ps1` | Minimal dashboard launcher (Windows) | — | MISSING |
| `scripts/start_webhook.ps1` | Minimal receiver launcher (Windows) | — | MISSING |

### Docker / Compose Elements

| Element | File | Where documented | Status |
|---|---|---|---|
| `receiver` service | docker-compose.yml / .host.yml | INSTALL.md, DOCKER_PACKAGE_PLAN.md | PRESENT |
| `dashboard` service | docker-compose.yml / .host.yml | INSTALL.md, DOCKER_PACKAGE_PLAN.md | PRESENT |
| `tailscale` service (compose.yml only) | docker-compose.yml:64 | DOCKER_PACKAGE_PLAN.md (partial) | PARTIAL |
| Volume `hermx-state` → `/app/data` (rw) | both compose | DASHBOARD_MODEL.md notes writable-mount requirement | PRESENT |
| Volume `hermx-data` → `/app/logs` | both compose | partial | PARTIAL |
| Bind `./engine-config.json:ro`, `./strategies` (ro in compose.yml, **rw** in host.yml receiver) | both compose | DOCKER_PACKAGE_PLAN.md | PARTIAL (ro/rw divergence undocumented) |
| Volume `tailscale-state`, bind `config/tailscale/serve.json` | docker-compose.yml only | — | MISSING |
| `docker-compose.host.yml` (`network_mode: host`, no hardening, `.env` bind) | docker-compose.host.yml | mentioned; hardening-difference vs compose.yml undocumented | PARTIAL |
| Compose env `HERMX_BIND_HOST=0.0.0.0` (compose.yml, not host.yml) | docker-compose.yml:17,39 | — | MISSING |
| `dashboard` hardening (`read_only: true`, `cap_drop: ALL`, `tmpfs`) | docker-compose.yml:33-62 | — | MISSING |

---

## Stale Documentation

| Doc | Element | Current code behavior | What doc says | Severity |
|---|---|---|---|---|
| CLAUDE.md, .claude/CLAUDE.md, README summary | Transport framework | Both services use stdlib `http.server` (`ThreadingHTTPServer` + `BaseHTTPRequestHandler`); no `fastapi`/`uvicorn` import exists in `src/` | "FastAPI webhook receiver" | **High** (misleads any contributor expecting FastAPI routing/middleware/deps) |
| docs/* (env tables) | `HERMX_EXEC_API` | Not read anywhere in code | Documented as an execution knob | Medium |
| docs/* | `HERMX_EXEC_SHADOW` | Not in code | Documented | Medium |
| docs/* | `HERMX_EXEC_WRITE_BACKEND` | Not in code | Documented | Medium |
| docs/* | `HERMX_ENFORCE_ALERT_SCHEMA` | Not in code (gate is the `enforce_alert_schema` config key, not an env var) | Documented as env var | Medium |
| docs/* | `HERMX_ADVISOR_MIN_SCORE` | Not in code | Documented | Low |
| README/docs | `HERMX_CRON_CREATE_ONLY`, `HERMX_CRON_DRY_RUN` | Not read by any `.py`/`.sh` in repo | Documented as cron-monitor controls | Medium (verify against `install-cron-monitors.sh` design intent) |
| docs/* | `HERMX_MONITOR_SUMMARY_HOUR_UTC`, `HERMX_PROACTIVE_ENABLED`, `HERMX_SKILLS` | Not in code | Documented | Low |
| docs/HERMES_CRON_MONITOR_DESIGN.md | `GET /api/monitor/alerts` endpoint | No such route exists in `dashboard.py` or `webhook_receiver.py` | Described as an endpoint | Medium (aspirational design doc presented as built) |
| ARCHITECTURE.md, MONITORING_GAPS_BRAINSTORM.md, INSTALL.md, hermes-execution.md | `/reconcile`, `/reconcile-alerts`, `/positions`, `/positions/signal`, `/ledger/freshness`, `/webhook/config`, `/status` | None are HTTP routes; the receiver/dashboard expose only `/webhook`, `/health`, `/latest`, `/api/close`, `/api/signals`, `/api/control/strategy/{id}`, `/api` | Written as if callable endpoints | Medium (readers will attempt non-existent routes; likely meant as `/api` payload fields) |
| docs/HERMES_CRON_MONITOR_DESIGN.md | `src/monitor_daemon.py` | Retired in favor of built-in Hermes cron (per project memory) | Documented as active | Medium |
| INSTALL.md vs ARCHITECTURE.md | `seen-signals.json` vs `seen-signals.jsonl` | Ledger extension inconsistent across docs | Both spellings used | Low |
| ARCHITECTURE.md, DOCKER_PACKAGE_PLAN.md | `shadow-config.json` | Dead code — `dashboard_core.shadow_config()` returns `{}`; receiver sources from `engine-config.json` | Referenced as a config source | Low |
| ARCHITECTURE.md | Endpoint inventory | Code also serves `/api/close`, `/api/signals`, `/api/control/strategy/{id}` | Lists only `/health`, `/latest`, `/webhook` | Medium |
| README.md | Slash-command list | 11 commands exist | Lists 10 (omits `/hx-exchange`, `/hx-tv-alerts`) | **High** (the reported complaint) |
| Cross-platform smoke | Kill-switch mechanism | `smoke_run.sh` forces `submit_orders=false` + `HERMX_LIVE_TRADING=false`; `smoke_run.ps1` instead sets `HERMX_SUBMIT_ENABLED=false` | Not documented; two different safety mechanisms by OS | Medium (an operator trusting one mechanism on the wrong OS gets no protection) |

---

## Recommendations (prioritized)

1. **Fix `README.md` slash-command list** — add `/hx-exchange` and `/hx-tv-alerts` so all 11 commands appear where operators look first. (Directly resolves the reported gap.)
2. **Add a complete endpoint table to `ARCHITECTURE.md`** — include `/api/close`, `/api/signals`, and `GET/POST/DELETE /api/control/strategy/{id}`, and state plainly that transport is stdlib `http.server`, not FastAPI.
3. **Correct the "FastAPI" claim** in `CLAUDE.md` and `.claude/CLAUDE.md` (dual-file rule — update both) and the README summary. This is a factual architecture error, not a phrasing nit.
4. **Publish a single authoritative env-var reference** (expand `setup/env.example` into the canonical source, or add a new config reference doc under `docs/`) covering all 57 `HERMX_*` vars with defaults. Add the 17 MISSING vars; **delete** the 10 stale ones (`HERMX_EXEC_*`, `HERMX_CRON_*`, `HERMX_ENFORCE_ALERT_SCHEMA`, `HERMX_ADVISOR_MIN_SCORE`, `HERMX_MONITOR_SUMMARY_HOUR_UTC`, `HERMX_PROACTIVE_ENABLED`, `HERMX_SKILLS`) or reconcile them with intended behavior.
5. **Document the operator scripts in `INSTALL.md`** — at minimum `docker-rebuild.sh`, `docker-update.sh`, and `security-audit.py`, with their key flags. Note the Windows `start_*.ps1` / `smoke_run.ps1` launchers.
6. **Document the compose surface** — the `read_only`/`cap_drop`/`tmpfs` hardening on the dashboard, the `ro` vs `rw` `strategies` mount divergence between `docker-compose.yml` and `docker-compose.host.yml`, and the `tailscale` service/`serve.json`. These affect security posture and are currently invisible.
7. **Resolve the smoke-test kill-switch inconsistency** — either unify `smoke_run.sh` and `smoke_run.ps1` on one mechanism or document both explicitly; a divergent safety gate across OSes is a money-system risk.
8. **Remove the phantom `/api/monitor/alerts` endpoint** from `HERMES_CRON_MONITOR_DESIGN.md` or mark it clearly as unbuilt/aspirational.
9. **Fill config-key gaps** — document `strategy_engine.default_status` and `quarantine_invalid_strategy_alerts`, and the `control-state.json: symbol_pauses` structure.

---

### Appendix — audit provenance
- Env-var ground truth: `grep -rhoE "HERMX_[A-Z0-9_]+" src scripts skills/hermx-ops/lib` → 57 distinct literals, each checked against docs.
- Slash-command ground truth: `skills/*/SKILL.md` frontmatter + `grep -rhoE "/hx-[a-z-]+" skills` → 11 commands.
- Endpoint ground truth: manual dispatch tables in `webhook_receiver.py` (`do_GET`/`do_POST`) and `dashboard.py` (`do_GET`/`do_POST`/`do_DELETE`).
- "Documented" = an explicit mention in `README.md`, `ARCHITECTURE.md`, `INSTALL.md`, `docs/*.md`, or `skills/**/*.md`.
- Counts are approximate where a category (config keys, exchange-credential env vars) has a long tail; the tables above list the operator-relevant members.
