# HermX → Devin Transition Plan

> **Purpose:** A single, self-contained document you paste into Devin to onboard it onto the HermX project with everything that does **not** migrate automatically from Windsurf/Cascade. It also tells you what *does* migrate on its own so you don't duplicate effort.
>
> **How to use this file:** Read **Section 0** first (it answers your "one doc vs. per project" question). Then follow **Section 7 (Transition Checklist)** top to bottom. Sections 4–6 are the content blocks — each one is pre-shaped to become a Devin *Knowledge* item or *Playbook*, with the **Trigger** and **Pin scope** already written for you.

---

## 0. One document or per-project? (your question, answered)

**Short answer: keep ONE master document (this file) for the human + initial bootstrap, but split its contents into multiple small Devin Knowledge items + Playbooks once you're in Devin.**

Reasons, from Devin's own guidance:

- **Devin retrieves Knowledge by *trigger*, not all at once.** It pulls the *entire* contents of a matched item, so each item must stay small and single-purpose. A monolith hurts retrieval. → *Split into focused items.*
- **Devin recommends folders** to group related Knowledge (by project/workflow) so you can bulk enable/disable. → *Use one folder per concern.*
- **Playbooks ≠ Knowledge.** Step-by-step procedures (your `/learn`, `/evolve`, `/deep-bug`, git flows) belong in **Playbooks**. General conventions and "gotchas" belong in **Knowledge**.

**You only have one repo (`mxc-admin/hermx-trader`), so "per project" mostly collapses to "this repo."** Recommendation:

- **Pin to this repo:** everything HermX-specific (architecture, money-safety rules, the control/signal skills, code-quality gotchas, deploy patterns).
- **Pin to all repos:** your universal working habits (dev-rules behavior, RTK/output hygiene, bug-fixing discipline, secrets hygiene). These are about *how you want any agent to behave*, not about HermX.
- **No pin (trigger-only):** narrow, rarely-needed notes (e.g. the MXC kinetic risk-index detail) where a clear Trigger Description does the routing.

So: **this one document now → ~8 Knowledge items + ~5 Playbooks in Devin later.** Section 4 and 5 are already chunked that way.

---

## 1. What Devin migrates automatically (do NOT re-enter these)

Devin auto-generates/pulls Knowledge from these files **if you connect the repo**:

| Source it reads automatically | Present in HermX | Notes |
|---|---|---|
| `CLAUDE.md` (root) | yes | Auto-pulled |
| `.claude/CLAUDE.md` | yes | Auto-pulled (Devin reads `CLAUDE.md` files) |
| `.windsurf/` (rules) | yes (`.windsurf/rules/*`) | Auto-pulled |
| `.cursorrules`, `.rules`, `.mdc` | no | n/a |
| `AGENTS.md` | no | Consider adding one (Section 7) |
| README / file structure | yes | Devin auto-summarizes repo on connect |

**It does NOT auto-pull generic `.md` files.** That means these are *invisible* to Devin unless you act:

- `skills/*.md` and `skills/*/SKILL.md` (hermx-control, signal-memory, emergency-stop, etc.)
- `.windsurf/workflows/*.md` (learn, evolve, deep-bug, git-*)
- `docs/*.md`, `ARCHITECTURE.md`, `INSTALL.md`, `setup/*`

→ These are handled in **Sections 4–6**.

---

## 2. What is LOST unless you copy it (the critical part)

The single biggest gap: **the Cascade/Windsurf memory database is local to Windsurf and is NOT in the repo.** Devin cannot see it. The high-value memories are transcribed verbatim in **Section 3** so they survive. Also at risk:

- **Windsurf Workflows** (`/learn`, `/evolve`, `/deep-bug`, git flows) → become **Playbooks** (Section 5).
- **`.claude/skills/` SKILL.md files** (hermx-control, signal-memory) → become **Knowledge + Playbooks** (Section 4 / 5).
- **Tool-routing 3-tier rules** (Cascade/CC/CC2 delegation) → **mostly obsolete in Devin** (Devin is its own single agent), but the *underlying habits* are preserved as Knowledge (Section 4.7).
- **MCP server config** (cc, cc2, grok, memory, tradingview-bridge) → Devin has its own MCP/integration model; see **Section 6**.

---

## 3. Memories that will be lost (transcribed verbatim)

> Paste each of these into Devin as a **Knowledge item** with the Trigger + Pin scope shown. These came from the Cascade memory DB and exist nowhere in the repo.

### 3.1 — Docker packaging & deploy model
**Trigger:** "Building, releasing, or debugging the HermX Docker image / docker-compose / installer." **Pin:** this repo.

HermX is packaged as a **Docker Compose deployable from a published GHCR image**, not a source clone:

1. **Multi-stage Dockerfile:** `node:20-slim` builds `dashboard-ui/out`, the Python stage bakes it in. `dashboard-ui/out` **must** be in the image or the dashboard silently serves legacy server-rendered HTML.
2. Bake `config/runtime.demo.json` as `/app/engine-config.json` — **do not** `COPY engine-config.json` (it's gitignored; breaks clean builds).
3. The installer (`scripts/install-docker.sh`) **seeds `strategies/` from the pulled image** before the first `docker compose up`. An empty bind-mounted dir shadows the baked files → zero strategies loaded.
4. The dashboard writes `control-state.json`; it needs `HERMX_DATA_DIR=/app/data` + a `hermx-state:/app/data` (rw) mount **even with `read_only: true`** on the root fs.
5. Named volumes (`hermx-data`, `hermx-state`) survive `docker compose pull && up -d`. Docker copies mount-point ownership from the image on first volume creation, so the image must `chown -R hermx:hermx /app` **before** `USER hermx`.
6. `shadow-config.json` is **dead code**: `dashboard_core.py:shadow_config()` returns `{}`. The app sources config from `engine-config.json` via `load_engine_config()`.

### 3.2 — How to run tests (venv)
**Trigger:** "Running or writing Python tests for HermX." **Pin:** this repo.

There is a local venv at `.venv/`. Run tests **only** via:
```bash
./.venv/bin/pytest <test_files> <options>
# e.g.
./.venv/bin/pytest tests/test_phase2_webhook_security.py -x -q
./.venv/bin/pytest tests/ -x -q
```
Do **not** use `python -m pytest` / `python3 -m pytest` / version-specific Python — pytest and deps are installed only inside `.venv/`.

### 3.3 — MXC kinetic risk-index gate
**Trigger:** "Working on the dashboard-risk skill or the risk-index trade gate." **Pin:** none (trigger-only).

- MXC kinetic crypto dashboard URL: `https://mxc-kinetic-crypto.replit.app/`
- Used by the planned `dashboard-risk` skill to read risk levels (`pp_acc`, `pp_vel`, `regime`, `risk_state`) and veto trades when risk is elevated/high.
- Toggle flag is `risk_index_gate_enabled` (**not** `dashboard_risk_enabled`).
- The toggle lives on the **local** HermX dashboard (`127.0.0.1:8098`), not the MXC global dashboard. Stored in `control-state.json` (same pattern as `symbol_pauses`).
- The skill checks this flag first via `GET /api` — if false, return `unknown` (fail-open). If true, fetch the MXC dashboard and evaluate risk.

### 3.4 — Config-deletion regression lesson
**Trigger:** "Removing or emptying a config source/file in HermX." **Pin:** this repo.

(Historical — dead code; `shadow_config()` is a no-op kept only for callers.) Deleting `shadow-config.json` and reducing `shadow_config()` to `return {}` caused a dashboard "Engine - ERROR": `_dashboard_executor`'s fallback `exchange="ccxt"` (a *backend* name) masked `CcxtExecutor`'s built-in `"okx"` default, so `getattr(ccxt, "ccxt")` returned `None`. **Rule:** when removing a config source, audit every consumer for masked defaults — especially where `or`-chains conflate backend names and venue names.

### 3.5 — Money-system proven patterns (from `.claude/CLAUDE.md`, worth pinning explicitly)
**Trigger:** "Touching webhook intake, dedupe, replay, or recovery logic in HermX." **Pin:** this repo.

- **`raw-webhooks.jsonl` is the durable WAL.** Every intake is fsync'd to it *before* the queue put, so it — not the in-memory `PROCESS_QUEUE` — is the recovery source. Replay it at startup; don't add a new queue store.
- **The dedupe ledger (`signals.jsonl`) is written AFTER dequeue.** It cleanly partitions "processed" from "queued but not dequeued" — the correctness backstop on replay.
- **`received_at` (microsecond ISO) is the join key** between intake and outcome rows. Collision-safe; use it to correlate, not as a freshness measure.
- **Freshness is bounded on signal bar time (`tv_time`), never server time (`received_at`).** After an outage the server clock is current but the bar is stale.
- **`normalize()` is non-deterministic for time-less payloads:** with no `tv_time` it falls back to `now_iso()`, yielding a new `signal_id` each call and breaking replay dedupe. Drop time-less payloads on replay; never re-derive their id from wall-clock.
- **Restarts are routine:** systemd `Restart=always` / `RestartSec=5`. Design recovery for frequent restarts.

---

## 4. Knowledge items (conventions & gotchas — paste each as its own item)

### 4.1 — Project overview
**Trigger:** always relevant to this repo. **Pin:** this repo.

HermX is a Python crypto-trading **execution layer**: it receives TradingView alerts, validates them, and dispatches orders through CCXT exchange adapters. Components: FastAPI webhook receiver, CCXT adapters, a local Next.js dashboard, and a paper/demo trading path. Key files:
- `src/webhook_receiver.py` — FastAPI alert receiver & validation
- `src/executors/ccxt_adapter.py` — CCXT exchange adapter
- `src/execution/service.py` — order dispatch & execution logic
- `src/dashboard.py` / `src/dashboard_core.py` — local dashboard backend
- `config/runtime.*.demo.json` — per-exchange runtime config
- `strategies/*.json` — per-strategy constraints (`strategy_id`, asset, `budget_usd`, `leverage`, `margin_mode`, timeframe)

### 4.2 — Code-quality known patterns (bugs & their fix patterns)
**Trigger:** "Editing dashboard, executor, Docker, or config code in HermX." **Pin:** this repo.

(Auto-migrates via `.windsurf/rules/code-quality.md` if the repo is connected — re-stated here so it survives even if rule auto-pull is incomplete.)
- **`normalize()` non-determinism** — see 3.5.
- **Config-deletion masked default** — see 3.4.
- **`shadow-config.json` is dead code** — `dashboard_core.py:shadow_config()` is a `{}` no-op; the receiver sources from `engine-config.json`. Grep before assuming a config file is live.
- **Dashboard UI silently broken** — `dashboard.py` resolves `STATIC_DIR = REPO_ROOT/dashboard-ui/out`; if the Dockerfile omits it, the `.is_dir()` gate fails and it falls back to legacy HTML with no error.
- **Empty bind-mount shadows baked files** — `./strategies:/app/strategies:ro` with an empty host dir replaces image contents entirely; installer must seed first.
- **`control-state.json` needs a writable mount** — `read_only: true` without `hermx-state:/app/data` (rw) makes mode toggles silently fail to persist.

### 4.3 — Code-quality anti-patterns (tests)
**Trigger:** "Writing or reviewing HermX tests." **Pin:** this repo.

- **Don't re-implement the handler inline in a test** — `test_intake_hardening.py::test_latest_corrupt_returns_503_not_500` copies the handler body, so it passes even if production regresses. Tests must exercise the production code path.
- **Don't arm tests via a legacy config-flag chain** — `test_unknown_resolver_controls.py::_armed_config` arms via a dead path; production moving on won't fail the test. Arm through the current production path.

### 4.4 — HermX control surface (money-safety critical)
**Trigger:** "Answering about HermX positions/PnL/arm state, or relaying/closing a trade." **Pin:** this repo.

This is the operational contract from `skills/hermx-control/SKILL.md`. **The safety lives in Python, not in the agent.** The agent only talks to the **local** HermX HTTP API over loopback (`127.0.0.1`, no key on-host):

- **Reads:** `GET 127.0.0.1:8098/api` (positions, PnL, executor health, ledgers), `GET 127.0.0.1:8098/health` (`mode`, `arm` block), `GET 127.0.0.1:8891/health` + `/latest`.
- **Act (relay):** `POST 127.0.0.1:8891/webhook` with a TradingView alert JSON. Required fields: `strategy_id`, `symbol`, `timeframe∈{30m,1h,2h,3h,4h}`, `side∈{buy,sell}`, `tv_signal_price`, `tv_time`, `exchange∈{okx,kucoin,bybit,hyperliquid}`, `source=tradingview`. **No size/notional/leverage field exists** — the receiver computes notional from the strategy file and runs the gate chain.
- **Operator-instructed close:** `POST 127.0.0.1:8891/api/close` (reduce-only; position must exist).

**Hard rules:** never call an exchange/CCXT/shell for orders; never invent a size/notional/leverage; never override the kill switch, gates, or a symbol pause; **never report a failed/stale read as "flat" — report UNKNOWN**; only act on a real inbound signal or explicit human instruction.

**Arm truth-table** (`/health` → `arm`): `kill_switch_engaged:true` → don't relay; `live_strategies:0` → no live strategies; `all_auth_healthy:false` → don't relay. `execution_mode` enum = `{demo,paper,live,shadow}`; **only `live` is real money.**

### 4.5 — Signal memory (read-only continuity)
**Trigger:** "Checking recent HermX signal/decision history before relaying or answering 'what traded lately?'." **Pin:** this repo.

From `skills/signal-memory/SKILL.md`. Read-only; never relays. `GET 127.0.0.1:8098/api/signals?n=50` (optional `&symbol=BTCUSDT`). Each record: `symbol`, `side`, `strategy_id`, `submitted_at`, `mode∈{submitted,not_submitted,vetoed_by_advisor,unknown}`, `reason`, `advisor_verdict∈{proceed,skip,unknown}`. Unreachable → treat as `unknown`, don't block. Empty ≠ paused. `mode:not_submitted` is normal in demo/paper.

### 4.6 — Dual-file rules convention (Cascade legacy — informational)
**Trigger:** "Editing files in `.claude/rules/` or `.windsurf/rules/`." **Pin:** this repo.

`code-quality.md` and `dev-rules.md` exist in **both** `.claude/rules/` (with YAML frontmatter) and `.windsurf/rules/` (no frontmatter). Content below the frontmatter must stay identical. **In a Devin-only world you can collapse these to one source of truth** (keep `.claude/` or convert to `AGENTS.md`) — but if you keep Cascade around in parallel, honor the dual-file sync.

### 4.7 — Working habits (universal — was the Cascade 3-tier routing)
**Trigger:** general behavior. **Pin:** all repos.

The Cascade "Tier 1/CC/CC2 delegation" routing is **Cascade-specific and does not apply to Devin** (Devin is a single autonomous agent — there are no `mcp0_*`/`mcp1_*` tiers to delegate to). Preserve only the underlying habits:
- **Plan before large changes.** If a task touches >3 files, break it down first.
- **Explore before acting** when scope is unknown.
- **For deep bugs** (survived 2+ fix attempts), switch to the systematic procedure (Playbook 5.3).

### 4.8 — Dev behavior rules (from `dev-rules.md`)
**Trigger:** general behavior. **Pin:** all repos.

1. Describe approach and get approval before implementing.
2. Ask clarifying questions on ambiguity — don't assume.
3. If a change touches >3 files, split it first.
4. Changes to shared libraries need explicit confirmation.
5. After writing code, list edge cases and suggested tests.
6. For bugs, write a minimal reproduction/test first, then fix.
7. When corrected, reflect, state a plan to avoid repeating it, and record the pattern.
8. Prefer minimal upstream root-cause fixes over downstream workarounds; avoid over-engineering.
9. **Secrets:** never hardcode keys; never put secrets in MCP/config files; use env or a secrets manager.

---

## 5. Playbooks (procedures — paste each as a Devin Playbook)

> Devin Playbook style: one imperative step per line; cover setup → task → delivery.

### 5.1 — `/learn` → "Capture session learnings"
1. Identify the most specific knowledge target for each learning.
2. Classify each learning: rejected approach / architecture decision / bug-fix pattern / proven pattern / constraint / domain rule.
3. Read the target file first to avoid duplicates; flag any contradiction with existing entries.
4. Append concisely (what + verdict + reason, ≤3 lines).
5. For repo-resident knowledge, update the file; **also create/update the equivalent Devin Knowledge item** (this replaces the old "save to Windsurf memory" step).
6. Output a summary of what was written and where.

### 5.2 — `/evolve` → "Consolidate knowledge"
1. Read all knowledge sources (repo rule/skill files + Devin Knowledge items).
2. Identify: patterns to promote to a rule, stale entries to retire, duplicates to merge, repeated tasks that deserve a new Playbook.
3. Check `CLAUDE.md`/`AGENTS.md` is still accurate and within budget; promote overflow to dedicated items.
4. Propose changes for approval before editing.
5. Execute approved changes as append/update only — never rewrite wholesale.
6. Output net change summary.

### 5.3 — `/deep-bug` → "Systematic deep-bug investigation"
1. Write a minimal reproduction that demonstrates the bug.
2. Isolate: trace backward through the call chain, log at each layer boundary, find where expected ≠ actual.
3. Root-cause analysis: what is the true cause (not the symptom), why the code exists this way, where else the pattern lives.
4. Fix upstream at the root cause with the minimal change.
5. Verify against the Step-1 reproduction; search for sibling occurrences.
6. Record the pattern (run the `/learn` Playbook).

### 5.4 — Git commit/checkpoint/push/undo
- **Commit:** stage changed files, write a concise message, commit locally. (HermX convention: noisy git commands prefixed with `rtk`; use `git log -n <N>` to avoid pagers.)
- **Checkpoint:** quick mid-session commit with a timestamped message.
- **Push:** push local commits to remote when ready to sync.
- **Undo:** soft/mixed/hard reset or unstage safely; confirm before any hard reset.

### 5.5 — Emergency stop / TradingView recovery
Port `skills/emergency-stop.md` and `skills/tradingview-recovery.md` verbatim into Playbooks (they're operational runbooks). Trigger them on "halt trading" / "TradingView alerts stopped firing."

---

## 6. MCP servers & integrations

Your Windsurf MCP servers (`cc`, `cc2`, `grok`, `memory`, `tradingview-bridge`) **do not transfer** — Devin has its own integration/MCP model. Map them:

| Windsurf MCP | Role | Devin equivalent / action |
|---|---|---|
| `cc` / `cc2` (Claude Code) | Delegation agents | **Drop** — Devin is the agent. |
| `memory` (knowledge graph) | Persistent memory | Replaced by **Devin Knowledge**. The graph was empty (`read_graph` returned nothing), so nothing to port. |
| `grok` | LLM helper | Drop or replace with Devin's own reasoning. |
| `tradingview-bridge` | Live TradingView chart control (78 tools) | **Keep as an MCP server in Devin** if Devin supports your MCP, or run it where you operate the chart. Document the connection in Devin if needed. |

**Secrets:** continue to keep `HERMX_SECRET` and exchange keys in env / a secrets manager — never in any committed config or Knowledge item.

---

## 7. Transition checklist (do this, in order)

1. **Connect the repo** `mxc-admin/hermx-trader` to Devin and let it auto-generate repo Knowledge from README + structure + `CLAUDE.md` + `.windsurf/rules/`.
2. **Review the auto-generated Knowledge** for completeness/accuracy (Devin's own best practice #1).
3. **Add an `AGENTS.md`** at repo root (Devin reads it natively) consolidating Sections 4.1, 4.4, 4.8 — this gives Devin a single canonical brief and lets you retire the dual-file split later.
4. **Create Knowledge items** from Section 3 (lost memories) and Section 4, each with its stated Trigger and Pin scope. Group them in folders: `hermx/architecture`, `hermx/safety`, `hermx/deploy`, `global/habits`.
5. **Create Playbooks** from Section 5.
6. **Set up MCP/integrations** per Section 6 (keep `tradingview-bridge` if supported; drop the rest).
7. **Verify secrets** are in env/secrets manager; nothing sensitive in Knowledge.
8. **Smoke test:** run 2 parallel Devin sessions on a small task (e.g. "add a test for the `normalize()` time-less drop") and confirm under *Accessed Knowledge* that it pulled 3.5 / 4.2. Iterate triggers if it didn't.
9. **Decide on Cascade/Windsurf:** if you're fully moving, you can stop maintaining the dual-file rules and `.windsurf/` workflows; if running in parallel, keep them synced.

---

## 8. Quick reference — Windsurf → Devin mapping

| Windsurf/Cascade concept | Lives in | Migrates? | Devin home |
|---|---|---|---|
| `CLAUDE.md`, `.claude/CLAUDE.md` | repo | auto | Repo Knowledge |
| `.windsurf/rules/*`, `.claude/rules/*` | repo | auto | Repo Knowledge |
| `skills/*/SKILL.md`, `skills/*.md` | repo (generic `.md`) | **no** | Knowledge (4.4/4.5) + Playbooks (5.5) |
| `.windsurf/workflows/*` | repo | **no** | Playbooks (Section 5) |
| Cascade memory DB | Windsurf local | **no** | Knowledge (Section 3) |
| MCP servers | Windsurf config | **no** | Devin integrations (Section 6) |
| 3-tier tool routing | rules | n/a in Devin | Habits only (4.7) |
