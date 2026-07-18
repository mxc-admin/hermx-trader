#!/usr/bin/env python3
"""Parallel MXC shadow webhook receiver.

Safe by design: receives TradingView alerts, answers quickly, enriches with
available MXC context, writes append-only ledgers, and only calls OKX when the
active config explicitly enables sandbox/demo execution.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from security.credentials import redact_secrets
from security.webhook_auth import (  # noqa: E402  pure security helpers (Phase 1 step 3)
    webhook_auth_config_healthy as _webhook_auth_config_healthy,
    env_file_permissions_healthy as _env_file_permissions_healthy,
    client_ip as _client_ip_impl,
    rate_limit_key as _rate_limit_key_impl,
    rate_limit_allow as _rate_limit_allow_impl,
    parse_replay_timestamp as _parse_replay_timestamp_impl,
    compute_webhook_hmac as _compute_webhook_hmac_impl,
    verify_webhook_hmac as _verify_webhook_hmac_impl,
    authenticate_webhook_request as _authenticate_webhook_request_impl,
)
# Phase 0b: money/decimal primitives extracted to webhook/money.py (pure leaf).
# Re-export so existing call sites and tests keep resolving wr.D, wr.dec_usd, ...
from webhook.money import (  # noqa: E402  F401
    D,
    dec_usd,
    dec_notional,
    dec_pct,
    dec_units,
    dec_text,
    usd_text,
    notional_text,
    pct_text,
    units_text,
    canonicalize_decimal_fields,
)
# Option A: leaf-pure time + JSONL-I/O primitives extracted to webhook/timeutil.py
# and webhook/ledger_io.py. Re-export so wr.now_iso / wr.append_jsonl / ... stay
# importable and monkeypatchable at call sites that live in this module. The
# path-bound recorders (record_pipeline_event, record_raw_webhook,
# startup_quarantine_partial_ledgers) deliberately STAY here -- they bind LOG_DIR
# path constants not yet extracted to a config module.
from webhook.timeutil import (  # noqa: E402  F401
    now_iso,
    parse_tv_time,
    latency_info,
    _epoch_from_iso,
)
from webhook.ledger_io import (  # noqa: E402  F401
    append_jsonl,
    append_jsonl_durable,
    read_jsonl_tolerant,
)
from webhook.config import (  # noqa: E402  F401
    load_engine_config,
    EXEC_BACKEND,
    EXECUTION_DEFAULTS,
    resolve_default_type,
    _env_bool,
)
# Phase 0: operator-alert emitters extracted to src/alerts.py. They read
# root-bound constants (ALERTS_LEDGER, QUEUE_SATURATION_ALERT_DEPTH, ...) lazily
# via `import webhook_receiver as _wr`, so those constants STAY defined here and
# tests keep monkeypatching them on wr. Re-export the emitters for call sites.
from alerts import (  # noqa: E402  F401
    emit_operator_alert,
    emit_auth_failure_alert,
    maybe_emit_queue_saturation_alert,
)
# Phase 1: signal dedupe + normalize/schema-validation extracted to
# src/signals/{dedupe,normalize}.py. The root-bound / monkeypatchable module
# state they read (_SIGNAL_DEDUPE_INDEX, _SIGNAL_DEDUPE_LOCK, SIGNALS_LEDGER,
# HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS, STRATEGIES, STRATEGY_ENGINE,
# ALERT_SCHEMA_PATH, _ALERT_SCHEMA_UNENFORCEABLE_ALERTED) STAYS defined here:
# tests monkeypatch it on wr, and the per-test importlib.reload of this module
# (tests/conftest.py `wr` fixture) must reset it -- state living in signals/
# would survive the reload and leak across tests. The moved functions read it
# lazily via `import webhook_receiver as _wr` (same pattern as src/alerts.py).
# Re-export so wr.<fn> call sites and monkeypatch seams keep working.
from signals.dedupe import (  # noqa: E402  F401
    dedupe_key,
    _signal_identity,
    stable_client_order_id,
    _dedupe_window_seconds,
    _load_signal_dedupe_index,
    check_and_mark_signal,
)
from signals.normalize import (  # noqa: E402  F401
    as_float,
    first,
    normalize,
    validate_strategy_alert,
    _alert_schema_validator,
    _alert_schema_enforcement_status,
    validate_alert_schema,
)
# Phase 2: control-state CRUD + atomic-write helpers extracted to
# src/control_state.py. The root-bound / reload-reset module state they read
# (CONTROL_STATE_FILE, _STATE_WRITE_LOCK, ALERTS_LEDGER) STAYS defined here:
# the per-test importlib.reload of this module (tests/conftest.py `wr` fixture)
# rebinds CONTROL_STATE_FILE into the tmp HERMX_ROOT, and the moved functions
# read it lazily via `import webhook_receiver as _wr` (same pattern as
# src/alerts.py / src/signals/). Re-export so wr.<fn> call sites and
# monkeypatch seams (e.g. test_intake_hardening patches wr._atomic_json_dump)
# keep working -- the order journal (Phase 4, still here) and record building
# call these through this module's globals.
from control_state import (  # noqa: E402  F401
    default_control_state,
    save_control_state,
    load_control_state,
    symbol_pause_info,
    pause_symbol,
    set_strategy_override,
    set_strategy_account,
    set_strategy_risk,
    clear_strategy_override,
    set_accounting_start,
    clear_accounting_start,
    accounting_start_for,
    set_trading_state,
    get_trading_state,
    clear_trading_state,
    _canonical_state_json,
    _fsync_dir,
    _atomic_json_dump,
    _fail_closed_state_write,
)
# Phase 3: strategy-record reads + execution readiness extracted to
# src/strategy/{records,readiness}.py. The root-bound / reload-reset module
# state they read (STRATEGIES_DIR, STRATEGIES) STAYS defined here: the
# per-test importlib.reload of this module (tests/conftest.py `wr` fixture)
# rebinds them into the tmp HERMX_ROOT, tests monkeypatch STRATEGIES_DIR on wr
# (test_phase5_normalization_cleanup) and inject into wr.STRATEGIES
# (test_phase_a_robustness setitem), so the moved functions read them lazily
# via `import webhook_receiver as _wr` (same pattern as src/alerts.py /
# src/signals/ / src/control_state.py). Re-export so wr.<fn> call sites and
# monkeypatch seams keep working. dashboard.py's D1 re-implementation
# (strategy_asset/load_strategy_files, param renamed) is deliberately
# untouched here -- reconciling it is deferred (REFACTOR_PLAN.md D1), same as
# Phase 2 deferred D2.
from strategy.records import (  # noqa: E402  F401
    strategy_instrument,
    _INSTRUMENT_TYPE_SUFFIXES,
    strategy_asset,
    strategy_budget_usd,
    normalize_strategy_record,
    load_strategy_files,
)
from strategy.readiness import (  # noqa: E402  F401
    _strategy_config_for_readiness,
    build_strategy_execution_readiness,
)
# Phase 4: order journal / submission-outcome state machine extracted to
# src/orders/journal.py. The root-bound / reload-reset module state it reads
# STAYS defined here: ORDER_JOURNAL_LEDGER / ORDER_JOURNAL_CHECKPOINT_FILE /
# LOG_DIR (rebound into the tmp HERMX_ROOT by the per-test importlib.reload of
# this module), HERMX_JOURNAL_SEGMENT_MAX_RECORDS (monkeypatched on wr by
# test_order_journal_checkpoint) + HERMX_JOURNAL_SEGMENT_RETENTION,
# _ORDER_JOURNAL_LOCK, and the reload-reset caches _order_journal_seq_cache /
# _order_journal_index (test_order_journal_checkpoint rebinds the index to
# None on wr). The moved functions read all of it lazily via `import
# webhook_receiver as _wr` -- the same pattern as src/alerts.py /
# src/signals/ / src/control_state.py / src/strategy/ -- and also call
# _read_order_journal_tail / append_jsonl_durable through _wr because tests
# rebind those seams on wr (_boom index proof, spy_durable write-ahead proof).
# Re-export so wr.<fn> call sites keep working -- reconcile (Phase 5, still
# here) calls load_open_orders / record_order_state / latest_order_record and
# execution/service.py resolves them from this module's globals via _h().
from orders.journal import (  # noqa: E402  F401
    ORDER_JOURNAL_SCHEMA_VERSION,
    ORDER_JOURNAL_CHECKPOINT_VERSION,
    ORDER_STATE_PLANNED,
    ORDER_STATE_SUBMITTED,
    ORDER_STATE_FILLED,
    ORDER_STATE_REJECTED,
    ORDER_STATE_UNKNOWN,
    ORDER_TERMINAL_STATES,
    ORDER_NON_TERMINAL_STATES,
    _ORDER_STATE_TRANSITIONS,
    order_state_can_transition,
    _read_order_journal_tail,
    _order_index_apply,
    _build_order_index,
    _order_index,
    _order_journal_next_seq,
    _parse_order_sealed_seq,
    _order_sealed_segment_paths,
    _read_all_order_records,
    _order_index_hash,
    _order_checkpoint_last_seq_floor,
    _read_order_checkpoint,
    _rotate_order_live_segment,
    _enforce_order_segment_retention,
    _order_checkpoint_and_rotate,
    _maybe_order_checkpoint_and_rotate,
    record_order_state,
    _order_intent_from_readiness,
    _cl_ord_id_from_readiness,
    load_open_orders,
    latest_order_record,
    order_history_for,
)
# Read-only order-journal troubleshooting (src/orders/troubleshoot.py). Classifies open
# UNKNOWN orders against known corruption/ambiguity patterns; served read-only via
# GET /api/admin/order-troubleshoot and applied (server-revalidated, never trusting a
# caller-supplied target state) via POST /api/admin/order-journal/apply-action below.
from orders.troubleshoot import (  # noqa: E402  F401
    run_classifiers,
    troubleshoot_all_open_orders,
)
# Phase 6: pre-execution advisor (advisory / risk gating) extracted to
# src/advisor.py. The config-bound / reload-reset module state it reads STAYS
# defined here: the HERMX_ADVISOR_* constants (resolved from the engine-config
# "advisor" block below; kept as wr module constants so tests can
# monkeypatch.setattr(wr, "HERMX_ADVISOR_ENABLED"/"HERMX_ADVISOR_COMMAND", ...)),
# plus the seams tests rebind on wr: _advisor_agent_query (test_phase8_advisor,
# test_phase6_advisor_execute), execute_if_enabled and record_pipeline_event
# (test_phase6_advisor_execute). The moved functions read all of them lazily
# via `import webhook_receiver as _wr` -- the same pattern as src/alerts.py /
# src/signals/ / src/control_state.py / src/strategy/ / src/orders/journal.py.
# Re-export so wr.<fn> call sites (build_record's execute_with_advisor call)
# and monkeypatch seams keep working. execute_if_enabled /
# _execute_authoritative (execution-service glue) deliberately stay here.
# Phase 5: the reconcile cluster was extracted to src/reconcile/. RECONCILE_STARTUP_
# COMPLETE/_AT, ROOT, ExecutorFactory, ALERTS_LEDGER, UNKNOWN_RESOLVER_*,
# PLANNED_ORDER_TIMEOUT_SECONDS, _RESOLVER_HEARTBEAT/_set_resolver_heartbeat, and
# unknown_resolver_enabled() STAY here (tests monkeypatch them on wr; per-test
# importlib.reload would not reset state living in a separate module). The new
# reconcile/* modules read/mutate that state lazily via `import webhook_receiver as
# _wr`, matching the Phase 0-4 pattern -- including _reconciliation_executor and
# reconcile_order_with_backoff/resolve_unknown_orders_once, which tests monkeypatch
# directly and expect same-package callers to observe via `_wr.` indirection.
from reconcile import (  # noqa: E402  F401
    _effective_execution_config,
    _reconciliation_executor,
    _executor_for_order,
    map_order_outcome,
    reconcile_order_once,
    reconcile_order_with_backoff,
    reconcile_startup,
    _order_age_seconds,
    _resolve_planned_orphan,
    resolve_unknown_orders_once,
    unknown_resolver_loop,
    reconcile_position_drift,
    emit_reconcile_alert,
    RECONCILE_ALERT_MISMATCH,
    RECONCILE_ALERT_RESOLVER_TIMEOUT,
    RECONCILE_ALERT_PLANNED_ABANDONED,
    RECONCILE_ALERT_PLANNED_ON_VENUE,
    RECONCILE_MAX_ATTEMPTS,
    RECONCILE_HISTORY_LIMIT,
)
from advisor import (  # noqa: E402  F401
    ADVISOR_SYSTEM_PROMPT,
    _advisor_state_snapshot,
    _advisor_build_prompt,
    _advisor_agent_query,
    _advisor_parse,
    run_execution_advisor,
    execute_with_advisor,
)

PORT = int(os.environ.get("HERMX_RECEIVER_PORT") or os.environ.get("SHADOW_PORT", "8891"))
# Address the HTTP server binds to. Default 127.0.0.1 keeps bare-host/systemd
# deploys loopback-only (unchanged behavior); the Docker bridge compose sets
# HERMX_BIND_HOST=0.0.0.0 so the container is reachable on the compose network.
HERMX_BIND_HOST = (os.environ.get("HERMX_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"
# Unified secret: HERMX_SECRET is the sole source authenticating both the webhook
# (X-Webhook-Secret) and the dashboard. Empty/missing => fail closed (every webhook
# gets 401; nothing is submitted).
SECRET = (os.environ.get("HERMX_SECRET") or "").strip()
HERMX_REQUIRE_HMAC = (os.environ.get("HERMX_REQUIRE_HMAC") or "false").strip().lower() in {"1", "true", "yes"}
HERMX_WEBHOOK_HMAC_KEY = (os.environ.get("HERMX_WEBHOOK_HMAC_KEY") or "").strip()
# Operational tuning constants (formerly HERMX_* env reads; hard-coded per the flag
# fluff audit -- no deployment tunes these). Kept as UPPER_SNAKE module constants so
# tests can monkeypatch.setattr(wr, "HERMX_X", ...) exactly as before.
HERMX_REPLAY_WINDOW_SECONDS = 300.0
HERMX_MAX_BODY_BYTES = 262144
HERMX_RATE_LIMIT_WINDOW_SECONDS = 60.0
HERMX_RATE_LIMIT_MAX_REQUESTS = 120
HERMX_QUEUE_MAXSIZE = 200
# --- Startup replay of unprocessed intake ------------------------------------
# On restart, intake rows fsync'd to raw-webhooks.jsonl but never dequeued from
# the in-memory PROCESS_QUEUE are lost (systemd Restart=always; TradingView
# does not retry). Replay re-queues only intake rows that are (a) recent,
# (b) not already processed, and (c) whose signal bar-time is still fresh.
# Dedupe (signals.jsonl) is the hard backstop against double-execution.
# Option A: drop any intake row whose payload lacks a TradingView time field --
# normalize() would otherwise fall back to now_iso() and mint a non-deterministic
# signal_id on replay, bypassing the dedupe ledger.
HERMX_REPLAY_ENABLED = (os.environ.get("HERMX_REPLAY_ENABLED") or "true").strip().lower() in {"1", "true", "yes"}
REPLAY_LOOKBACK_SECONDS = 300.0
REPLAY_MAX_TV_AGE_SECONDS = 120.0
HERMX_SUBMIT_TIMEOUT_SECONDS = 45.0
HERMX_WORKER_POOL_SIZE = 1
# B1 -- venue position-drift detection (observe-only). Default OFF: the receiver only
# runs the startup journal-vs-venue position audit when explicitly armed. Detection +
# alerting never auto-correct (reconcile_position_drift).
HERMX_POSITION_DRIFT_ENABLED = (os.environ.get("HERMX_POSITION_DRIFT_ENABLED") or "false").strip().lower() in {"1", "true", "yes"}
HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS = 86400.0
# Liveness watchdog: STALE_SECONDS is the single knob. <= 0 disables the watchdog
# entirely (merged the former HERMX_WATCHDOG_ENABLED bool into this per the flag audit).
HERMX_WATCHDOG_STALE_SECONDS = float(os.environ.get("HERMX_WATCHDOG_STALE_SECONDS", "120") or "120")
HERMX_QUEUE_LAG_SLO_SECONDS = 30.0
HERMX_REQUEST_TIMEOUT_SECONDS = 30.0
# Outbound operator-alert webhook timeout (URL itself stays env-facing at emit time
# -- URL presence is the real feature toggle; the timeout is not operator-tuned).
HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS = 2.0
ROOT = Path(os.environ.get("HERMX_ROOT") or Path(__file__).resolve().parents[1])
LOG_DIR = ROOT / "logs"
# Mutable per-process state snapshots live under DATA_DIR so they can be mapped
# to a dedicated, persistent location (a named volume under Docker) independent
# of the read-only config/strategies mounts. Default == ROOT, so bare-host
# deploys keep writing the four JSON files alongside the repo (unchanged).
DATA_DIR = Path(os.environ.get("HERMX_DATA_DIR", ROOT))
LATEST_FILE = DATA_DIR / "latest.json"
# --- Consolidated JSONL ledgers ------------------------------------------------
# raw-webhooks.jsonl  -- every inbound webhook, tagged with a ``phase`` field via
#   record_raw_webhook(): "intake" = raw HTTP receipt, "webhook" = post-normalization
#   outcome. Merges the former shadow-intake.jsonl + shadow-webhooks.jsonl.
# pipeline.jsonl      -- every signal-processing event, tagged with a ``stage`` field
#   via record_pipeline_event(). Merges shadow-decisions, strategy-alerts,
#   strategy-alert-quarantine, shadow-duplicates, advisor-decisions, paper-trades,
#   executions, and shadow-processing-errors.
# Both are size-rotated (see _rotate_ledger_if_large / HERMX_LEDGER_ROTATE_MAX_BYTES).
RAW_WEBHOOK_LEDGER = LOG_DIR / "raw-webhooks.jsonl"
PIPELINE_LEDGER = LOG_DIR / "pipeline.jsonl"
CONTROL_STATE_FILE = DATA_DIR / "control-state.json"
# Unified operator/reconcile/state alert ledger. Every alert row carries a ``kind``
# field ("operator", "reconcile", or "state") so the dashboard and operators can
# filter; this merges the former operator-alerts.jsonl, reconcile-alerts.jsonl, and
# state-alerts.jsonl. Fail-closed state-write errors (:221 -- a journal/checkpoint
# write that fails, e.g. ENOSPC) surface here as kind="state" AND re-raise so the
# money path is blocked rather than proceeding on lost state.
ALERTS_LEDGER = LOG_DIR / "alerts.jsonl"
# Rotate the live ORDER-journal segment into a sealed file once it reaches this many
# records, AFTER writing a verified checkpoint that subsumes them. Module constant
# (env-overridable) so a test can force a checkpoint+rotation without writing
# thousands of records.
HERMX_JOURNAL_SEGMENT_MAX_RECORDS = 1000
# Retention: keep the last K sealed segments for forensic replay. The verified
# checkpoint already subsumes every sealed segment (older sealed files are
# replay-unnecessary), so they are pruned beyond K. Set < 0 to keep all.
HERMX_JOURNAL_SEGMENT_RETENTION = 5
# Size-based rotation for the high-volume consolidated ledgers (pipeline.jsonl,
# raw-webhooks.jsonl). Unlike the position/order journals -- which rotate by record
# count behind a verified checkpoint -- these are append-only forensic logs with no
# checkpoint, so once the live file exceeds HERMX_LEDGER_ROTATE_MAX_BYTES it is sealed
# to ``<name>.<n>.jsonl`` (monotonic n) and a fresh live file is started. The last
# HERMX_LEDGER_ROTATE_RETENTION sealed segments are kept; older ones are pruned
# (set < 0 to keep all). Default 64 MiB keeps the bounded reverse-tail dashboard
# reads cheap while retaining ample history.
HERMX_LEDGER_ROTATE_MAX_BYTES = 64 * 1024 * 1024
HERMX_LEDGER_ROTATE_RETENTION = 5
# Execution outcomes are now recorded to the unified PIPELINE_LEDGER under
# stage="execution" (record_pipeline_event). The separate execution-plan.jsonl and
# executions.jsonl ledgers were retired in the JSONL ledger consolidation: the dead
# execution-plan write was already gone, and the executions outcome ledger folded
# into pipeline.jsonl. The dashboard reads the "execution" stage of pipeline.jsonl.
# Submission-outcome state machine + write-ahead order journal (REFACTOR_PLAN.md:204,
# :216 -- Phase 1 task 5). Append-only, durable (fsync) log of the lifecycle
# PLANNED -> SUBMITTED -> (FILLED | REJECTED | UNKNOWN). The PLANNED/SUBMITTED records
# are persisted BEFORE the submit subprocess so restart reconciliation (Task 4) has
# authoritative clOrdId keys even after a crash mid-send. UNKNOWN (timeout/crash) is a
# first-class state that triggers reconciliation, NOT a failure.
# (Schema/checkpoint versions + ORDER_STATE_* live in src/orders/journal.py --
# Phase 4 -- and are re-exported above; the two LOG_DIR-bound paths stay here.)
ORDER_JOURNAL_LEDGER = LOG_DIR / "order-journal.jsonl"
# Order-journal lifecycle (verified checkpoint + segment rotation, Phase 1 task 7).
# Without it _order_journal_next_seq() and latest_order_record() re-read the WHOLE
# append-only journal on every submit -- O(n) per order, unbounded growth. The
# checkpoint folds the journal into the bounded "latest record per cl_ord_id" index
# (the dedupe/idempotency authority) plus each order's origin ts, so a load rebuilds
# from (checkpoint + live-segment tail) instead of the full history, and rotation seals
# the live segment so disk does not grow without limit. The order journal is the
# submission state machine and is always active.
ORDER_JOURNAL_CHECKPOINT_FILE = LOG_DIR / "order-journal.checkpoint.json"
# Exchange reconciliation (REFACTOR_PLAN.md:208-215 -- Phase 1 task 4 + task 6).
# OBSERVE-ONLY in every form: reconciliation reads the venue and may update the
# local order journal and emit alerts, but it NEVER submits, cancels, or auto-trades.
# It consumes the Task-3 venue-neutral query interface and the Task-5 order journal.
# There are exactly THREE reconciliation paths, gated and wired independently:
#   1. STARTUP      reconcile_startup() -- runs once on boot (always; not flag-gated);
#                   recovers crash-orphaned SUBMITTED orders. Read-only single pass.
#   2. POST-SUBMIT  inline in ExecutionService.execute(), gated by
#                   reconcile_post_submit_enabled() (HERMX_RECONCILE_ENABLED, default
#                   OFF => byte-identical to pre-Task-4 stdout-driven outcome, :223).
#   3. PERIODIC     unknown_resolver_loop() -- a daemon thread polling every ~30s,
#                   gated by unknown_resolver_enabled() (UNKNOWN_RESOLVER_INTERVAL_SECONDS
#                   > 0, default ON); re-reconciles still-open SUBMITTED/UNKNOWN orders.
# "read-only" above means read-only against the EXCHANGE: all three may persist a
# legal SUBMITTED/UNKNOWN -> terminal transition to the local order journal.
#
# MONEY-SAFETY mapping (map_order_outcome): only a venue-confirmed canceled+zero-fill
# becomes REJECTED. Absence (not_found across get_order/pending/archive) maps to UNKNOWN,
# never REJECTED -- a missing order may have filled and aged out, so we keep tracking it
# rather than drop a possible live position as flat.
#
# UNKNOWN LIFECYCLE BACKSTOP (periodic resolver): an order whose age FROM ORIGIN exceeds
# HERMX_UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS (default 900s) is alerted
# (UNKNOWN_RESOLVER_TIMEOUT, severity=error) and its symbol is PAUSED. It is NEVER
# auto-closed -- ambiguity is not proof of any outcome. Alerts/pauses are deduped per
# (symbol, cl_ord_id, state) so a single stuck order does not re-fire every tick.
# RUNBOOK on a symbol pause: (1) inspect alerts.jsonl (kind in {reconcile, operator})
# for the cl_ord_id; (2) confirm the true order/position state on the venue UI/API;
# (3) reconcile the order journal to that truth; (4) clear the pause via the control
# state (symbol_pauses) once the symbol is safe to trade again. A paused symbol hard-
# blocks submission (symbol_pause_info gate in ExecutionService.execute).
# RECONCILE_ALERT_MISMATCH/RESOLVER_TIMEOUT/PLANNED_ABANDONED/PLANNED_ON_VENUE and
# RECONCILE_MAX_ATTEMPTS/BASE_DELAY/CAP_DELAY/WALL_CLOCK_BUDGET/HISTORY_LIMIT and
# _PRESENT_ORDER_STATES moved to src/reconcile/ (Phase 5) -- see the `from reconcile
# import (...)` re-export shim further down for wr.<name> compatibility.
# Concrete operator transport (Task 6): alerts are mirrored to the unified
# ALERTS_LEDGER (kind="operator") and optionally POSTed to an external webhook
# (HERMX_ALERT_WEBHOOK_URL).
ALERT_AUTH_FAILURE = "AUTH_FAILURE"
ALERT_QUEUE_SATURATION = "QUEUE_SATURATION"
# Task 6 periodic resolver controls.
UNKNOWN_RESOLVER_INTERVAL_SECONDS = 30.0
UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS = 900.0
UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK = 50
# PLANNED orphan backstop: a PLANNED order older than this (and unknown to the venue) is
# resolved PLANNED->REJECTED (never_submitted). Shorter than the SUBMITTED/UNKNOWN timeout
# because a PLANNED order was never sent -- there is no in-flight venue state to wait on,
# only the small window of an in-process submit between the PLANNED and SUBMITTED writes.
PLANNED_ORDER_TIMEOUT_SECONDS = 300.0
# Queue saturation signaling threshold for early warning; hard rejection now uses
# PROCESS_QUEUE.maxsize and returns 503 when full.
QUEUE_SATURATION_ALERT_DEPTH = 100
# Set once the one-time startup reconcile bootstrap finishes; exposed for FUTURE
# enforcement (Task 6 may disarm submission until this is True). In THIS task it is
# only set/logged -- it never hard-blocks the disabled/observe-only path.
RECONCILE_STARTUP_COMPLETE = False
RECONCILE_STARTUP_AT: "str | None" = None
# signals.jsonl is the SINGLE dedup authority (JSONL ledger consolidation): the
# in-memory dedupe index (_SIGNAL_DEDUPE_INDEX) is rebuilt from it on first use and
# every newly-seen signal is appended to it. The former seen-signals.json snapshot
# (a redundant second authority) was removed.
SIGNALS_LEDGER = LOG_DIR / "signals.jsonl"
# Engine + advisor config live in engine-config.json (leaf webhook/config.py);
# STRATEGY_ENGINE is sourced from it. The legacy shadow-config.json file and the
# CONFIG global (fees/funding/policies/execution/assets/risk) were removed entirely:
# execution backend is CCXT, fees/funding come from the venue, ALLOWED_SYMBOLS
# derives from strategy files, and the policy decision engine is dead.
ENGINE_CONFIG_FILE = ROOT / "engine-config.json"
ENGINE_CONFIG = load_engine_config(ENGINE_CONFIG_FILE)
STRATEGY_ENGINE = ENGINE_CONFIG.get("strategy_engine", {}) or {}
STRATEGIES_DIR = ROOT / str(STRATEGY_ENGINE.get("strategies_dir") or "strategies")

# Phase 8 pre-execution advisor (see engine-config "advisor" block). Env vars
# override config so an operator can flip it on a running VPS without editing JSON.
_ADVISOR_CFG = ENGINE_CONFIG.get("advisor", {}) or {}

# ENABLED stays env-overridable (single live-veto switch operators may flip on a
# running VPS). The sub-settings are NOT env-facing: they resolve from the
# engine-config "advisor" block, falling back to ADVISOR_DEFAULTS. Kept as module
# constants so a test can monkeypatch.setattr(wr, "HERMX_ADVISOR_COMMAND", ...).
HERMX_ADVISOR_ENABLED = _env_bool("HERMX_ADVISOR_ENABLED", bool(_ADVISOR_CFG.get("enabled", False)))
HERMX_ADVISOR_COMMAND = str(_ADVISOR_CFG.get("command") or "hermes")
HERMX_ADVISOR_SKILLS = str(_ADVISOR_CFG.get("skills") or "hermx-control")
HERMX_ADVISOR_MODEL = str(_ADVISOR_CFG.get("model") or "")
HERMX_ADVISOR_TIMEOUT_SECONDS = float(_ADVISOR_CFG.get("timeout_seconds") or 30.0)
# advisor-decisions, strategy-alerts, and strategy-alert-quarantine were folded into
# the unified PIPELINE_LEDGER (stages "advisor", "strategy_match", "quarantine").
# Phase 6 / M2 (REFACTOR_PLAN.md): explicit alert-schema enforcement at intake.
# The JSON schema lives in the source repo (NOT under HERMX_ROOT, which tests
# redirect to a temp dir), so resolve it relative to this file's repo root.
ALERT_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "tradingview-alert.schema.json"
# Observe-only counters for the alert-schema feature. Mutating a dict needs no
# `global` declaration, and adding/incrementing these never alters any ledger
# record or return value, so default-OFF behavior stays byte-identical.
ALERT_SCHEMA_METRICS = {"invalid": 0, "quarantined": 0}


# canonical_timeframe lives in the shared module so the receiver and the
# dashboard can never drift (Phase 4 / D8). Re-exported here so existing
# references (`canonical_timeframe(...)`) and importers keep working unchanged.
from hermx_shared import canonical_timeframe, live_trading_enabled  # noqa: E402,F401


# Strategy-record reads (strategy_instrument, strategy_asset, strategy_budget_usd,
# normalize_strategy_record, load_strategy_files and _INSTRUMENT_TYPE_SUFFIXES)
# moved to src/strategy/records.py (Phase 3); re-exported via the import shim at
# the top of this module. load_strategy_files reads STRATEGIES_DIR lazily through
# _wr, so the module-level STRATEGIES bind below keeps its import-time semantics.
STRATEGIES = load_strategy_files()

LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROCESS_QUEUE: queue.Queue[tuple] = queue.Queue(maxsize=max(1, HERMX_QUEUE_MAXSIZE))
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}
_STATE_WRITE_LOCK = threading.RLock()
_ORDER_JOURNAL_LOCK = threading.Lock()
_SIGNAL_DEDUPE_LOCK = threading.Lock()
_SIGNAL_DEDUPE_INDEX: dict[str, dict] = {"signals": {}, "keys": {}, "loaded": False}
_SYMBOL_LOCKS: dict[str, threading.Lock] = {}
_SYMBOL_LOCKS_LOCK = threading.Lock()
_SYMBOL_TICKET_LOCK = threading.Lock()
_SYMBOL_TICKET_TURN = threading.Condition()
_SYMBOL_TICKET_NEXT: dict[str, int] = {}
_SYMBOL_TICKET_RUN: dict[str, int] = {}
_SYMBOL_BURNED_TICKETS: set[tuple[str, int]] = set()  # (symbol, ticket) reserved but never enqueued (queue.Full); protected by _SYMBOL_TICKET_LOCK
_WORKER_HEARTBEATS: dict[str, float] = {}
_RESOLVER_HEARTBEAT: float | None = None
_WATCHDOG_LOCK = threading.Lock()
_WATCHDOG_SUBMISSION_PAUSED = False
_WATCHDOG_REASON = ""
_WATCHDOG_LAST_ALERTS: dict[str, float] = {}
_WORKER_NAMES: list[str] = []

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.FileHandler(LOG_DIR / "receiver.log"), logging.StreamHandler(sys.stdout)],
)
logging.Formatter.converter = lambda *_args: datetime.now(timezone.utc).timetuple()

sys.path.insert(0, "/root/trading-system")
# Ensure this src/ directory is importable so the executors package resolves
# regardless of the working directory the receiver is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Exchange-agnostic execution layer. The factory selects the right adapter from
# config["execution"]["exchange"] (okx_demo, kucoin_paper, bybit_testnet, ...).
try:
    from executors import ExecutorFactory
except Exception as exc:  # fail closed: execution simply stays disabled
    ExecutorFactory = None
    logging.warning("Executor factory unavailable: %s", exc)

try:
    from execution import ExecutionService
except Exception as exc:  # fail closed: execution simply stays disabled
    ExecutionService = None
    logging.warning("ExecutionService unavailable: %s", exc)

ALLOWED_SYMBOLS = frozenset(s.get("asset") for s in STRATEGIES.values() if s.get("asset"))


def webhook_auth_config_healthy() -> bool:
    return _webhook_auth_config_healthy(SECRET, HERMX_REQUIRE_HMAC, HERMX_WEBHOOK_HMAC_KEY)


def env_file_permissions_healthy(path: Path | None = None) -> bool:
    return _env_file_permissions_healthy(ROOT, path)


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    return _client_ip_impl(handler)


def _symbol_lock(symbol: str | None) -> threading.Lock:
    key = str(symbol or "").strip().upper() or "_UNKNOWN"
    with _SYMBOL_LOCKS_LOCK:
        lock = _SYMBOL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SYMBOL_LOCKS[key] = lock
        return lock


def _payload_symbol(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return "_UNKNOWN"
    symbol = payload.get("symbol") or payload.get("asset") or payload.get("ticker")
    return str(symbol or "").strip().upper() or "_UNKNOWN"


def _reserve_symbol_ticket(symbol: str | None) -> tuple[str, int]:
    key = str(symbol or "").strip().upper() or "_UNKNOWN"
    with _SYMBOL_TICKET_LOCK:
        ticket = int(_SYMBOL_TICKET_NEXT.get(key) or 0)
        _SYMBOL_TICKET_NEXT[key] = ticket + 1
        _SYMBOL_TICKET_RUN.setdefault(key, 0)
    return key, ticket


def _queue_work_item(payload: dict, intake_received_at: str) -> tuple[dict, str, str, int]:
    symbol, ticket = _reserve_symbol_ticket(_payload_symbol(payload))
    return payload, intake_received_at, symbol, ticket


def _symbol_ticket_is_turn(symbol: str, ticket: int) -> bool:
    with _SYMBOL_TICKET_TURN:
        return int(_SYMBOL_TICKET_RUN.get(symbol) or 0) == int(ticket)


def _drain_burned_tickets_locked(symbol: str) -> None:
    """Caller MUST hold _SYMBOL_TICKET_TURN. Advances RUN past any contiguous run
    of burned (reserved-but-never-enqueued) tickets starting at the current RUN."""
    with _SYMBOL_TICKET_LOCK:
        run = int(_SYMBOL_TICKET_RUN.get(symbol) or 0)
        advanced = False
        while (symbol, run) in _SYMBOL_BURNED_TICKETS:
            _SYMBOL_BURNED_TICKETS.discard((symbol, run))
            run += 1
            advanced = True
        if advanced:
            _SYMBOL_TICKET_RUN[symbol] = run


def _advance_symbol_ticket_turn(symbol: str, ticket: int) -> None:
    with _SYMBOL_TICKET_TURN:
        current = int(_SYMBOL_TICKET_RUN.get(symbol) or 0)
        if current <= int(ticket):
            _SYMBOL_TICKET_RUN[symbol] = int(ticket) + 1
        _drain_burned_tickets_locked(symbol)
        _SYMBOL_TICKET_TURN.notify_all()


def _burn_symbol_ticket(symbol: str, ticket: int) -> None:
    """Record a ticket that was reserved but never enqueued (queue.Full) and
    immediately drain it if RUN is already sitting on the hole."""
    key = str(symbol or "").strip().upper() or "_UNKNOWN"
    with _SYMBOL_TICKET_TURN:
        with _SYMBOL_TICKET_LOCK:
            _SYMBOL_BURNED_TICKETS.add((key, int(ticket)))
        _drain_burned_tickets_locked(key)
        _SYMBOL_TICKET_TURN.notify_all()


def _set_worker_heartbeat(name: str) -> None:
    _WORKER_HEARTBEATS[name] = time.time()


def _set_resolver_heartbeat() -> None:
    global _RESOLVER_HEARTBEAT
    _RESOLVER_HEARTBEAT = time.time()


def _watchdog_submission_state() -> tuple[bool, str]:
    with _WATCHDOG_LOCK:
        return (not _WATCHDOG_SUBMISSION_PAUSED), _WATCHDOG_REASON


def _set_watchdog_submission_paused(paused: bool, reason: str) -> None:
    global _WATCHDOG_SUBMISSION_PAUSED, _WATCHDOG_REASON
    with _WATCHDOG_LOCK:
        _WATCHDOG_SUBMISSION_PAUSED = bool(paused)
        _WATCHDOG_REASON = str(reason or "")


def _maybe_watchdog_alert(kind: str, payload: dict, *, severity: str = "error", cooldown_s: float = 60.0) -> None:
    now = time.time()
    last = float(_WATCHDOG_LAST_ALERTS.get(kind) or 0.0)
    if now - last < max(1.0, cooldown_s):
        return
    _WATCHDOG_LAST_ALERTS[kind] = now
    emit_operator_alert(kind, payload, severity=severity)


def _queue_oldest_age_seconds() -> float:
    try:
        with PROCESS_QUEUE.mutex:
            if not PROCESS_QUEUE.queue:
                return 0.0
            oldest = PROCESS_QUEUE.queue[0]
        if not isinstance(oldest, tuple) or len(oldest) < 2:
            return 0.0
        enq_dt = parse_tv_time(oldest[1])
        if enq_dt is None:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - enq_dt).total_seconds())
    except Exception:
        return 0.0


def liveness_watchdog_loop(stop_event: "threading.Event | None" = None, sleep=time.sleep) -> None:
    # STALE_SECONDS <= 0 disables the watchdog. Short-circuit BEFORE the max(5.0, ...)
    # floor below so 0 means "off", not "everything is instantly stale".
    if HERMX_WATCHDOG_STALE_SECONDS <= 0:
        return
    interval = max(1.0, min(10.0, HERMX_WATCHDOG_STALE_SECONDS / 4.0))
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        now = time.time()
        stale_seconds = max(5.0, HERMX_WATCHDOG_STALE_SECONDS)
        stale_workers = [name for name in _WORKER_NAMES if (now - float(_WORKER_HEARTBEATS.get(name) or 0.0)) > stale_seconds]
        resolver_stale = unknown_resolver_enabled() and _RESOLVER_HEARTBEAT is not None and (now - _RESOLVER_HEARTBEAT) > stale_seconds
        oldest_lag_s = _queue_oldest_age_seconds()
        lag_breached = oldest_lag_s > max(1.0, HERMX_QUEUE_LAG_SLO_SECONDS)
        degraded = bool(stale_workers or resolver_stale or lag_breached)
        if degraded:
            reason = "watchdog_degraded"
            details = {
                "stale_workers": stale_workers,
                "resolver_stale": bool(resolver_stale),
                "oldest_queue_lag_s": round(oldest_lag_s, 3),
                "queue_lag_slo_s": HERMX_QUEUE_LAG_SLO_SECONDS,
                "stale_threshold_s": stale_seconds,
            }
            _set_watchdog_submission_paused(True, reason)
            _maybe_watchdog_alert("WATCHDOG_DEGRADED", details, severity="error", cooldown_s=30.0)
        else:
            was_ok, _ = _watchdog_submission_state()
            if not was_ok:
                _set_watchdog_submission_paused(False, "")
                _maybe_watchdog_alert("WATCHDOG_RECOVERED", {"recovered": True}, severity="info", cooldown_s=30.0)
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            sleep(interval)


def _rate_limit_key(handler: BaseHTTPRequestHandler) -> str:
    # Only trust spoofable forwarding headers (CF-Connecting-IP / X-Forwarded-For)
    # when bound OFF-HOST (behind a reverse proxy). On loopback there is no proxy, so
    # an attacker could otherwise mint a fresh rate-limit bucket per request via a
    # forged header. _LOOPBACK_BIND_HOSTS is module-level and resolved at call time.
    trust = str(HERMX_BIND_HOST or "").strip().lower() not in _LOOPBACK_BIND_HOSTS
    return _rate_limit_key_impl(handler, trust_forwarding=trust)


def rate_limit_allow(source_key: str, now_seconds: float | None = None) -> tuple[bool, dict]:
    return _rate_limit_allow_impl(
        source_key,
        _RATE_LIMIT_BUCKETS,
        _RATE_LIMIT_LOCK,
        HERMX_RATE_LIMIT_WINDOW_SECONDS,
        HERMX_RATE_LIMIT_MAX_REQUESTS,
        now_seconds=now_seconds,
    )


def _parse_replay_timestamp(value: str) -> float | None:
    return _parse_replay_timestamp_impl(value, parse_tv_time)


def compute_webhook_hmac(timestamp: str, body: bytes, key: str) -> str:
    return _compute_webhook_hmac_impl(timestamp, body, key)


def verify_webhook_hmac(headers, body: bytes, now_seconds: float | None = None) -> tuple[bool, str]:
    return _verify_webhook_hmac_impl(
        headers,
        body,
        HERMX_REQUIRE_HMAC,
        HERMX_WEBHOOK_HMAC_KEY,
        HERMX_REPLAY_WINDOW_SECONDS,
        _parse_replay_timestamp,
        compute_webhook_hmac,
        now_seconds=now_seconds,
    )


def authenticate_webhook_request(
    handler: BaseHTTPRequestHandler, body: bytes, payload: dict | None = None
) -> tuple[bool, int, str]:
    # ``secret_key`` in the JSON body is the DEFAULT transport for direct
    # TradingView-native alerts (their webhook action cannot send custom headers);
    # the ``X-Webhook-Secret`` header remains for relay/proxy setups. See
    # authenticate_webhook_request() in security/webhook_auth.py for the precedence.
    return _authenticate_webhook_request_impl(
        handler,
        body,
        SECRET,
        _client_ip,
        lambda headers, request_body: verify_webhook_hmac(headers, request_body),
        emit_auth_failure_alert,
        body_secret=(payload or {}).get("secret_key"),
    )


# Signal dedupe / idempotency cluster (dedupe_key, _signal_identity,
# stable_client_order_id, _dedupe_window_seconds, _load_signal_dedupe_index,
# check_and_mark_signal) moved to src/signals/dedupe.py (Phase 1); re-exported
# via the import shim at the top of this module.


# --- Consolidated-ledger writers + size rotation ------------------------------
# Valid ``stage`` values for record_pipeline_event(). Every signal-processing event
# is one row in pipeline.jsonl tagged with one of these stages; the dashboard filters
# by stage. ("tab_health" is reserved -- tab-health.jsonl is produced out-of-process
# and is NOT written here; see TAB_HEALTH_LEDGER. "intake" mirrors the raw-webhook
# phase for callers that want a pipeline-side marker.)
PIPELINE_STAGES = frozenset({
    "intake", "dedup_reject", "strategy_match", "quarantine", "decision",
    "advisor", "paper_trade", "execution", "error", "tab_health",
    "startup_replay",
})
# Valid ``phase`` values for record_raw_webhook() (raw-webhooks.jsonl).
#   "intake"  = raw HTTP receipt (accepted, queued)
#   "webhook" = post-normalization outcome (dequeued + processed)
#   "dropped" = terminal: accepted-to-WAL but never queued (e.g. queue full → 503),
#               so replay must NOT resurrect it.
RAW_WEBHOOK_PHASES = frozenset({"intake", "webhook", "dropped"})


def _next_sealed_ledger_index(path: Path) -> int:
    """Next monotonic seal index for ``<stem>.<n>.jsonl`` sealed segments of *path*."""
    stem, suffix = path.stem, path.suffix
    max_n = -1
    for p in path.parent.glob(f"{stem}.*{suffix}"):
        mid = p.name[len(stem) + 1 : len(p.name) - len(suffix)]
        if mid.isdigit():
            max_n = max(max_n, int(mid))
    return max_n + 1


def _prune_sealed_ledgers(path: Path, retention: int) -> None:
    """Keep the last ``retention`` sealed ``<stem>.<n>.jsonl`` segments (retention < 0
    keeps all; retention == 0 prunes all)."""
    if retention < 0:
        return
    stem, suffix = path.stem, path.suffix
    sealed: list[tuple[int, Path]] = []
    for p in path.parent.glob(f"{stem}.*{suffix}"):
        mid = p.name[len(stem) + 1 : len(p.name) - len(suffix)]
        if mid.isdigit():
            sealed.append((int(mid), p))
    sealed.sort()
    doomed = sealed if retention == 0 else sealed[:-retention]
    for _n, p in doomed:
        try:
            p.unlink()
        except OSError:
            pass


def _rotate_ledger_if_large(path: Path, max_bytes: int | None = None, retention: int | None = None) -> None:
    """Size-based rotation for the append-only consolidated ledgers. Once *path* is at
    or above ``max_bytes`` it is sealed to ``<stem>.<n>.jsonl`` and a fresh live file is
    started by the next append. Best-effort: any failure leaves the live file in place
    (never loses data) and only logs."""
    max_bytes = HERMX_LEDGER_ROTATE_MAX_BYTES if max_bytes is None else max_bytes
    retention = HERMX_LEDGER_ROTATE_RETENTION if retention is None else retention
    if max_bytes <= 0:
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < max_bytes:
        return
    sealed = path.parent / f"{path.stem}.{_next_sealed_ledger_index(path)}{path.suffix}"
    try:
        os.replace(path, sealed)
    except OSError as exc:
        logging.warning("ledger rotation failed for %s: %s", path, exc)
        return
    _prune_sealed_ledgers(path, retention)


def _signal_id_of(record: dict | None) -> str | None:
    """Best-effort signal-id correlation key from a processing record."""
    norm = (record or {}).get("normalized") or {}
    return norm.get("signal_id") or None


def _scrub_secret_key(obj, _depth: int = 0) -> None:
    """Belt-and-suspenders: strip any ``secret_key`` field before a record is
    persisted, at any nesting depth (top level, ``payload``, ``extras``,
    ``normalized`` …). The primary redaction happens once at intake (do_POST) via
    object identity, so on the live path this is a no-op; it exists so a FUTURE new
    persist call site cannot reintroduce a secret leak into a durable ledger. Depth
    is bounded so a pathological structure can never make logging loop forever.
    Mutates ``obj`` in place (records are freshly built dicts; the only removed key
    is the secret itself, so this never drops legitimate data)."""
    if _depth > 6 or obj is None:
        return
    if isinstance(obj, dict):
        obj.pop("secret_key", None)
        for value in obj.values():
            _scrub_secret_key(value, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _scrub_secret_key(value, _depth + 1)


def record_pipeline_event(stage: str, signal_id: str | None, payload: dict | None = None, *, durable: bool = False) -> None:
    """Append one signal-processing event to the unified pipeline ledger.

    Every event is ``{ts, stage, signal_id, **payload}``. ``stage`` identifies the
    pipeline phase (see PIPELINE_STAGES); the dashboard filters by it. ``payload`` is
    spread at top level so the existing row shapes (full decision/strategy records,
    execution outcomes, trades, advisor decisions, errors) are preserved verbatim --
    only ``stage``/``signal_id``/``ts`` are stamped on top. The rotation check runs
    after the durable append so a write is never lost to rotation."""
    if stage not in PIPELINE_STAGES:  # pragma: no cover - guard against typos
        logging.warning("record_pipeline_event: unknown stage %r", stage)
    record = {"ts": now_iso(), "stage": stage, "signal_id": signal_id or None}
    if payload:
        for key, value in payload.items():
            if key not in ("ts", "stage", "signal_id"):
                record[key] = value
    # Observe-only: surface the alert's optional ``extras`` debugging context at the
    # event top level so operators can grep it out of pipeline.jsonl without digging
    # into the nested ``normalized`` block. Never affects execution.
    if "extras" not in record and payload:
        norm = payload.get("normalized")
        if isinstance(norm, dict) and isinstance(norm.get("extras"), dict):
            record["extras"] = norm["extras"]
    _scrub_secret_key(record)
    (append_jsonl_durable if durable else append_jsonl)(PIPELINE_LEDGER, record)
    _rotate_ledger_if_large(PIPELINE_LEDGER)


def record_raw_webhook(phase: str, payload: dict) -> None:
    """Append one inbound-webhook row to the unified raw-webhooks ledger, tagged with a
    ``phase`` field ("intake" = raw HTTP receipt; "webhook" = post-normalization)."""
    if phase not in RAW_WEBHOOK_PHASES:  # pragma: no cover - guard against typos
        logging.warning("record_raw_webhook: unknown phase %r", phase)
    record = {"phase": phase}
    record.update(payload or {})
    _scrub_secret_key(record)
    append_jsonl(RAW_WEBHOOK_LEDGER, record)
    _rotate_ledger_if_large(RAW_WEBHOOK_LEDGER)


def startup_quarantine_partial_ledgers(paths: "list[Path] | tuple[Path, ...] | None" = None) -> dict:
    """Startup sweep for trailing partial JSONL lines (Task 2 remainder / :206).

    Uses read_jsonl_tolerant() across runtime ledgers so crash-truncated tails are
    quarantined into ``*.corrupt`` sidecars instead of blowing up readers.
    """
    scan_paths = list(paths) if paths is not None else [
        RAW_WEBHOOK_LEDGER,
        PIPELINE_LEDGER,
        ORDER_JOURNAL_LEDGER,
        ALERTS_LEDGER,
        SIGNALS_LEDGER,
    ]
    summary = {"checked": 0, "quarantined": [], "errors": []}
    for path in scan_paths:
        if not path.exists():
            continue
        summary["checked"] += 1
        corrupt_path = path.parent / (path.name + ".corrupt")
        before_mtime = corrupt_path.stat().st_mtime_ns if corrupt_path.exists() else None
        try:
            read_jsonl_tolerant(path)
        except Exception as exc:
            summary["errors"].append(f"{path.name}: {exc}")
            continue
        after_mtime = corrupt_path.stat().st_mtime_ns if corrupt_path.exists() else None
        if after_mtime is not None and after_mtime != before_mtime:
            summary["quarantined"].append(path.name)
    return summary


def _has_time_field(payload: dict) -> bool:
    """Return True if the payload carries a TradingView bar time we can use
    for replay freshness and dedupe. Option A: we drop any intake row whose
    payload lacks a time field, because normalize() would fall back to now_iso()
    and produce a non-deterministic signal_id on replay."""
    return bool(
        first(payload, "tv_time", "time", "timestamp", "bar_time", "candle_time")
    )


# Normalize + strategy/schema validation cluster (as_float, first, normalize,
# validate_strategy_alert, _alert_schema_validator, _alert_schema_enforcement_status,
# validate_alert_schema and the _ALERT_SCHEMA_* validator cache) moved to
# src/signals/normalize.py (Phase 1); re-exported via the import shim at the
# top of this module. _has_time_field stays here (replay-owned).

# Set once we have warned that enforcement is armed but unenforceable -- the alert is a
# config-level condition, so a single operator alert per process suffices (no per-webhook
# spam). Reset by tests via monkeypatch. Stays defined HERE (not signals/normalize.py)
# so the per-test importlib.reload(wr) resets it; signals.normalize reads/writes it
# lazily through _wr.
_ALERT_SCHEMA_UNENFORCEABLE_ALERTED = False


# Control-state CRUD cluster (default_control_state, save/load_control_state,
# symbol_pause_info, pause_symbol, set/clear_strategy_override,
# set/clear_accounting_start, accounting_start_for, set/get/clear_trading_state
# and the _VALID_*/_STRATEGY_MODE_* constants) moved to src/control_state.py
# (Phase 2); re-exported via the import shim at the top of this module.
# _strategy_config_for_readiness moved to src/strategy/readiness.py (Phase 3);
# it reads STRATEGIES lazily through _wr, so the wr.STRATEGIES setitem seam
# (test_phase_a_robustness) keeps working.


# Atomic-write helpers (_canonical_state_json, _fsync_dir, _atomic_json_dump,
# _fail_closed_state_write) moved to src/control_state.py (Phase 2); re-exported
# via the import shim at the top of this module. The order journal (Phase 4,
# still below) and record building resolve them through this module's globals,
# so wr._atomic_json_dump monkeypatch seams keep working.


# build_strategy_execution_readiness moved to src/strategy/readiness.py
# (Phase 3); re-exported via the import shim at the top of this module.


# ``live_trading_enabled`` (the global HERMX_LIVE_TRADING kill switch) now lives in
# ``hermx_shared`` -- a pure env read with no module state -- and is imported above so
# existing references (`live_trading_enabled(...)`) keep working unchanged. It is wired
# into the ExecutionService gate for ``execution_mode == "live"`` submissions.


# ---------------------------------------------------------------------------
# Submission-outcome state machine + write-ahead order journal -- moved to
# src/orders/journal.py (Phase 4) and re-exported from the import block near
# the top of this module. Only the reload-reset caches below stay here: the
# per-test importlib.reload of this module resets them, and the moved
# functions read/write them through `import webhook_receiver as _wr`.
# ---------------------------------------------------------------------------

# Monotonic seq for the ORDER journal. Derived once from the checkpoint floor +
# sealed-segment seqs + live tail at first use, then incremented in-process; reset to
# None on module (re)load. Survives rotation+restart because the floor folds in the
# checkpoint and the sealed filenames (encoded seq, no file read).
_order_journal_seq_cache: "int | None" = None

# In-memory index rebuilt once (lazily) from the bounded tail and updated on every
# append, so latest_order_record() / load_open_orders() never re-read the whole journal
# on the submit hot path. ``latest`` = newest record per cl_ord_id (ALL states -- the
# idempotency/dedupe authority); ``origin`` = (seq, ts) of each order's FIRST record so
# the lifecycle backstop measures age from origin, never reset by re-recording. None
# until built; reset to None on module (re)load.
_order_journal_index: "dict | None" = None


# ---------------------------------------------------------------------------
# Exchange reconciliation (REFACTOR_PLAN.md:208-215 -- Phase 1 task 4, OBSERVE-ONLY).
#
# Reconciliation CONSUMES the Task-3 query interface (executor.get_order /
# get_open_orders / get_order_history_archive / get_positions) and the Task-5 order
# journal. It maps the exchange's truth onto the submission-outcome state machine and,
# at most, (a) updates terminal order-journal states and (b) emits RECONCILE_MISMATCH
# alerts. It NEVER submits, cancels, or auto-trades (:215 "does not auto-trade").
# Periodic UNKNOWN/SUBMITTED re-reconciliation and per-symbol pause controls live in
# resolve_unknown_orders_once()/unknown_resolver_loop() below (Task 6).
# ---------------------------------------------------------------------------

# _reconcile_float moved to src/reconcile/orders.py (Phase 5).
# Shared truthiness for the reconciliation feature flags so all three paths gate on
# IDENTICAL rules and differ only by their documented default. A value in the falsey
# set (or empty) disables; anything else enables. ``default`` is returned only when
# the variable is UNSET, so every call site declares its own default explicitly
# (post-submit OFF, periodic resolver ON) rather than burying it in get(name, "1").
_RECONCILE_FLAG_FALSEY = frozenset({"false", "0", "no", ""})


def _reconcile_flag_enabled(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _RECONCILE_FLAG_FALSEY


def reconcile_post_submit_enabled() -> bool:
    """Observe-only soak gate for the POST-SUBMIT (inline) reconciliation path (:223).

    ``HERMX_RECONCILE_ENABLED`` unset/falsey => OFF (the submit path stays
    byte-identical to pre-Task-4: stdout drives the tentative terminal record, no
    query subprocess is spawned). Truthy => the exchange drives the authoritative
    SUBMITTED->terminal transition (:214 "never trust stdout alone"). The STARTUP and
    PERIODIC paths are gated separately; startup runs regardless of this flag.
    """
    return _reconcile_flag_enabled("HERMX_RECONCILE_ENABLED", default=False)


# map_order_outcome / _order_is_present / _order_matches / reconcile_order_once /
# reconcile_order_with_backoff moved to src/reconcile/orders.py (Phase 5).
# emit_operator_alert / emit_auth_failure_alert / maybe_emit_queue_saturation_alert
# were extracted to src/alerts.py (Phase 0) and are re-exported at the top of this
# module. They read ALERTS_LEDGER / HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS /
# ALERT_AUTH_FAILURE / ALERT_QUEUE_SATURATION / QUEUE_SATURATION_ALERT_DEPTH (still
# defined here) lazily via `import webhook_receiver as _wr`.


# emit_reconcile_alert / reconcile_position_drift / _effective_execution_config /
# _reconciliation_executor / _executor_for_order / reconcile_startup moved to
# src/reconcile/ (Phase 5). ROOT / ExecutorFactory / ALERTS_LEDGER stay here and are
# read lazily via `import webhook_receiver as _wr` from those modules.


def unknown_resolver_enabled() -> bool:
    """Observe-only gate for the PERIODIC background resolver (unknown_resolver_loop).

    Defaults ON. INTERVAL_SECONDS is the single knob (merged the former
    HERMX_UNKNOWN_RESOLVER_ENABLED bool per the flag audit): <= 0 disables the daemon
    thread. Like the other two paths it only updates the order journal / emits alerts
    and never submits, cancels, or auto-trades.
    """
    return UNKNOWN_RESOLVER_INTERVAL_SECONDS > 0


# _order_age_seconds / _resolve_planned_orphan / resolve_unknown_orders_once /
# unknown_resolver_loop moved to src/reconcile/unknown_resolver.py (Phase 5).
# UNKNOWN_RESOLVER_*/PLANNED_ORDER_TIMEOUT_SECONDS/_RESOLVER_HEARTBEAT/
# _set_resolver_heartbeat stay here and are read/called lazily via
# `import webhook_receiver as _wr` from that module.


def _record_execution_outcome(_ledger, row: dict) -> None:
    """ExecutionService hook adapter. The service performs exactly one ledger write --
    ``append_jsonl(execution_ledger, {received_at, okx_execution})`` -- which we route
    into the unified pipeline ledger as stage="execution". The ``_ledger`` handle is
    accepted for the legacy hook signature and ignored."""
    record_pipeline_event("execution", None, row)


def _run_execution_service(record: dict, *, journal_hook=None) -> dict:
    """Construct the controlled ExecutionService with the standard money-safety hook
    wiring and run one submission. ``journal_hook`` overrides the append_jsonl hook so
    a caller (e.g. the operator-close path) can stamp extra audit fields on the
    journaled execution row; it defaults to the plain pipeline outcome writer."""
    service = ExecutionService(
        config=_effective_execution_config(),
        root=ROOT,
        executor_factory=ExecutorFactory,
        submit_timeout_seconds=HERMX_SUBMIT_TIMEOUT_SECONDS,
        hooks={
            "append_jsonl": journal_hook or _record_execution_outcome,
            "execution_ledger": PIPELINE_LEDGER,
            "webhook_auth_config_healthy": webhook_auth_config_healthy,
            "watchdog_submission_state": _watchdog_submission_state,
            "live_trading_enabled": live_trading_enabled,
            "symbol_pause_info": symbol_pause_info,
            # Phase A gates: A1 reads the strategy capital cap; A2 reads trading_state.
            "strategy_config_lookup": _strategy_config_for_readiness,
            "trading_state": get_trading_state,
            "order_intent_from_readiness": _order_intent_from_readiness,
            "cl_ord_id_from_readiness": _cl_ord_id_from_readiness,
            "latest_order_record": latest_order_record,
            "record_order_state": record_order_state,
            "fail_closed_state_write": _fail_closed_state_write,
            "order_state_planned": ORDER_STATE_PLANNED,
            "order_state_submitted": ORDER_STATE_SUBMITTED,
            "order_state_filled": ORDER_STATE_FILLED,
            "order_state_rejected": ORDER_STATE_REJECTED,
            "order_state_unknown": ORDER_STATE_UNKNOWN,
            "reconcile_post_submit_enabled": reconcile_post_submit_enabled,
            "reconciliation_executor": _reconciliation_executor,
            "reconcile_order_with_backoff": reconcile_order_with_backoff,
            "order_state_can_transition": order_state_can_transition,
            "emit_reconcile_alert": emit_reconcile_alert,
            "reconcile_alert_mismatch": RECONCILE_ALERT_MISMATCH,
            "redact_secrets": redact_secrets,
        },
    )
    return service.execute(record)


def _execute_via_service(record: dict) -> dict:
    return _run_execution_service(record)


# ---------------------------------------------------------------------------
# Operator-instructed close (POST /api/close).
# A close is a RISK-REDUCING flatten an operator triggers out-of-band (e.g. via
# Telegram). It routes through the SAME controlled ExecutionService as a normal
# submit, so the write-ahead journal, idempotency, auth-health and watchdog gates
# all still run. Two gates are deliberately bypassed via the readiness
# ``close_only`` flag (see ExecutionService.execute): the global kill switch and
# the per-symbol pause. Both exist to stop NEW risk; a close only reduces it, and
# blocking it would trap an operator who needs to flatten during exactly the state
# those gates flag. The single per-strategy submit_orders flag still arms it.
# ---------------------------------------------------------------------------


def _operator_close_cl_ord_id(symbol: str, strategy_id: str) -> str:
    """Per-(symbol, strategy) close id at 1-second granularity, OKX-safe.

    ``opcls`` + truncated sha256 hex of ``{symbol}_{strategy_id}_{YYYYMMDD}_{HHMMSS}``
    (same collision domain as the legacy readable id, same pattern as
    ``signals.dedupe.stable_client_order_id``): alphanumeric-only and 32 chars, per
    OKX's clientOrderId spec — the legacy underscore id was passed raw to the venue.
    Idempotent within the same UTC second: an accidental resubmit hashes to the same
    id, collides on the order-journal dedupe key and is refused
    ``duplicate_cl_ord_id``; two DISTINCT closes for the same symbol/strategy later
    the same day get different ids. The hash is not invertible, so attribution of
    new closes relies entirely on the submit-time map (``pnl_strategy_map``, written
    in ``ExecutionService.execute``); the legacy-id parser
    (``pnl_ledger._parse_operator_close_strategy_id``) remains only for historical
    ledger rows. Symbol/strategy/operator/reason stay human-readable in the
    pipeline-event journal row (``journal_extra`` below)."""
    now = datetime.now(timezone.utc)
    basis = f"{symbol}_{strategy_id}_{now.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"opcls{digest}"[:32]


def build_operator_close_readiness(symbol: str, strategy: dict, cl_ord_id: str) -> dict:
    """Readiness for an operator close. Mirrors the live values the signal path
    resolves (execution_mode, submit_orders, sandbox, instrument) but carries
    ``close_only=True`` and a CLOSE_LONG/CLOSE_SHORT intent so the adapter flattens
    whichever side is currently open (reduceOnly), and no new position is opened."""
    strategy = strategy or {}
    strategy_id = str(strategy.get("strategy_id") or "")
    execution_mode = str(strategy.get("execution_mode") or "demo").lower()
    # Honor the same live control-state override the signal path checks, so an
    # operator who has flipped mode/arming in the dashboard sees it reflected here.
    _cs_ov = (load_control_state().get("strategy_overrides") or {}).get(strategy_id)
    if isinstance(_cs_ov, dict) and _cs_ov.get("execution_mode"):
        execution_mode = str(_cs_ov["execution_mode"]).lower()
    # Both demo and live submit orders; the difference is sandbox vs real account.
    submit_orders = True
    sandbox = execution_mode != "live"  # demo -> True; live -> False
    instrument = strategy_instrument(strategy)
    margin_mode = strategy.get("margin_mode", "isolated")
    return {
        "mode": "operator_close",
        # The flag that bypasses gate 3 (kill switch) and the symbol pause; see
        # ExecutionService.execute. Every other gate still applies.
        "close_only": True,
        "live_execution_enabled": submit_orders,
        "execution_mode": execution_mode,
        "simulated_trading": sandbox,
        "exchange": EXEC_BACKEND,
        "symbol": symbol,
        "inst_id": instrument.get("inst_id"),
        "instrument": instrument,
        "strategy_id": strategy_id,
        "asset": strategy.get("asset") or symbol,
        "margin_mode": margin_mode,
        "td_mode": margin_mode,
        "leverage": strategy.get("leverage"),
        "signal_side": None,
        "execution_intent": {
            "policy": f"operator_close:{strategy_id}",
            "decision": "CLOSE",
            # Emit both close legs; the adapter closes whichever side is open and
            # skips the other (reduceOnly on the close that fires).
            "actions": ["CLOSE_LONG", "CLOSE_SHORT"],
            "reduce_only": True,
            "client_order_id": cl_ord_id,
            "client_order_id_close": cl_ord_id,
        },
        "okx_fill": {"client_order_id": cl_ord_id},
        "block_reason": None,
    }


def execute_operator_close(symbol: str, strategy: dict, *, operator=None, reason=None) -> dict:
    """Build a close readiness record and submit it through the controlled service.
    Returns the service result with the deterministic ``cl_ord_id`` and the close
    ``submitted_at`` timestamp attached (underscore-prefixed, so they never collide
    with adapter result keys). The journaled execution row is stamped with
    ``kind="operator_close"`` plus the operator/reason audit trail."""
    strategy_id = str((strategy or {}).get("strategy_id") or "")
    cl_ord_id = _operator_close_cl_ord_id(symbol, strategy_id)
    submitted_at = now_iso()
    readiness = build_operator_close_readiness(symbol, strategy, cl_ord_id)
    record = {"received_at": submitted_at, "execution_readiness": readiness}

    journal_extra = {
        "kind": "operator_close",
        "operator": operator,
        "reason": reason,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "cl_ord_id": cl_ord_id,
    }

    def _journal(_ledger, row: dict) -> None:
        record_pipeline_event("execution", None, {**row, **journal_extra})

    if (
        ExecutionService is None
        or ExecutorFactory is None
        or not ExecutorFactory.available()
    ):
        result = {"ok": True, "mode": "not_submitted", "reason": "execution_unavailable"}
        _journal(None, {"received_at": submitted_at, "okx_execution": result})
    else:
        result = _run_execution_service(record, journal_hook=_journal)
    return {**result, "_cl_ord_id": cl_ord_id, "_submitted_at": submitted_at}


def execute_if_enabled(record: dict) -> dict:
    """Authoritative submission entry point: route through ExecutionService (CCXT)."""
    return _execute_authoritative(record)


def _execute_authoritative(record: dict) -> dict:
    """Authoritative submission. Post P5-06/P5-07 cutover this ALWAYS routes through
    ExecutionService, whose sole executor backend is CCXT. The legacy inline okx_demo
    subprocess path was deleted.

    FAIL CLOSED: if the controlled execution surface is unavailable (ExecutionService
    or ExecutorFactory failed to import, or no executor backend is registered because
    the optional ``ccxt`` dependency is missing), we NEVER submit. We return a
    not_submitted/execution_unavailable outcome and append it to the execution ledger,
    exactly like a blocked gate -- no order, no order-journal PLANNED/SUBMITTED writes.
    """
    if (
        ExecutionService is None
        or ExecutorFactory is None
        or not ExecutorFactory.available()
    ):
        result = {
            "ok": True,
            "mode": "not_submitted",
            "reason": "execution_unavailable",
        }
        try:
            record_pipeline_event("execution", _signal_id_of(record), {"received_at": record.get("received_at"), "strategy_id": (record.get("execution_readiness") or {}).get("strategy_id") or None, "okx_execution": result})
        except Exception as exc:  # pipeline ledger is observability; must never block the outcome
            logging.warning("execution ledger append failed: %s", exc)
        return result
    return _execute_via_service(record)


# --- Phase 8: optional pre-execution advisor -------------------------------
# The advisor cluster (ADVISOR_SYSTEM_PROMPT, _advisor_state_snapshot,
# _advisor_build_prompt, _advisor_agent_query, _advisor_parse,
# run_execution_advisor, execute_with_advisor) moved to src/advisor.py
# (Phase 6); re-exported via the import shim at the top of this module. The
# HERMX_ADVISOR_* config constants stay above (engine-config block) so tests
# keep monkeypatching them on wr.


def _build_close_record(payload: dict, normalized: dict, received_at_override: str | None = None) -> tuple[int, dict]:
    """Intake path for a webhook-driven close (action=close). Runs the SAME
    source / schema / strategy-match / dedupe gates as an open signal, then routes
    through the operator-close executor (close_only=True). A close carries no side,
    so it deliberately bypasses the action-not-buy/sell gate that guards open signals."""
    if normalized.get("source") != "tradingview":
        return 202, {"ok": True, "ignored": True, "reason": "non_tradingview_source", "normalized": normalized}

    received_at = received_at_override or now_iso()

    # Schema enforcement — identical posture to the open path (observe-only default).
    _alert_schema_enforcement_status()
    schema_ok, schema_error = validate_alert_schema(normalized)
    if not schema_ok:
        ALERT_SCHEMA_METRICS["invalid"] += 1
        if bool(STRATEGY_ENGINE.get("enforce_alert_schema", False)):
            ALERT_SCHEMA_METRICS["quarantined"] += 1
            reason = f"alert_schema_invalid:{schema_error}"
            record = {
                "received_at": received_at,
                "mode": "strategy_alert_quarantine",
                "ok": True,
                "quarantined": True,
                "reason": reason,
                "payload": payload,
                "normalized": normalized,
                "strategy_config": None,
            }
            record_pipeline_event("quarantine", normalized.get("signal_id"), record)
            record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "quarantined": True, "reason": reason})
            return 202, record
        logging.warning("alert schema invalid (observe-only, processing anyway): %s", schema_error)

    # A close must resolve to a KNOWN strategy — venue/instrument routing lives in
    # the strategy file. No match → quarantine (never a 400 side_not_allowed).
    strategy_ok, strategy_config, strategy_error = validate_strategy_alert(normalized)
    if not strategy_ok or strategy_config is None:
        reason = strategy_error or "strategy_id_unknown"
        record = {
            "received_at": received_at,
            "mode": "strategy_alert_quarantine",
            "ok": True,
            "quarantined": True,
            "reason": reason,
            "payload": payload,
            "normalized": normalized,
            "strategy_config": strategy_config,
        }
        record_pipeline_event("quarantine", normalized.get("signal_id"), record)
        record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "quarantined": True, "reason": reason})
        return 202, record

    duplicate, dedupe = check_and_mark_signal(normalized, received_at)
    latency = latency_info(normalized.get("tv_time"), received_at)
    if duplicate:
        record = {
            "received_at": received_at,
            "mode": "webhook_close",
            "config_snapshot": {"strategy_engine": STRATEGY_ENGINE},
            "ok": True,
            "duplicate": True,
            "dedupe": dedupe,
            "latency": latency,
            "payload": payload,
            "normalized": normalized,
            "strategy_config": strategy_config,
        }
        record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "duplicate": True})
        record_pipeline_event("dedup_reject", normalized.get("signal_id"), record)
        return 200, record

    # Route through the SAME controlled operator-close executor. close_only=True in
    # its readiness bypasses the kill switch + symbol pause; every other gate runs.
    # When the execution surface is unavailable this returns not_submitted (no order).
    result = execute_operator_close(normalized["symbol"], strategy_config, reason="webhook_close")
    record = {
        "received_at": received_at,
        "mode": "webhook_close",
        "config_snapshot": {"strategy_engine": STRATEGY_ENGINE},
        "ok": True,
        "payload": payload,
        "normalized": normalized,
        "duplicate": False,
        "dedupe": dedupe,
        "latency": latency,
        "strategy_config": strategy_config,
        "strategy_warning": strategy_error,
        "close_only": True,
        "okx_execution": result,
    }
    record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "strategy_id": normalized.get("strategy_id"), "close": True})
    record_pipeline_event("strategy_match", normalized.get("signal_id"), record)
    record_pipeline_event("decision", normalized.get("signal_id"), record)
    _atomic_json_dump(LATEST_FILE, record)
    return 200, record


def build_record(payload: dict, received_at_override: str | None = None) -> tuple[int, dict]:
    normalized = normalize(payload)

    # PR2 close branch: action=close reduces risk, so it reuses the operator-close
    # path (close_only=True → bypasses the kill switch + symbol pause). It carries
    # no side, so it must return BEFORE the action-not-buy/sell gate below.
    if normalized.get("action") == "close":
        return _build_close_record(payload, normalized, received_at_override)

    # A close has already returned above, so `action` here is buy/sell for a valid open;
    # a malformed open alert (invalid side, no valid action) carries an `action` that is
    # neither buy nor sell → 400.
    if normalized.get("action") not in {"buy", "sell"}:
        return 400, {"ok": False, "error": "side_not_allowed", "normalized": normalized}
    if normalized.get("source") != "tradingview":
        return 202, {"ok": True, "ignored": True, "reason": "non_tradingview_source", "normalized": normalized}

    received_at = received_at_override or now_iso()

    # Phase 6 / M2: explicit alert-schema validation at intake. OBSERVE-ONLY by
    # default -- gated behind strategy_engine.enforce_alert_schema (default OFF).
    # When OFF, a schema-invalid alert is logged + counted but processed exactly
    # as before (zero behavior change). When ON, it is routed to the EXISTING
    # strategy-alert quarantine path and never processed.
    # Surface the fail-open-while-armed hole: if enforcement is armed but the validator
    # cannot load, operators are alerted (deduped) instead of silently trusting nothing.
    _alert_schema_enforcement_status()
    schema_ok, schema_error = validate_alert_schema(normalized)
    if not schema_ok:
        ALERT_SCHEMA_METRICS["invalid"] += 1
        if bool(STRATEGY_ENGINE.get("enforce_alert_schema", False)):
            ALERT_SCHEMA_METRICS["quarantined"] += 1
            reason = f"alert_schema_invalid:{schema_error}"
            record = {
                "received_at": received_at,
                "mode": "strategy_alert_quarantine",
                "ok": True,
                "quarantined": True,
                "reason": reason,
                "payload": payload,
                "normalized": normalized,
                "strategy_config": None,
            }
            record_pipeline_event("quarantine", normalized.get("signal_id"), record)
            record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "quarantined": True, "reason": reason})
            return 202, record
        logging.warning("alert schema invalid (observe-only, processing anyway): %s", schema_error)

    strategy_ok, strategy_config, strategy_error = validate_strategy_alert(normalized)
    if not strategy_ok:
        record = {
            "received_at": received_at,
            "mode": "strategy_alert_quarantine",
            "ok": True,
            "quarantined": True,
            "reason": strategy_error,
            "payload": payload,
            "normalized": normalized,
            "strategy_config": strategy_config,
        }
        record_pipeline_event("quarantine", normalized.get("signal_id"), record)
        record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "quarantined": True, "reason": strategy_error})
        return 202, record
    if strategy_config is None and normalized["symbol"] not in ALLOWED_SYMBOLS:
        return 400, {"ok": False, "error": "symbol_not_allowed", "normalized": normalized}

    duplicate, dedupe = check_and_mark_signal(normalized, received_at)
    latency = latency_info(normalized.get("tv_time"), received_at)
    if duplicate:
        record = {
            "received_at": received_at,
            "mode": "strategy_file_trial" if strategy_config else "no_strategy_match",
            "config_snapshot": {"strategy_engine": STRATEGY_ENGINE},
            "ok": True,
            "duplicate": True,
            "dedupe": dedupe,
            "latency": latency,
            "payload": payload,
            "normalized": normalized,
            "strategy_config": strategy_config,
        }
        record_raw_webhook("webhook", {"received_at": received_at, "payload": payload, "normalized": normalized, "duplicate": True})
        record_pipeline_event("dedup_reject", normalized.get("signal_id"), record)
        if strategy_config:
            record_pipeline_event("strategy_match", normalized.get("signal_id"), record)
        return 200, record

    if strategy_config is not None:
        direction = "long" if normalized.get("action") == "buy" else "short"
        decision = {
            "policy": f"strategy_file:{normalized.get('strategy_id')}",
            "decision": "TRADE",
            "action": "TRADE",
            "risk_weight": 1.0,
            "target_direction": direction,
            "score": None,
            "reasons": [
                "strategy_id matched strategy file",
                "Duo Base Dev alert is in trial mode",
                "OKX order submission is disabled until explicit promotion",
            ],
        }
        record = {
            "received_at": received_at,
            "mode": "strategy_file_trial",
            "config_snapshot": {
                "strategy_engine": STRATEGY_ENGINE,
                "strategy": {
                    "strategy_id": strategy_config.get("strategy_id"),
                    "timeframe": strategy_config.get("timeframe"),
                    "budget_usd": strategy_budget_usd(strategy_config),
                    "leverage": strategy_config.get("leverage"),
                    "margin_mode": strategy_config.get("margin_mode"),
                    "execution_mode": strategy_config.get("execution_mode"),
                },
            },
            "ok": True,
            "payload": payload,
            "normalized": normalized,
            "duplicate": False,
            "dedupe": dedupe,
            "latency": latency,
            "market_context": {
                "chart_type": strategy_config.get("chart_type") or normalized.get("chart_type"),
            },
            "strategy_config": strategy_config,
            "strategy_warning": strategy_error,
            "strategy_decision": decision,
            "decision": decision,
            "policies": {},
            "paper_events": [],
        }
        record["execution_readiness"] = build_strategy_execution_readiness(record)
        record["okx_execution"] = execute_with_advisor(record)
        record_raw_webhook("webhook", {"received_at": record["received_at"], "payload": payload, "normalized": normalized, "strategy_id": normalized.get("strategy_id")})
        record_pipeline_event("strategy_match", normalized.get("signal_id"), record)
        record_pipeline_event("decision", normalized.get("signal_id"), record)
        _atomic_json_dump(LATEST_FILE, record)
        return 200, record

    # No strategy file matched. The legacy shadow/policy engine (mxc reads, policy
    # decisions, paper trading) that used to process these generic alerts has been
    # removed, so a non-strategy alert is now an observe-only no-op: it is recorded
    # but never decisioned or executed.
    record = {
        "received_at": received_at,
        "mode": "no_strategy_match",
        "config_snapshot": {"strategy_engine": STRATEGY_ENGINE},
        "ok": True,
        "payload": payload,
        "normalized": normalized,
        "duplicate": False,
        "dedupe": dedupe,
        "latency": latency,
        "market_context": {"chart_type": normalized.get("chart_type"), "okx_mark_price": normalized.get("okx_mark_price"), "okx_last_price": normalized.get("okx_last_price")},
        "strategy_config": None,
        "decision": {},
        "policies": {},
        "paper_events": [],
    }
    record_raw_webhook("webhook", {"received_at": record["received_at"], "payload": payload, "normalized": normalized})
    record_pipeline_event("decision", normalized.get("signal_id"), record)
    _atomic_json_dump(LATEST_FILE, record)
    return 200, record


def process_payload_async(payload: dict, intake_received_at: str) -> None:
    try:
        status, record = build_record(payload, intake_received_at)
        if status >= 400:
            record_pipeline_event("error", _signal_id_of(record), {"received_at": intake_received_at, "status": status, "record": record})
            logging.warning("Shadow async processing rejected status=%s error=%s", status, record.get("error"))
        else:
            normalized = record.get("normalized") or {}
            logging.info(
                "Shadow async processed symbol=%s action=%s tv_time=%s status=%s",
                normalized.get("symbol"),
                normalized.get("action"),
                normalized.get("tv_time"),
                status,
            )
    except Exception as exc:
        record_pipeline_event("error", None, {"received_at": intake_received_at, "error": str(exc), "payload": payload})
        logging.exception("Shadow async processing failed")


def worker_loop(worker_name: str = "shadow-policy-worker-1") -> None:
    while True:
        _set_worker_heartbeat(worker_name)
        try:
            item = PROCESS_QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue
        if not isinstance(item, tuple) or len(item) < 2:
            PROCESS_QUEUE.task_done()
            continue
        payload = item[0]
        intake_received_at = item[1]
        symbol = _payload_symbol(payload)
        ticket: int | None = None
        if len(item) >= 4:
            item_symbol = str(item[2] or "").strip().upper()
            if item_symbol:
                symbol = item_symbol
            try:
                ticket = int(item[3])
            except (TypeError, ValueError):
                ticket = None
        if ticket is not None and not _symbol_ticket_is_turn(symbol, ticket):
            PROCESS_QUEUE.put(item)
            PROCESS_QUEUE.task_done()
            time.sleep(0.001)
            continue
        try:
            with _symbol_lock(symbol):
                _set_worker_heartbeat(worker_name)
                process_payload_async(payload, intake_received_at)
                _set_worker_heartbeat(worker_name)
        finally:
            if ticket is not None:
                _advance_symbol_ticket_turn(symbol, ticket)
            PROCESS_QUEUE.task_done()


def replay_intake_webhooks(now_seconds=None) -> "tuple[int, int, int]":
    """Re-queue intake rows that were accepted (HTTP 200) but never dequeued
    before a restart. Returns (replayed, skipped, dropped). Best-effort:
    never raises.

    Option A: drops any intake row whose payload lacks a time field, because
    normalize() would use now_iso() for tv_time, producing a different
    signal_id on replay and bypassing the dedupe ledger.
    """
    if not HERMX_REPLAY_ENABLED or REPLAY_LOOKBACK_SECONDS <= 0:
        return (0, 0, 0)

    now = time.time() if now_seconds is None else float(now_seconds)
    try:
        rows = read_jsonl_tolerant(RAW_WEBHOOK_LEDGER)
    except Exception as exc:
        logging.error("replay: failed reading raw-webhooks.jsonl: %s", exc)
        return (0, 0, 0)

    processed = {
        r.get("received_at")
        for r in rows
        if isinstance(r, dict) and r.get("phase") in ("webhook", "dropped") and r.get("received_at")
    }
    intake_rows = [
        r for r in rows
        if isinstance(r, dict) and r.get("phase") == "intake"
    ]

    replayed = skipped = dropped = 0
    seen_received_at: "set[str]" = set()

    # Load dedupe index once (covers the 24h window).
    with _SIGNAL_DEDUPE_LOCK:
        _load_signal_dedupe_index(now_seconds=now)
        sig_idx = _SIGNAL_DEDUPE_INDEX.get("signals", {})
        key_idx = _SIGNAL_DEDUPE_INDEX.get("keys", {})

    for r in intake_rows:
        rcv = r.get("received_at")
        if not rcv or rcv in seen_received_at:
            skipped += 1
            continue
        seen_received_at.add(rcv)

        if rcv in processed:
            skipped += 1
            continue

        rcv_epoch = _epoch_from_iso(rcv)
        if rcv_epoch is None or rcv_epoch < now - REPLAY_LOOKBACK_SECONDS:
            skipped += 1
            continue

        payload = r.get("payload")
        if not isinstance(payload, dict):
            skipped += 1
            continue

        # Option A: require a deterministic time field.
        if not _has_time_field(payload):
            logging.warning("replay: dropping intake row at %s -- no time field in payload", rcv)
            dropped += 1
            continue

        norm = normalize(payload)
        sid = str(norm.get("signal_id") or "")
        dedup_key = dedupe_key(norm)
        if sid in sig_idx or dedup_key in key_idx:
            skipped += 1
            continue

        tv_time_str = str(norm.get("tv_time") or "")
        tv_epoch = _epoch_from_iso(tv_time_str)
        if tv_epoch is None:
            logging.warning("replay: dropping intake row at %s -- unparseable tv_time %r", rcv, tv_time_str)
            dropped += 1
            continue
        if tv_epoch < now - REPLAY_MAX_TV_AGE_SECONDS:
            logging.info(
                "replay: dropping stale signal tv_time=%s (epoch=%s, now=%s, delta=%s)",
                tv_time_str, tv_epoch, now, now - tv_epoch,
            )
            dropped += 1
            continue

        try:
            work_item = _queue_work_item(payload, rcv)
            PROCESS_QUEUE.put_nowait(work_item)
            replayed += 1
            logging.info("replay: requeued signal %s (received_at=%s)", sid, rcv)
        except queue.Full:
            logging.warning("replay: queue full, dropping signal %s", sid)
            _burn_symbol_ticket(work_item[2], work_item[3])
            dropped += 1
            break  # further puts will also fail

    return (replayed, skipped, dropped)


class Handler(BaseHTTPRequestHandler):
    timeout = max(1.0, HERMX_REQUEST_TIMEOUT_SECONDS)  # kills stalled body reads

    def _send(self, status: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/health", "/shadow/health"}:
            self._send(200, {"ok": True, "service": "mxc-vps-shadow-receiver", "port": PORT, "mode": "shadow_only", "latest": str(LATEST_FILE)})
        elif path in {"/latest", "/shadow/latest"}:
            if not LATEST_FILE.exists():
                self._send(404, {"ok": False, "error": "no_latest_yet"})
            else:
                try:
                    self._send(200, json.loads(LATEST_FILE.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    self._send(503, {"ok": False, "error": "latest_unreadable"})
        elif path in {"/api/admin/order-troubleshoot", "/shadow/api/admin/order-troubleshoot"}:
            self._handle_order_troubleshoot_report()
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def _handle_order_troubleshoot_report(self) -> None:
        """GET /api/admin/order-troubleshoot -- read-only. Authenticated identically to
        /api/close (X-Dashboard-Token == HERMX_SECRET, constant-time, fail closed if the
        secret is blank). Never issues a live venue call -- see orders/troubleshoot.py."""
        provided = (self.headers.get("X-Dashboard-Token") or "").strip()
        if not SECRET or not hmac.compare_digest(provided, SECRET):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        results = troubleshoot_all_open_orders()
        self._send(200, {
            "ok": True,
            "results": [
                {
                    "cl_ord_id": r.cl_ord_id,
                    "issue_type": r.issue_type,
                    "evidence": r.evidence,
                    "actions": [{"id": a.id, "label": a.label} for a in r.actions],
                }
                for r in results
            ],
        })

    def _handle_order_journal_apply_action(self) -> None:
        """POST /api/admin/order-journal/apply-action -- the server re-derives eligibility
        itself from the journal's OWN history (run_classifiers) and NEVER accepts a
        caller-supplied target state; action_id only selects among what is currently,
        server-computed eligible for this cl_ord_id. See orders/troubleshoot.py."""
        provided = (self.headers.get("X-Dashboard-Token") or "").strip()
        if not SECRET or not hmac.compare_digest(provided, SECRET):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0:
                raise ValueError("negative content length")
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length > max(1, HERMX_MAX_BODY_BYTES):
            self._send(413, {"ok": False, "error": "payload_too_large", "max_body_bytes": max(1, HERMX_MAX_BODY_BYTES)})
            return
        try:
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:
            self._send(400, {"ok": False, "error": "invalid_json", "detail": str(exc)})
            return
        cl_ord_id = str(body.get("cl_ord_id") or "").strip()
        action_id = str(body.get("action_id") or "").strip()
        operator = body.get("operator")
        reason = body.get("reason")
        if not cl_ord_id:
            self._send(400, {"ok": False, "error": "missing_cl_ord_id"})
            return
        if not action_id:
            self._send(400, {"ok": False, "error": "missing_action_id", "cl_ord_id": cl_ord_id})
            return
        result = run_classifiers(cl_ord_id)
        eligible_ids = {a.id for a in (result.actions if result else [])}
        if result is None or action_id not in eligible_ids:
            self._send(200, {"ok": True, "outcome": "refused", "reason": "action_not_currently_eligible", "cl_ord_id": cl_ord_id})
            return
        if action_id != "restore_terminal":
            # Unreachable given the eligible_ids check above (only restore_terminal is
            # ever offered today) -- fail closed defensively if that ever changes.
            self._send(200, {"ok": True, "outcome": "refused", "reason": "unknown_action_id", "cl_ord_id": cl_ord_id})
            return
        latest = latest_order_record(cl_ord_id)
        from_state = (latest or {}).get("state")
        to_state = result.evidence.get("terminal_state")
        intent = (latest or {}).get("intent") or {}
        try:
            record_order_state(
                cl_ord_id,
                to_state,
                intent=intent,
                detail={"troubleshoot_heal": True, "operator": operator, "reason": reason, "evidence": result.evidence},
                prev_state=from_state,
            )
        except ValueError as exc:
            self._send(200, {"ok": True, "outcome": "refused", "reason": "race_lost", "detail": str(exc), "cl_ord_id": cl_ord_id})
            return
        except OSError as exc:
            _fail_closed_state_write("order-journal-troubleshoot-heal", exc, context={"cl_ord_id": cl_ord_id})
            self._send(500, {"ok": False, "error": "state_write_failed", "cl_ord_id": cl_ord_id})
            return
        self._send(200, {"ok": True, "outcome": "healed", "cl_ord_id": cl_ord_id, "from_state": from_state, "to_state": to_state})

    def _handle_operator_close(self) -> None:
        """POST /api/close -- operator-instructed flatten. Authenticated by the
        dashboard token (X-Dashboard-Token == HERMX_SECRET, constant-time, fail closed
        if the secret is blank). Routes through the controlled close path, which
        bypasses ONLY the kill switch + symbol pause (a close reduces risk)."""
        provided = (self.headers.get("X-Dashboard-Token") or "").strip()
        # Fail closed: a blank server secret can never authenticate a close.
        if not SECRET or not hmac.compare_digest(provided, SECRET):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0:
                raise ValueError("negative content length")
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length > max(1, HERMX_MAX_BODY_BYTES):
            self._send(413, {"ok": False, "error": "payload_too_large", "max_body_bytes": max(1, HERMX_MAX_BODY_BYTES)})
            return
        try:
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:
            self._send(400, {"ok": False, "error": "invalid_json", "detail": str(exc)})
            return
        symbol = str(body.get("symbol") or "").strip()
        strategy_id = str(body.get("strategy_id") or "").strip()
        operator = body.get("operator")
        reason = body.get("reason")
        if not symbol:
            self._send(400, {"ok": False, "error": "missing_symbol"})
            return
        if not strategy_id:
            self._send(400, {"ok": False, "error": "missing_strategy_id", "symbol": symbol})
            return
        strategy = STRATEGIES.get(strategy_id)
        if not strategy:
            self._send(404, {"ok": False, "error": "unknown_strategy_id", "symbol": symbol, "strategy_id": strategy_id})
            return
        try:
            result = execute_operator_close(symbol, strategy, operator=operator, reason=reason)
        except Exception as exc:  # unexpected server-side failure only
            logging.exception("operator close failed")
            self._send(500, {"ok": False, "error": redact_secrets(str(exc)), "symbol": symbol})
            return
        mode = result.get("mode")
        if mode == "not_submitted":
            # An expected control outcome (blocked gate / idempotent duplicate), not an error.
            self._send(200, {"ok": False, "mode": "not_submitted", "reason": result.get("reason"), "symbol": symbol})
        elif result.get("ok"):
            self._send(200, {
                "ok": True,
                "mode": "submitted",
                "symbol": symbol,
                "cl_ord_id": result.get("_cl_ord_id"),
                "submitted_at": result.get("_submitted_at"),
            })
        else:
            # Adapter reported a non-ok outcome (e.g. submit_exception/rejected): an
            # expected, journaled execution result -- surface it without a 500.
            self._send(200, {"ok": False, "mode": mode, "reason": result.get("reason") or result.get("error"), "symbol": symbol})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/api/close", "/shadow/api/close"}:
            self._handle_operator_close()
            return
        if parsed.path in {"/api/admin/order-journal/apply-action", "/shadow/api/admin/order-journal/apply-action"}:
            self._handle_order_journal_apply_action()
            return
        if parsed.path not in {"/webhook", "/shadow/webhook"}:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative content length")
        except Exception:
            self._send(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length > max(1, HERMX_MAX_BODY_BYTES):
            self._send(413, {"ok": False, "error": "payload_too_large", "max_body_bytes": max(1, HERMX_MAX_BODY_BYTES)})
            return
        source_key = _rate_limit_key(self)
        allowed, rate_meta = rate_limit_allow(source_key)
        if not allowed:
            self._send(429, {"ok": False, "error": "rate_limited", **rate_meta})
            return
        try:
            raw_body = self.rfile.read(length) if length else b""
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            self._send(400, {"ok": False, "error": "invalid_json", "detail": str(exc)})
            return
        # A syntactically valid but non-object JSON body (list / string / number / bool)
        # parses fine yet has no ``.get()``/``.pop()`` -- reject it here BEFORE auth or any
        # dict access so a `[1,2,3]` body is a clean 400, not a downstream AttributeError/500.
        if not isinstance(payload, dict):
            self._send(400, {"ok": False, "error": "invalid_payload"})
            return
        auth_ok, auth_status, auth_error = authenticate_webhook_request(self, raw_body, payload)
        if not auth_ok:
            self._send(auth_status, {"ok": False, "error": auth_error})
            return
        # LINCHPIN: strip the body ``secret_key`` from the live payload dict the instant
        # auth succeeds and BEFORE the first persist. This same dict object is reused all
        # the way through _queue_work_item and every downstream worker-phase persist call,
        # so this single pop keeps the shared secret out of raw-webhooks.jsonl and
        # pipeline.jsonl everywhere. Defensively strip a nested extras.secret_key too, in
        # case an operator nests it. (record_* writers also scrub as belt-and-suspenders.)
        if isinstance(payload, dict):
            payload.pop("secret_key", None)
            _extras = payload.get("extras")
            if isinstance(_extras, dict):
                _extras.pop("secret_key", None)
        intake_received_at = now_iso()
        record_raw_webhook("intake", {"received_at": intake_received_at, "payload": payload, "path": parsed.path})
        work_item = _queue_work_item(payload, intake_received_at)
        try:
            PROCESS_QUEUE.put_nowait(work_item)
        except queue.Full:
            _burn_symbol_ticket(work_item[2], work_item[3])
            # Terminal marker: the intake row was written but the signal never made it
            # onto the queue (503 to the client). Without this, replay's processed set
            # (built from "webhook" outcomes) would resurrect the dropped signal on the
            # next restart.
            record_raw_webhook("dropped", {"received_at": intake_received_at, "reason": "queue_full"})
            emit_operator_alert(
                ALERT_QUEUE_SATURATION,
                {
                    "queue_depth": PROCESS_QUEUE.qsize(),
                    "queue_maxsize": PROCESS_QUEUE.maxsize,
                    "dropped": True,
                    "path": parsed.path,
                    "source_key": source_key,
                },
                severity="error",
            )
            self._send(503, {"ok": False, "error": "queue_full", "queue_depth": PROCESS_QUEUE.qsize(), "queue_maxsize": PROCESS_QUEUE.maxsize})
            return
        queue_depth = PROCESS_QUEUE.qsize()
        maybe_emit_queue_saturation_alert(queue_depth)
        self._send(200, {"ok": True, "status": "queued", "received_at": intake_received_at, "queue_depth": queue_depth})

    def log_message(self, _fmt, *_args):
        return


def log_execution_arm_state() -> None:
    """Startup self-check: print the effective order-submission posture.

    Phase A surfaces the two operative controls: per-strategy ``execution_mode``
    (demo vs live counts across the loaded strategy files) and the single global
    ``HERMX_LIVE_TRADING`` kill switch. Every Phase-A submission routes to the demo
    sandbox, so the live switch is informational until Phase B wires the live gate.
    """
    modes = [str((s or {}).get("execution_mode") or "demo").lower() for s in STRATEGIES.values()]
    demo_count = sum(1 for m in modes if m != "live")
    live_count = sum(1 for m in modes if m == "live")
    live_enabled, live_raw = live_trading_enabled()
    logging.info(
        "EXECUTION ARM STATE: execution_mode demo=%s live=%s (total_strategies=%s) "
        "HERMX_LIVE_TRADING=%s (live_trading_enabled=%s) "
        "reconcile_post_submit=%s unknown_resolver_enabled=%s startup_reconcile_complete=%s "
        "auth_config_healthy=%s require_hmac=%s queue_maxsize=%s "
        "worker_pool_size=%s submit_timeout_s=%s watchdog_enabled=%s",
        demo_count,
        live_count,
        len(modes),
        live_raw,
        live_enabled,
        reconcile_post_submit_enabled(),
        unknown_resolver_enabled(),
        RECONCILE_STARTUP_COMPLETE,
        webhook_auth_config_healthy(),
        HERMX_REQUIRE_HMAC,
        PROCESS_QUEUE.maxsize,
        max(1, HERMX_WORKER_POOL_SIZE),
        max(1.0, HERMX_SUBMIT_TIMEOUT_SECONDS),
        HERMX_WATCHDOG_STALE_SECONDS > 0,
    )


_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


def bind_security_warnings(bind_host: "str | None" = None, require_hmac: "bool | None" = None) -> list:
    """PURE: startup security warnings for the receiver's network exposure.

    Binding a non-loopback interface (e.g. 0.0.0.0 or a LAN IP) makes the webhook
    reachable OFF-HOST. With HERMX_REQUIRE_HMAC=false the only protection is the shared
    secret -- no per-request HMAC + replay-freshness check -- so a leaked/guessed secret
    is fully replayable. Surface this loudly at boot so it is a deliberate choice."""
    host = HERMX_BIND_HOST if bind_host is None else bind_host
    rh = HERMX_REQUIRE_HMAC if require_hmac is None else require_hmac
    out: list = []
    if str(host or "").strip().lower() not in _LOOPBACK_BIND_HOSTS and not rh:
        out.append(
            f"SECURITY: receiver bound to non-loopback {host} with HERMX_REQUIRE_HMAC=false -- "
            "the webhook is reachable off-host protected ONLY by the shared secret (no HMAC / "
            "replay-freshness). Set HERMX_REQUIRE_HMAC=true (+ HERMX_WEBHOOK_HMAC_KEY) or bind 127.0.0.1."
        )
    return out


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    if not SECRET:
        logging.error("Webhook auth misconfigured: HERMX_SECRET is missing/blank. Receiver FAILS CLOSED with 401 for all webhook requests.")
    if HERMX_REQUIRE_HMAC and not HERMX_WEBHOOK_HMAC_KEY:
        logging.error("HMAC is required but HERMX_WEBHOOK_HMAC_KEY is missing/blank. Receiver FAILS CLOSED with 401 for all webhook requests.")
    for warning in bind_security_warnings():
        logging.warning(warning)
    if not env_file_permissions_healthy():
        logging.error(".env permissions are too broad; expected 600-style owner-only access.")
    quarantine_summary = startup_quarantine_partial_ledgers()
    if quarantine_summary["quarantined"]:
        logging.warning("Startup quarantined truncated ledger tails: %s", quarantine_summary["quarantined"])
    if quarantine_summary["errors"]:
        logging.error("Startup ledger quarantine errors: %s", quarantine_summary["errors"])
    log_execution_arm_state()
    # One-time startup reconcile (REFACTOR_PLAN.md:215, acceptance :236). OBSERVE-ONLY:
    # reconciles open orders + compares OKX positions vs local, emitting
    # RECONCILE_MISMATCH on divergence. Read-only and best-effort -- a failure must
    # never prevent the receiver from coming up.
    try:
        reconcile_startup()
    except Exception as exc:  # pragma: no cover - never block boot on observe-only reconcile
        logging.error("startup reconcile failed (observe-only, continuing): %s", exc)
    # Replay any intake webhooks accepted before a restart but never dequeued.
    # TradingView got HTTP 200 and will not retry -- we must recover them. This
    # runs BEFORE worker threads start, so replayed items are already queued when
    # workers begin dequeuing.
    if HERMX_REPLAY_ENABLED and REPLAY_LOOKBACK_SECONDS > 0:
        try:
            replayed, skipped, dropped = replay_intake_webhooks()
        except Exception as exc:  # pragma: no cover - never block boot on best-effort replay
            logging.error("startup replay failed (continuing): %s", exc)
        else:
            if replayed or dropped:
                logging.info(
                    "Startup replay: requeued=%d skipped=%d dropped=%d",
                    replayed, skipped, dropped,
                )
                record_pipeline_event(
                    "startup_replay", None,
                    {"replayed": replayed, "skipped": skipped, "dropped": dropped, "at": now_iso()},
                )
    if unknown_resolver_enabled():
        threading.Thread(target=unknown_resolver_loop, daemon=True, name="unknown-resolver").start()
    pool_size = max(1, HERMX_WORKER_POOL_SIZE)
    _WORKER_NAMES.clear()
    for i in range(pool_size):
        worker_name = f"shadow-policy-worker-{i + 1}"
        _WORKER_NAMES.append(worker_name)
        _set_worker_heartbeat(worker_name)
        threading.Thread(target=worker_loop, args=(worker_name,), daemon=True, name=worker_name).start()
    if HERMX_WATCHDOG_STALE_SECONDS > 0:
        threading.Thread(target=liveness_watchdog_loop, daemon=True, name="watchdog").start()
    server = ThreadingHTTPServer((HERMX_BIND_HOST, PORT), Handler)
    logging.info("MXC VPS shadow receiver listening on %s:%s", HERMX_BIND_HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
