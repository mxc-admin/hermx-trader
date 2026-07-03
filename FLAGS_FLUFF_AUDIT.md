# HermX — Ruthless Flag Fluff Audit

**Date:** 2026-07-03
**Question:** "The amount of flags is nuts." Of ~50 live `HERMX_*` env reads, how many are *necessary* vs over-engineered knobs nobody will ever turn?
**Method:** static extraction of every `os.environ.get` / `_env_*` read in `src/`, cross-referenced against test overrides, `setup/env.example`, `docker-compose*.yml`.
**Complements** `FLAGS_AUDIT.md` (which catalogs *dead* flags + renames). This doc classifies the *live* runtime flags for removal.

---

## 1. Ruthless Summary

**We have ~51 live runtime flags. Only 8 are non-negotiable. The other ~43 are tuning, cosmetics, or unused-feature toggles that can be hard-coded or merged.**

| Bucket | What it is | Count | Verdict |
|---|---|---:|---|
| **A. Non-negotiable** | auth, network binding, kill-switch, ports, path, venue | **8** | Keep. |
| **B. Operational tuning** | timeouts, thresholds, SLOs, queue depths, rotation sizes | **26** | Hard-code the default. Nobody tunes these. |
| **C. UI cosmetics** | refresh interval, display TZ, dev CORS | **4** | Hard-code / delete. Developer conveniences, not operator controls. |
| **D. Unused-feature toggles** | Advisor LLM subsystem (off by default), alert-webhook | **7** | Collapse to 1 enable each; the config-detail flags are dead weight. |
| **E. Duplicate / mergeable** | enable+param pairs, TZ name+offset | **6** (pairs) | Merge: `PARAM=0` means disabled. |

Net: **~50 flags → 8 mandatory + ~4 optional escape hatches ≈ 12 real knobs.** Roughly a **75% reduction** in operator-facing surface.

The core complaint is valid. The flag count comes from three habits:
1. **Every timeout/threshold got an env read "just in case"** — 26 of them. None have a real deployment that changes them.
2. **A whole off-by-default LLM subsystem (Advisor) exposes 5 flags** — 4 of which only matter if you turn the feature on, which almost nobody does.
3. **Enable + parameter pairs were split into two flags** where one collapses into the other (`STALE_SECONDS=0` ⇒ disabled).

---

## 2. Flag-by-Flag Classification

Confidence = probability the classification is correct (i.e. that it's *safe* to remove/merge). Higher = more confident it's fluff.

### A — Non-negotiable (KEEP) — 8 flags

| Flag | Read @ | Why it must stay | Conf. it's fluff |
|---|---|---|---:|
| `HERMX_SECRET` | `webhook_receiver.py:99`, `dashboard.py:48` | Sole auth token, fail-closed. Per-deploy secret. | 0.00 |
| `HERMX_LIVE_TRADING` | `hermx_shared.py:67` | Global real-money kill switch. | 0.00 |
| `HERMX_BIND_HOST` | `webhook_receiver.py:95`, `dashboard.py:44` | Loopback vs `0.0.0.0`; Docker needs it. | 0.05 |
| `HERMX_RECEIVER_PORT` (`SHADOW_PORT`) | `webhook_receiver.py:91` | Port binding — environment-specific. | 0.10 |
| `HERMX_DASHBOARD_PORT` (`CLEAN_DASHBOARD_PORT`) | `dashboard.py:40` | Port binding. | 0.10 |
| `HERMX_ROOT` (`SHADOW_ROOT`) | `webhook_receiver.py:126`, `dashboard.py:38` | App base dir; bare-host vs container. | 0.10 |
| `HERMX_DATA_DIR` | `webhook_receiver.py:132`, `dashboard.py:56` | State/ledger dir; compose mounts a volume here. | 0.10 |
| `HERMX_CCXT_EXCHANGE` | `ccxt_adapter.py:174` | Default venue selection. | 0.15 |

### B — Operational tuning (HARD-CODE) — 26 flags

Defaults are correct for essentially every deployment. Each is one more line in `env.example` and one more thing to misconfigure.

| Flag | Default | Read @ | Recommendation | Conf. fluff |
|---|---|---|---|---:|
| `HERMX_MAX_BODY_BYTES` | 262144 | `webhook_receiver.py:103` | Hard-code 256 KiB. | 0.92 |
| `HERMX_RATE_LIMIT_WINDOW_SECONDS` | 60 | `:104` | Hard-code. | 0.90 |
| `HERMX_RATE_LIMIT_MAX_REQUESTS` | 120 | `:105` | Hard-code. | 0.88 |
| `HERMX_REPLAY_WINDOW_SECONDS` | 300 | `:102` | Hard-code (HMAC replay window). | 0.85 |
| `HERMX_QUEUE_MAXSIZE` | 200 | `:106` | Hard-code. | 0.85 |
| `HERMX_QUEUE_SATURATION_ALERT_DEPTH` | 100 | `:265` | Hard-code (or derive = maxsize/2). | 0.90 |
| `HERMX_QUEUE_LAG_SLO_SECONDS` | 30 | `:124` | Hard-code. | 0.90 |
| `HERMX_REQUEST_TIMEOUT_SECONDS` | 30 | `:125` | Hard-code. | 0.85 |
| `HERMX_SUBMIT_TIMEOUT_SECONDS` | 45 | `ccxt_adapter.py:75`, `:119` | Hard-code (read in 2 files — extra reason to centralize). | 0.80 |
| `HERMX_WORKER_POOL_SIZE` | 1 | `:120` | Hard-code 1 (single worker is deliberate for ordering). | 0.78 |
| `HERMX_REPLAY_LOOKBACK_SECONDS` | 300 | `:117` | Hard-code. | 0.88 |
| `HERMX_REPLAY_MAX_TV_AGE_SECONDS` | 120 | `:118` | Hard-code (freshness bound). | 0.82 |
| `HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS` | 86400 | `:121` | Hard-code 24h. | 0.80 |
| `HERMX_WATCHDOG_STALE_SECONDS` | 120 | `:123` | Hard-code (merge w/ ENABLED — see E). | 0.88 |
| `HERMX_UNKNOWN_RESOLVER_INTERVAL_SECONDS` | 30 | `:255` | Hard-code (merge w/ ENABLED). | 0.90 |
| `HERMX_UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS` | 900 | `:256` | Hard-code. | 0.85 |
| `HERMX_UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK` | 50 | `:257` | Hard-code. | 0.90 |
| `HERMX_PLANNED_ORDER_TIMEOUT_SECONDS` | 300 | `:262` | Hard-code. | 0.90 |
| `HERMX_JOURNAL_SEGMENT_MAX_RECORDS` | 1000 | `:157` | Hard-code; keep as **module constant** so tests monkeypatch it (5 tests force rotation via this). | 0.70 |
| `HERMX_JOURNAL_SEGMENT_RETENTION` | 5 | `:161` | Hard-code. | 0.92 |
| `HERMX_LEDGER_ROTATE_MAX_BYTES` | 64 MiB | `:170` | Hard-code. | 0.90 |
| `HERMX_LEDGER_ROTATE_RETENTION` | 5 | `:171` | Hard-code. | 0.92 |
| `HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS` | 2 | `:2067` | Hard-code. | 0.92 |
| `HERMX_REPLAY_ENABLED` | true | `:116` | Keep as escape hatch OR hard-code true. Recovery-safety default. | 0.60 |
| `HERMX_WATCHDOG_ENABLED` | true | `:122` | Merge into `STALE_SECONDS=0` ⇒ off (see E). | 0.72 |
| `HERMX_EXEC_BACKEND` | `ccxt`/`""` | `service.py:35`, `webhook/config.py:10` | Only one backend exists. Keep as test/ops override but drop from `env.example`. Also has inconsistent default (see `FLAGS_AUDIT.md §3.3`). | 0.60 |

### C — UI cosmetics (HARD-CODE / DELETE) — 4 flags

| Flag | Default | Read @ | Recommendation | Conf. fluff |
|---|---|---|---|---:|
| `HERMX_DASH_REFRESH_SECONDS` | 20 | `dashboard.py:78` | Hard-code. UI polling cadence — not an operator control. Also duplicated by `NEXT_PUBLIC_REFRESH_INTERVAL` (10000ms) in the UI. | 0.92 |
| `HERMX_DASH_TZ` | `""`→UTC | `dashboard_core.py:144` | Hard-code UTC, or keep ONE merged TZ flag (see E). Display-only. | 0.85 |
| `HERMX_DASH_TZ_OFFSET_HOURS` | `""`→UTC | `dashboard_core.py:152` | **Delete** — duplicate of `HERMX_DASH_TZ` (E). | 0.92 |
| `HERMX_DEV_CORS` | `""` (off) | `dashboard.py:2403` | Hard-code off. Dev-only convenience; security-adjacent (should never be on in prod). | 0.90 |

### D — Unused-feature toggles (COLLAPSE) — 7 flags

**Advisor LLM subsystem** — off by default (`enabled: False`). Spawns `hermes -z` as a subprocess per signal to veto trades. 5 flags for a feature almost nobody runs. Per project memory, monitoring pivoted to **Hermes cron**, not this inline advisor — reinforcing that this path is low-usage.

| Flag | Default | Read @ | Recommendation | Conf. fluff |
|---|---|---|---|---:|
| `HERMX_ADVISOR_ENABLED` | false | `:294`, `dashboard.py:49` | Keep the ONE enable flag (if feature stays). | 0.30 |
| `HERMX_ADVISOR_COMMAND` | `hermes` | `:295` | Hard-code `hermes`. | 0.85 |
| `HERMX_ADVISOR_SKILLS` | `hermx-control` | `:296` | Hard-code. | 0.82 |
| `HERMX_ADVISOR_MODEL` | `""` | `:297` | Hard-code / drop (`""` = hermes default). | 0.85 |
| `HERMX_ADVISOR_TIMEOUT_SECONDS` | 30 | `:298` | Hard-code. | 0.85 |
| `HERMX_ALERT_WEBHOOK_URL` | `""` | `:2065` | Keep — real feature enable (URL present ⇒ on). | 0.20 |
| _(`HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS` counted in B)_ | | | | |

> The Advisor already reads a 3-layer default chain (env → `engine-config.json` advisor block → `ADVISOR_DEFAULTS`). If the feature is kept, its non-enable settings belong in the **config file**, not env. Dropping the 4 env reads loses nothing — config still overrides.

### E — Duplicate / mergeable pairs — 6 merges

| Merge | From → To | Rationale |
|---|---|---|
| `HERMX_WATCHDOG_ENABLED` + `HERMX_WATCHDOG_STALE_SECONDS` | → `STALE_SECONDS` (`0` = disabled) | One knob. |
| `HERMX_UNKNOWN_RESOLVER_ENABLED` + `..._INTERVAL_SECONDS` | → `INTERVAL_SECONDS` (`0` = disabled) | One knob. |
| `HERMX_REQUIRE_HMAC` + `HERMX_WEBHOOK_HMAC_KEY` | → key present ⇒ HMAC required | Presence-implies-enable; kills a bool. |
| `HERMX_DASH_TZ` + `HERMX_DASH_TZ_OFFSET_HOURS` | → single `HERMX_DASH_TZ` (accept IANA *or* `±N`) | Two ways to say the same thing. |
| `HERMX_DASH_REFRESH_SECONDS` + `NEXT_PUBLIC_REFRESH_INTERVAL` | → one source, one unit | Backend 20s vs UI 10000ms — different values *and* units (see `FLAGS_AUDIT.md §3.2`). |
| `HERMX_ALERT_WEBHOOK_URL` + `..._TIMEOUT_SECONDS` | → URL enables; timeout hard-coded | Timeout is fluff; URL is the real toggle. |

---

## 3. Removal Execution Plan

### Tier 1 — Pure hard-codes (delete env read, keep the literal as a module constant)

Zero behavior change for any real deployment. **Convert `X = int(os.environ.get("HERMX_X", "N"))` → `X = N`.**

`webhook_receiver.py`:
- `:103` MAX_BODY_BYTES · `:104-105` RATE_LIMIT_* · `:106` QUEUE_MAXSIZE · `:117-118` REPLAY_LOOKBACK/MAX_TV_AGE · `:121` SIGNAL_DEDUPE_WINDOW · `:124` QUEUE_LAG_SLO · `:125` REQUEST_TIMEOUT · `:157` JOURNAL_SEGMENT_MAX_RECORDS (keep as bare constant) · `:161` JOURNAL_SEGMENT_RETENTION · `:170-171` LEDGER_ROTATE_* · `:255-257` UNKNOWN_RESOLVER_* · `:262` PLANNED_ORDER_TIMEOUT · `:265` QUEUE_SATURATION_ALERT_DEPTH · `:2067` ALERT_WEBHOOK_TIMEOUT · `:102` REPLAY_WINDOW · `:119` SUBMIT_TIMEOUT · `:120` WORKER_POOL_SIZE
- `ccxt_adapter.py:75` SUBMIT_TIMEOUT (centralize to one constant)

`dashboard.py` / `dashboard_core.py`:
- `dashboard.py:78` DASH_REFRESH_SECONDS · `:2403` DEV_CORS
- `dashboard_core.py:144,152` DASH_TZ / TZ_OFFSET → single UTC constant (or one merged flag)

`webhook/config.py`:
- `:295-298` ADVISOR_COMMAND/SKILLS/MODEL/TIMEOUT → keep in `ADVISOR_DEFAULTS`, drop the 4 `_env_*` reads at `webhook_receiver.py:295-298`

### Tier 2 — Merges (E)

- Watchdog → `STALE_SECONDS=0` disables (`:122-123`)
- Resolver → `INTERVAL_SECONDS=0` disables (`:2219` gate + `:255`)
- HMAC → key presence enables (`:100-101`)
- TZ → one flag (`dashboard_core.py:144-152`)

### Tier 3 — Keep (real escape hatches)

`HERMX_SECRET`, `HERMX_LIVE_TRADING`, `HERMX_BIND_HOST`, `HERMX_RECEIVER_PORT`, `HERMX_DASHBOARD_PORT`, `HERMX_ROOT`, `HERMX_DATA_DIR`, `HERMX_CCXT_EXCHANGE`, `HERMX_ALERT_WEBHOOK_URL`, `HERMX_ADVISOR_ENABLED`, `HERMX_RECONCILE_ENABLED` (active rollout soak gate + 8 tests), `HERMX_DASH_AUTH` (never document disabling), `HERMX_REPLAY_ENABLED` (recovery safety).

---

## 4. Risk Assessment — what could break

**Tests are the main blast radius.** These flags are set by tests as overrides to exercise behavior. Hard-coding the env read means those tests must switch to `monkeypatch.setattr(module, "CONSTANT", ...)`:

| Flag | # tests | What the test forces | Migration |
|---|---:|---|---|
| `HERMX_JOURNAL_SEGMENT_MAX_RECORDS` | 5 | small value → force checkpoint+rotation | monkeypatch module constant — **keep it a bare module-level `int`** so this is a 1-line change |
| `HERMX_REPLAY_WINDOW_SECONDS` | 4 | HMAC replay window edge | monkeypatch |
| `HERMX_WATCHDOG_STALE_SECONDS` | 3 | small → trigger watchdog | monkeypatch |
| `HERMX_WATCHDOG_ENABLED` | 3 | on/off | monkeypatch (or merged flag) |
| `HERMX_QUEUE_LAG_SLO_SECONDS` | 3 | SLO breach | monkeypatch |
| `HERMX_RATE_LIMIT_{WINDOW,MAX}` | 1+1 | force 429 | monkeypatch |
| `HERMX_MAX_BODY_BYTES` | 1 | force 413 | monkeypatch |
| `HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS` | 2 | dedupe edge | monkeypatch |
| `HERMX_UNKNOWN_RESOLVER_ENABLED` | 2 | on/off | monkeypatch / merged |
| `HERMX_REPLAY_ENABLED` | 2 | on/off | keep flag |
| `HERMX_ADVISOR_{ENABLED,COMMAND}` | 2+1 | exercise advisor | keep ENABLED; monkeypatch COMMAND |
| `HERMX_SUBMIT_TIMEOUT_SECONDS` · `HERMX_DASH_TZ{,_OFFSET}` | 1 each | behavior/format | monkeypatch |

> **Design guidance:** keep hard-coded values as **module-level constants** (`HERMX_MAX_BODY_BYTES = 262144`), not inlined literals. Then a test does `monkeypatch.setattr("webhook_receiver.HERMX_MAX_BODY_BYTES", 10)` — same seam, minus the env indirection and minus the `env.example` line. This is the clean way to keep the flags *testable* without keeping them *operator-facing*.

**Docker / systemd:** `docker-compose*.yml` sets only `HERMX_DATA_DIR`, `HERMX_BIND_HOST`, `HERMX_IMAGE` — all in the KEEP list. Hard-coding B/C/D flags is safe: any `.env`/compose var that's no longer read simply becomes inert (no crash). No compose or unit file breaks.

**Documented setup:** `setup/env.example` lists ~10 of the B/D flags. Removing them from code means pruning them from `env.example` (a doc win, not a break). The dead flags in `FLAGS_AUDIT.md §3.1` should be purged in the same pass.

**Behavioral risk of the merges (E):** the `PARAM=0 ⇒ disabled` convention must be implemented carefully — e.g. `WATCHDOG_STALE_SECONDS=0` must short-circuit *before* the stale comparison, or it degenerates to "everything is instantly stale." Each merge needs a guard test.

**Not safe to touch:** `HERMX_RECONCILE_ENABLED` — it's an active soak gate (default OFF, being rolled out) with 8 tests. `HERMX_LIVE_TRADING`, `HERMX_SECRET`, `HERMX_DASH_AUTH` — safety/auth. Leave all of these.

---

## 5. Overall Confidence

**0.82** that ~34 flags (all of B minus the 3 keep-as-hatch, all of C, 4 of D, plus the E merges) can be removed or merged with **zero production behavior change**, the only cost being ~25 one-line test migrations from `setenv` to `monkeypatch.setattr`.

Lower-confidence calls, flagged for your decision:
- `HERMX_EXEC_BACKEND` (0.60) — only one backend, but tests + config use it; drop from docs, keep the read.
- `HERMX_REPLAY_ENABLED` / `HERMX_WATCHDOG_ENABLED` (0.60–0.72) — recovery/safety toggles; defensible to keep one escape hatch each.
- `HERMX_ADVISOR_ENABLED` (0.30) — keep; it gates a real (if rarely-used) subsystem. The *sub-settings* are the fluff, not the enable.

**Bottom line: the complaint is correct.** ~8 flags do real work. The rest is defensive tuning that was never going to be tuned. Hard-code the constants, keep them as module-level values for testability, collapse the 6 enable/param pairs, and move Advisor's 4 sub-settings into the config file. Operator-facing surface drops from ~50 to ~12.

> **Per `dev-rules.md`:** this proposal touches `webhook_receiver.py`, `dashboard.py`, `dashboard_core.py`, `webhook/config.py`, `ccxt_adapter.py` and the test suite — well over 3 files and a shared import surface. **Do NOT implement as one change.** Break into Tier-1 (pure hard-codes, per-file), Tier-2 (merges, each with a guard test), and get explicit confirmation before editing shared modules.
