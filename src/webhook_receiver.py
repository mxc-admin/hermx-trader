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
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib import request as urllib_request
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
ORDER_JOURNAL_LEDGER = LOG_DIR / "order-journal.jsonl"
ORDER_JOURNAL_SCHEMA_VERSION = 1
# Order-journal lifecycle (verified checkpoint + segment rotation, Phase 1 task 7).
# Without it _order_journal_next_seq() and latest_order_record() re-read the WHOLE
# append-only journal on every submit -- O(n) per order, unbounded growth. The
# checkpoint folds the journal into the bounded "latest record per cl_ord_id" index
# (the dedupe/idempotency authority) plus each order's origin ts, so a load rebuilds
# from (checkpoint + live-segment tail) instead of the full history, and rotation seals
# the live segment so disk does not grow without limit. The order journal is the
# submission state machine and is always active.
ORDER_JOURNAL_CHECKPOINT_FILE = LOG_DIR / "order-journal.checkpoint.json"
ORDER_JOURNAL_CHECKPOINT_VERSION = 1
ORDER_STATE_PLANNED = "PLANNED"
ORDER_STATE_SUBMITTED = "SUBMITTED"
ORDER_STATE_FILLED = "FILLED"
ORDER_STATE_REJECTED = "REJECTED"
ORDER_STATE_UNKNOWN = "UNKNOWN"
# Terminal states accept no further transitions; the rest are "open" and are what
# load_open_orders() surfaces to startup reconciliation.
ORDER_TERMINAL_STATES = frozenset({ORDER_STATE_FILLED, ORDER_STATE_REJECTED})
ORDER_NON_TERMINAL_STATES = frozenset({ORDER_STATE_PLANNED, ORDER_STATE_SUBMITTED, ORDER_STATE_UNKNOWN})
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
RECONCILE_ALERT_MISMATCH = "RECONCILE_MISMATCH"
RECONCILE_ALERT_RESOLVER_TIMEOUT = "UNKNOWN_RESOLVER_TIMEOUT"
# A PLANNED order that crashed before submission (never advanced to SUBMITTED) was, by
# write-ahead ordering, NEVER sent to the venue -- the resolver rejects it never_submitted.
RECONCILE_ALERT_PLANNED_ABANDONED = "PLANNED_ORDER_ABANDONED"
# Anomaly: a PLANNED orphan that the venue unexpectedly DOES know about (should not
# happen given write-ahead) -- promoted to SUBMITTED for normal reconciliation, alerted.
RECONCILE_ALERT_PLANNED_ON_VENUE = "PLANNED_ORDER_ON_VENUE"
# Concrete operator transport (Task 6): alerts are mirrored to the unified
# ALERTS_LEDGER (kind="operator") and optionally POSTed to an external webhook
# (HERMX_ALERT_WEBHOOK_URL).
ALERT_AUTH_FAILURE = "AUTH_FAILURE"
ALERT_QUEUE_SATURATION = "QUEUE_SATURATION"
# Bounded exponential backoff (:213): max 5 attempts, 500ms base, ~8s cap, <=~20s wall.
RECONCILE_MAX_ATTEMPTS = 5
RECONCILE_BASE_DELAY_SECONDS = 0.5
RECONCILE_CAP_DELAY_SECONDS = 8.0
RECONCILE_WALL_CLOCK_BUDGET_SECONDS = 20.0
RECONCILE_HISTORY_LIMIT = 100
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
# Raw OKX order states that mean "the order genuinely exists on the venue". Anything
# else returned by the query layer (not_found / error / not_implemented / unknown /
# empty) is treated as "not present here" so the fallback chain keeps searching.
_PRESENT_ORDER_STATES = frozenset({"live", "partially_filled", "filled", "canceled"})
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


def strategy_instrument(row: dict) -> dict:
    """PURE: canonical instrument block for a strategy.

    A v2 strategy carries a generic ``instrument`` block ({exchange, inst_id,
    type}); this resolver reads it directly and never touches the legacy
    ``okx_inst_id`` key (Layer C removed that runtime bridge). Every strategy on
    disk is v2, so a record WITHOUT an instrument block resolves to {} -- the
    venue-less top-level ``inst_id`` -> okx fallback is gone (it silently assumed a
    venue, which is a money-safety hazard once non-okx venues exist). Callers fail
    closed on an empty result. The strategy NEVER carries credentials
    (REFACTOR_PLAN.md §0.4) -- this only maps the public venue/instrument selection.
    """
    inst = (row or {}).get("instrument")
    if isinstance(inst, dict) and inst.get("inst_id"):
        return {
            "exchange": str(inst.get("exchange") or "okx").lower(),
            "inst_id": str(inst.get("inst_id")),
            "type": str(inst.get("type") or "swap"),
        }
    return {}


# Instrument-type suffixes that are NOT part of the BASE+QUOTE asset symbol.
_INSTRUMENT_TYPE_SUFFIXES = {"SWAP", "FUTURES", "FUTURE", "PERP", "SPOT", "MARGIN", "OPTION"}


def strategy_asset(strategy: dict) -> str:
    """PURE: the BASE+QUOTE asset symbol for a strategy (e.g. ``BTCUSDT``).

    The v3 strategy shape dropped the explicit ``asset`` field; the symbol is now
    derived from the canonical ``instrument.inst_id``. An OKX-native id
    (``BTC-USDT-SWAP``) or a CCXT-unified id (``BTC/USDT:USDT``) both resolve to
    ``BTCUSDT`` so the alert-symbol match (uppercased, separators stripped) keeps
    working. A still-present top-level ``asset`` is honored as an override.
    """
    explicit = str((strategy or {}).get("asset") or "").strip().upper()
    if explicit:
        return explicit
    inst_id = str((strategy_instrument(strategy) or {}).get("inst_id") or "")
    if not inst_id:
        return ""
    core = inst_id.split(":", 1)[0].replace("/", "-")  # drop settle ccy, unify sep
    parts = [p for p in core.split("-") if p]
    if len(parts) >= 3 and parts[-1].upper() in _INSTRUMENT_TYPE_SUFFIXES:
        parts = parts[:-1]
    return "".join(parts).upper()


def strategy_budget_usd(strategy: dict) -> float:
    """Read budget from capital.budget_usd (v2 nested) with flat fallback."""
    cap = strategy.get("capital")
    if isinstance(cap, dict):
        v = cap.get("budget_usd")
        if v is not None:
            return float(v)
    v = strategy.get("budget_usd")
    return float(v) if v is not None else 0.0


def normalize_strategy_record(row: dict) -> dict:
    """v2 loader shim (REFACTOR_PLAN.md Phase 6 / Layer C).

    A schema_version 2 strategy selects its exchange via the generic
    ``instrument`` block and uses ``submit_orders``. This canonicalizes the
    instrument-first shape in place:

      * v2 records (carry ``instrument``): normalize exchange/type defaults so
        downstream resolvers see a complete block.

    Layer C removes the legacy ``okx_inst_id`` -> ``instrument`` runtime bridge:
    strategy files are now canonical v2 on disk, so no v1 synthesis happens here.
    The ``okx_submit_orders`` bridge is deliberately left untouched (out of scope
    for this slice) so the execution-readiness / submit path keeps byte-identical
    behavior.
    """
    inst = row.get("instrument")
    if isinstance(inst, dict) and inst.get("inst_id"):
        inst["exchange"] = str(inst.get("exchange") or "okx").lower()
        inst["type"] = str(inst.get("type") or "swap")
        if "okx_submit_orders" not in row:
            row["okx_submit_orders"] = bool(row.get("submit_orders", False))
    return row


def load_strategy_files() -> dict:
    strategies = {}
    if not STRATEGIES_DIR.exists():
        return strategies
    for path in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            sid = str(row.get("strategy_id") or "").strip()
            if not sid:
                continue
            row = normalize_strategy_record(row)
            row["_path"] = str(path)
            row["timeframe"] = canonical_timeframe(row.get("timeframe"))
            # v3 dropped the explicit asset field; derive the BASE+QUOTE symbol from
            # the canonical instrument so alert-symbol matching keeps working.
            row["asset"] = strategy_asset(row)
            strategies[sid] = row
        except Exception as exc:
            logging.warning("Failed to load strategy file %s: %s", path, exc)
    return strategies


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
ALLOWED_SIDES = {"buy", "sell"}


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


def authenticate_webhook_request(handler: BaseHTTPRequestHandler, body: bytes) -> tuple[bool, int, str]:
    return _authenticate_webhook_request_impl(
        handler,
        body,
        SECRET,
        _client_ip,
        lambda headers, request_body: verify_webhook_hmac(headers, request_body),
        emit_auth_failure_alert,
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
    (append_jsonl_durable if durable else append_jsonl)(PIPELINE_LEDGER, record)
    _rotate_ledger_if_large(PIPELINE_LEDGER)


def record_raw_webhook(phase: str, payload: dict) -> None:
    """Append one inbound-webhook row to the unified raw-webhooks ledger, tagged with a
    ``phase`` field ("intake" = raw HTTP receipt; "webhook" = post-normalization)."""
    if phase not in RAW_WEBHOOK_PHASES:  # pragma: no cover - guard against typos
        logging.warning("record_raw_webhook: unknown phase %r", phase)
    record = {"phase": phase}
    record.update(payload or {})
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


def default_control_state() -> dict:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "mode": "shadow_only",
        "live_trading": "paused",
        "manual_pause": False,
        "pause_reason": "",
        "symbol_pauses": {},
        "strategy_overrides": {},
        # Phase 3 accounting windows: {strategy_id: {accounting_start_at: ms, set_at}}.
        # Locks P&L before the timestamp without deleting ledger history.
        "accounting_windows": {},
        # Phase A (A2) global trading_state: "active" (normal) | "reducing" (risk-off,
        # close-only). MUST live in the default dict or the load_control_state merge
        # ({k in default}) silently drops it -- the same class of bug that once dropped
        # accounting_windows. A simple string, so the merge preserves it with no
        # special re-attach (unlike the dict-valued keys below).
        "trading_state": "active",
        "notes": "Shadow control file. Dashboard/Hermes may read this. Live execution remains disabled here.",
    }


def save_control_state(state: dict) -> None:
    with _STATE_WRITE_LOCK:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{CONTROL_STATE_FILE.name}.", suffix=".tmp", dir=str(CONTROL_STATE_FILE.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(CONTROL_STATE_FILE)
            _fsync_dir(CONTROL_STATE_FILE.parent)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def load_control_state() -> dict:
    if not CONTROL_STATE_FILE.exists():
        state = default_control_state()
        save_control_state(state)
        return state
    try:
        state = json.loads(CONTROL_STATE_FILE.read_text(encoding="utf-8"))
        default = default_control_state()
        merged = {k: v for k, v in (default | state).items() if k in default}
        merged["symbol_pauses"] = state.get("symbol_pauses") if isinstance(state.get("symbol_pauses"), dict) else {}
        merged["strategy_overrides"] = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
        # Phase 3 accounting windows. Preserved explicitly because the ``if k in
        # default`` merge above would otherwise drop it (same reason symbol_pauses/
        # strategy_overrides are re-attached from the raw state, not the merge).
        merged["accounting_windows"] = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
        # Backward compat: remap legacy override mode labels. "shadow" was the old
        # pause concept (validate+ledger, no orders) -> "pause"; "paper" was the
        # sandbox-submit concept -> "demo"; a stored "pause" stays "pause". Only the
        # display label is rewritten; execution_mode/submit_orders are untouched.
        _legacy = {"shadow": "pause", "paper": "demo", "pause": "pause"}
        for _ov in merged["strategy_overrides"].values():
            if isinstance(_ov, dict) and _ov.get("mode") in _legacy:
                _ov["mode"] = _legacy[_ov["mode"]]
        return merged
    except Exception:
        return default_control_state()


def symbol_pause_info(symbol: "str | None", state: "dict | None" = None) -> "dict | None":
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    cur_state = state if state is not None else load_control_state()
    pauses = cur_state.get("symbol_pauses") if isinstance(cur_state.get("symbol_pauses"), dict) else {}
    pause = pauses.get(sym)
    if isinstance(pause, dict) and pause.get("paused"):
        return pause
    return None


def pause_symbol(symbol: "str | None", reason: str) -> bool:
    """Persist a per-symbol pause artifact (Task 6 operator control)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    state = load_control_state()
    pauses = state.get("symbol_pauses") if isinstance(state.get("symbol_pauses"), dict) else {}
    current = pauses.get(sym) if isinstance(pauses.get(sym), dict) else {}
    next_reason = str(reason or "")[:500]
    if current.get("paused") and current.get("reason") == next_reason:
        return False
    pauses[sym] = {
        "paused": True,
        "paused_at": now_iso(),
        "reason": next_reason,
    }
    state["symbol_pauses"] = pauses
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


_VALID_STRATEGY_MODES = frozenset({"pause", "demo", "live"})
# Legacy UI-mode label remap: old control-state.json may carry shadow/paper.
# "shadow" was the old pause concept (validate+ledger, no orders) -> "pause";
# "paper" was the sandbox-submit concept -> "demo".
_LEGACY_STRATEGY_MODE_ALIASES = {"shadow": "pause", "paper": "demo"}
# Per-mode flag mapping. ``submit_orders`` gates actual submission (pause = off);
# ``execution_mode`` selects the account (demo sandbox vs live real). Must stay in
# sync with dashboard._STRATEGY_MODE_FLAGS.
_STRATEGY_MODE_FLAGS = {
    "pause": {"execution_mode": "demo", "submit_orders": False},
    "demo":  {"execution_mode": "demo", "submit_orders": True},
    "live":  {"execution_mode": "live", "submit_orders": True},
}


def set_strategy_override(strategy_id: str, mode: str) -> bool:
    """Set a per-strategy execution mode override in control-state.json.
    mode must be one of: 'pause' (no orders), 'demo' (sandbox) or 'live' (real venue)."""
    sid = str(strategy_id or "").strip()
    mode = str(mode or "").strip().lower()
    mode = _LEGACY_STRATEGY_MODE_ALIASES.get(mode, mode)
    if not sid or mode not in _VALID_STRATEGY_MODES:
        return False
    flags = _STRATEGY_MODE_FLAGS[mode]
    state = load_control_state()
    overrides = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
    overrides[sid] = {"mode": mode, **flags, "set_at": now_iso()}
    state["strategy_overrides"] = overrides
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def clear_strategy_override(strategy_id: str) -> bool:
    """Remove a strategy override, reverting to the strategy file's execution_mode/submit_orders."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    state = load_control_state()
    overrides = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
    if sid not in overrides:
        return False
    overrides.pop(sid)
    state["strategy_overrides"] = overrides
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def set_accounting_start(strategy_id: str, start_ms: "int | None") -> bool:
    """Set (or clear) a per-strategy accounting-window start in control-state.json.

    ``start_ms`` is a millisecond epoch: P&L from closes strictly before it is locked
    out of the strategy's current window (the ledger keeps the rows; the aggregation
    simply ignores them — see pnl_ledger.read_closed_trades). ``None``/absent clears
    the window. Additive: mirrors set_strategy_override; leaves strategy_overrides
    untouched. Returns True on a successful write."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    if start_ms is None:
        return clear_accounting_start(sid)
    try:
        ts = int(start_ms)
    except (TypeError, ValueError):
        return False
    if ts < 0:
        return False
    state = load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    windows[sid] = {"accounting_start_at": ts, "set_at": now_iso()}
    state["accounting_windows"] = windows
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def clear_accounting_start(strategy_id: str) -> bool:
    """Remove a strategy's accounting window (revert to the whole-ledger total)."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    state = load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    if sid not in windows:
        return False
    windows.pop(sid)
    state["accounting_windows"] = windows
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def accounting_start_for(strategy_id: str) -> "int | None":
    """The strategy's accounting-window start (ms epoch), or None if unset."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    windows = load_control_state().get("accounting_windows") or {}
    entry = windows.get(sid)
    if isinstance(entry, dict):
        try:
            v = entry.get("accounting_start_at")
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


# Phase A (A2) -- global trading_state. Collapsed to ONE extra state ("reducing" /
# risk-off): a Nautilus-style HALTED that also blocks closes would contradict HermX's
# deliberate never-block-a-close invariant (a close only REDUCES exposure). "active" is
# normal; "reducing" blocks every non-close order at the ExecutionService gate.
_VALID_TRADING_STATES = frozenset({"active", "reducing"})


def set_trading_state(state: str) -> bool:
    """Set the global trading_state in control-state.json. Validates the input:
    only 'active' or 'reducing' are accepted (anything else is a no-op returning
    False, so a typo can never disarm the gate)."""
    st = str(state or "").strip().lower()
    if st not in _VALID_TRADING_STATES:
        return False
    cs = load_control_state()
    cs["trading_state"] = st
    cs["updated_at"] = now_iso()
    save_control_state(cs)
    return True


def get_trading_state() -> str:
    """Read the global trading_state, defaulting to 'active'. An unknown/legacy value
    fails open to 'active' (normal trading) -- 'reducing' is the safe EXTRA state, and
    the live kill switch still guards real-venue submits independently."""
    st = str(load_control_state().get("trading_state") or "active").strip().lower()
    return st if st in _VALID_TRADING_STATES else "active"


def clear_trading_state() -> bool:
    """Reset trading_state to 'active' (the gate no-op)."""
    return set_trading_state("active")


def _strategy_config_for_readiness(readiness: "dict | None") -> dict:
    """Resolve the strategy config (with its capital block) for a readiness record so
    the A1 pre-trade notional ceiling can read ``capital.max_notional_usd``. Reads the
    module-bound STRATEGIES by the readiness ``strategy_id``; empty dict when unknown."""
    sid = str((readiness or {}).get("strategy_id") or "").strip()
    if not sid:
        return {}
    return STRATEGIES.get(sid) or {}


def _canonical_state_json(state: dict) -> str:
    """Canonical JSON for hashing: sorted keys, compact separators. Independent of
    the pretty-printed on-disk form, so checkpoint formatting cannot affect the
    integrity hash."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a rename/replace inside it is durable."""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError):
        pass


def _atomic_json_dump(path: Path, obj: dict) -> None:
    """Write JSON atomically + durably (tmp -> fsync -> replace -> dir fsync).
    Propagates OSError so the caller can fail closed on a full disk
    (REFACTOR_PLAN.md:221)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)


def _fail_closed_state_write(operation: str, exc: Exception, context: dict | None = None) -> None:
    """A journal/checkpoint write failed (e.g. ENOSPC). Emit a loud, operator-visible
    ERROR + a record on the alert ledger, then let the caller re-raise so the money
    path is BLOCKED rather than proceeding on unpersisted/lost state
    (REFACTOR_PLAN.md:221, fail closed to no-submit). The alert append is
    best-effort: the alert ledger may sit on the same full disk, so the re-raise —
    not the alert — is the real fail-closed guarantee."""
    logging.error("STATE WRITE FAILED (%s) -- FAILING CLOSED, submission blocked: %s", operation, exc)
    try:
        append_jsonl(ALERTS_LEDGER, {"ts": now_iso(), "kind": "state", "alert": "STATE_WRITE_FAILED", "operation": operation, "error": str(exc), "context": context or {}})
    except Exception:
        pass


def build_strategy_execution_readiness(record: dict) -> dict:
    normalized = record.get("normalized") or {}
    strategy = record.get("strategy_config") or {}
    # execution_mode is operative: ``sandbox`` is True for demo and False ONLY for live.
    # The resolved ``simulated_trading`` (= sandbox) and the ``execution_mode`` flow into
    # readiness so the ExecutionService gate can require HERMX_LIVE_TRADING for live
    # submissions and the adapter sandboxes accordingly.
    execution_mode = str((strategy or {}).get("execution_mode") or "demo").lower()
    # submit_orders gates actual submission. Absent in the file -> default True (the
    # historical "submit" posture); Pause sets it False (validate+ledger, no order).
    submit_orders = bool((strategy or {}).get("submit_orders", True))
    # Runtime override from control-state.json (set_strategy_override / dashboard UI).
    # Checked live per-signal so no restart is needed when the operator changes mode.
    # An override carries BOTH execution_mode and submit_orders (see _STRATEGY_MODE_FLAGS).
    _cs_overrides = (load_control_state().get("strategy_overrides") or {})
    _cs_ov = _cs_overrides.get(record.get("strategy_id") or (strategy or {}).get("strategy_id") or "")
    if isinstance(_cs_ov, dict) and _cs_ov.get("execution_mode"):
        execution_mode = str(_cs_ov["execution_mode"]).lower()
    if isinstance(_cs_ov, dict) and "submit_orders" in _cs_ov:
        submit_orders = bool(_cs_ov["submit_orders"])
    sandbox = (execution_mode != "live")  # demo -> True; live -> False
    # submit_orders is the submission gate: Pause -> False (no orders to either venue);
    # Demo/Live -> True. execution_mode then decides sandbox vs real account.
    live_execution_enabled = bool(submit_orders)
    live_allowed = live_execution_enabled
    direction = "long" if normalized.get("side") == "buy" else "short"
    signal_identity = _signal_identity(normalized)
    # Reversal signals submit two legs (close the opposite position, then open the new
    # one). Each leg needs its OWN clOrdId or the venue rejects the second as a duplicate.
    # ``client_order_id`` stays the OPEN-leg id (the leg that defines the final position
    # and the journal dedupe key); the close-leg id is carried alongside it.
    client_order_id_close = stable_client_order_id(signal_identity, role="close")
    client_order_id_open = stable_client_order_id(signal_identity, role="open")
    client_order_id = client_order_id_open
    base_notional = strategy_budget_usd(strategy) * float(strategy.get("leverage") or 1.0)
    planned_notional = float(dec_notional(base_notional))
    # Exchange-agnostic instruction contract (Phase 6 / M3, ARCHITECTURE.md). ``td_mode``
    # below is the OKX translation of this same value, so derive both from one expression.
    margin_mode = strategy.get("margin_mode", "isolated")
    instrument = strategy_instrument(strategy)
    # Derive ccxt_default_type from strategy instrument.type (e.g., swap, spot, future)
    instrument_type = resolve_default_type(instrument)
    plan = {
        "mode": "strategy_file_live_order_enabled" if live_allowed else "strategy_file_trial_no_order",
        "live_execution_enabled": live_allowed,
        "execution_mode": execution_mode,
        "simulated_trading": sandbox,
        "execution_policy": f"strategy_file:{normalized.get('strategy_id')}",
        "execution_policy_label": strategy.get("name") or normalized.get("strategy_id"),
        "exchange": EXEC_BACKEND,
        "ccxt_default_type": instrument_type,
        "route": EXECUTION_DEFAULTS["route"],
        "account": EXECUTION_DEFAULTS["account"],
        "symbol": normalized.get("symbol"),
        "inst_id": instrument.get("inst_id"),
        "expected_leverage": strategy.get("leverage"),
        "td_mode": margin_mode,
        # --- Exchange-agnostic instruction contract (Phase 6 / M3) ---
        # THE wire contract going forward (ARCHITECTURE.md). The inst_id / td_mode
        # keys above stay present but are now adapter-derived translations of these:
        # the CCXT adapter maps inst_id<->symbol and tdMode<-margin_mode. Every value
        # here is byte-identical to its OKX-named twin / execution_intent field, so orders
        # and downstream readers are unchanged.
        "instrument": instrument,
        "strategy_id": strategy.get("strategy_id") or normalized.get("strategy_id"),
        "asset": strategy.get("asset") or normalized.get("symbol"),
        "target_side": direction,
        "target_notional_usd": planned_notional,
        "margin_mode": margin_mode,
        "leverage": strategy.get("leverage"),
        "timeframe": normalized.get("timeframe"),
        "tv_time": normalized.get("tv_time"),
        "signal_side": normalized.get("side"),
        "signal_price": normalized.get("tv_signal_price"),
        "execution_intent": {
            "policy": f"strategy_file:{normalized.get('strategy_id')}",
            "decision": "TRADE",
            "risk_weight": 1.0,
            "target_direction": direction,
            "actions": ["CLOSE_OPPOSITE_IF_ANY", f"OPEN_{direction.upper()}"],
            "base_notional_usd": strategy_budget_usd(strategy),
            "planned_notional_usd": planned_notional,
            "client_order_id": client_order_id,
            "client_order_id_open": client_order_id_open,
            "client_order_id_close": client_order_id_close,
        },
        "okx_fill": {
            "status": "not_sent_strategy_trial" if not live_allowed else "ready_to_send_when_strategy_promoted",
            "order_id": None,
            "client_order_id": client_order_id,
            "avg_fill_price": None,
            "filled_size": None,
            "fee_usd": None,
            "slippage_pct": None,
            "position_after_order": None,
        },
        "block_reason": None if live_allowed else "Duo Base Dev strategy trial is not approved for OKX submission",
    }
    # The separate execution-plan.jsonl ledger was removed entirely (constant + sweep
    # entry): nothing consumed it. The authoritative submission outcome is recorded to
    # pipeline.jsonl (stage="execution"), which the dashboard reads.
    return plan


# ``live_trading_enabled`` (the global HERMX_LIVE_TRADING kill switch) now lives in
# ``hermx_shared`` -- a pure env read with no module state -- and is imported above so
# existing references (`live_trading_enabled(...)`) keep working unchanged. It is wired
# into the ExecutionService gate for ``execution_mode == "live"`` submissions.


# ---------------------------------------------------------------------------
# Submission-outcome state machine + write-ahead order journal
# (REFACTOR_PLAN.md:204 write-ahead ordering, :216 state machine -- Phase 1 task 5).
# ---------------------------------------------------------------------------

# Legal transitions. None is the implicit pre-existence state (a brand-new clOrdId).
# PLANNED -> REJECTED covers an order aborted *before* it is ever sent. SUBMITTED and
# UNKNOWN may both still resolve to any terminal outcome (or re-UNKNOWN, so a resolver
# can re-reconcile). Terminal states {FILLED, REJECTED} are frozen: no transitions.
_ORDER_STATE_TRANSITIONS: "dict[str | None, frozenset[str]]" = {
    None: frozenset({ORDER_STATE_PLANNED}),
    ORDER_STATE_PLANNED: frozenset({ORDER_STATE_SUBMITTED, ORDER_STATE_REJECTED}),
    ORDER_STATE_SUBMITTED: frozenset({ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_UNKNOWN}),
    ORDER_STATE_UNKNOWN: frozenset({ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_UNKNOWN}),
    ORDER_STATE_FILLED: frozenset(),
    ORDER_STATE_REJECTED: frozenset(),
}


def order_state_can_transition(old: "str | None", new: str) -> bool:
    """PURE predicate: is ``old -> new`` a legal order-state transition? Unknown
    ``old`` states and any ``new`` that is not reachable return False (fail closed)."""
    return new in _ORDER_STATE_TRANSITIONS.get(old, frozenset())


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


def _read_order_journal_tail(path: Path) -> list:
    """Tolerant per-line reader for the ORDER journal live segment.

    Unlike read_jsonl_tolerant (which RAISES on mid-file corruption -- correct for the
    position journal where corruption means money state is wrong), a single corrupt
    order-journal line must NOT brick the index and block ALL submits. We log it loudly,
    quarantine the offending line to ``<path>.corrupt`` for forensics, and skip it --
    that one order is effectively failed-closed (absent from the index) while every other
    order keeps flowing. A truncated trailing line is the expected torn-tail case."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw:
        return []
    lines = raw.split("\n")
    last_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip():
            last_idx = i
    out: list = []
    corrupt: list = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            if i == last_idx:
                logging.warning("order-journal: quarantined truncated trailing line in %s", path)
            else:
                logging.error("order-journal: skipping corrupt mid-file line %d in %s (order failed closed)", i, path)
            corrupt.append(ln)
    if corrupt:
        try:
            (path.parent / (path.name + ".corrupt")).write_text("\n".join(corrupt), encoding="utf-8")
        except Exception:
            pass
    return out


def _order_index_apply(index: dict, rec: dict) -> None:
    """Fold one order-journal record into the index (latest-by-max-seq, origin-by-min-seq).
    Idempotent in seq, so applying a record already folded in is a no-op."""
    seq = rec.get("seq")
    if not isinstance(seq, int):
        return
    cl = rec.get("cl_ord_id")
    latest = index["latest"]
    cur = latest.get(cl)
    if cur is None or seq > int(cur.get("seq") or -1):
        latest[cl] = rec
    origin = index["origin"]
    cur_origin = origin.get(cl)
    if cur_origin is None or seq < cur_origin[0]:
        origin[cl] = (seq, rec.get("ts"))


def _build_order_index() -> dict:
    """Rebuild the order index from the VERIFIED checkpoint (latest-per-cl + origin,
    subsuming every sealed segment) plus the live-segment tail (records newer than the
    checkpoint). Bounded: the live segment is rotation-capped and sealed segments are
    folded into the checkpoint, so this never replays the full history."""
    index = {"latest": {}, "origin": {}}
    ckpt = _read_order_checkpoint()
    last_seq = -1
    if ckpt is not None:
        last_seq = ckpt["last_seq"]
        for rec in ckpt.get("index_records") or []:
            _order_index_apply(index, rec)
        for cl, seq, ts in ckpt.get("origins") or []:
            cur = index["origin"].get(cl)
            if cur is None or seq < cur[0]:
                index["origin"][cl] = (seq, ts)
    for rec in _read_order_journal_tail(ORDER_JOURNAL_LEDGER):
        s = rec.get("seq")
        if isinstance(s, int) and s > last_seq:
            _order_index_apply(index, rec)
    return index


def _order_index() -> dict:
    """The in-memory order index, built lazily on first use (under the journal lock)."""
    global _order_journal_index
    if _order_journal_index is None:
        _order_journal_index = _build_order_index()
    return _order_journal_index


def _order_journal_next_seq() -> int:
    global _order_journal_seq_cache
    if _order_journal_seq_cache is None:
        last = -1
        cl = _order_checkpoint_last_seq_floor()
        if cl is not None and cl > last:
            last = cl
        for seq, _path in _order_sealed_segment_paths():
            if seq > last:
                last = seq
        for rec in _read_order_journal_tail(ORDER_JOURNAL_LEDGER):
            s = rec.get("seq")
            if isinstance(s, int) and s > last:
                last = s
        _order_journal_seq_cache = last
    _order_journal_seq_cache += 1
    return _order_journal_seq_cache


# --- Order-journal sealed segments + checkpoint (mirrors the position-journal helpers) ---

def _parse_order_sealed_seq(name: str):
    """The seq encoded in a sealed order segment ``order-journal.<seq>.jsonl`` (the last
    seq it covers), or None if the name is not a sealed segment."""
    prefix, suffix = "order-journal.", ".jsonl"
    if name.startswith(prefix) and name.endswith(suffix):
        mid = name[len(prefix):-len(suffix)]
        if mid.isdigit():
            return int(mid)
    return None


def _order_sealed_segment_paths() -> list:
    """Sealed order-journal segments as (seq, path), ascending. The live segment and the
    ``.corrupt`` quarantine file are excluded by the naming rule; the checkpoint (``.json``)
    by suffix."""
    out = []
    for p in LOG_DIR.glob("order-journal.*.jsonl"):
        seq = _parse_order_sealed_seq(p.name)
        if seq is not None:
            out.append((seq, p))
    out.sort(key=lambda t: t[0])
    return out


def _read_all_order_records() -> list:
    """Every order record across sealed segments (seq order) + the live segment, sorted
    by seq. Used by the checkpoint fold; tolerant of corrupt/torn lines."""
    records: list = []
    for _seq, path in _order_sealed_segment_paths():
        records.extend(_read_order_journal_tail(path))
    records.extend(_read_order_journal_tail(ORDER_JOURNAL_LEDGER))
    records.sort(key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else -1)
    return records


def _order_index_hash(index_records: list, origins: list) -> str:
    payload = {
        "index_records": sorted(index_records, key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else -1),
        "origins": sorted(origins, key=lambda o: str(o[0])),
    }
    return hashlib.sha256(_canonical_state_json(payload).encode("utf-8")).hexdigest()


def _order_checkpoint_last_seq_floor() -> "int | None":
    """The checkpoint's last_seq used only as a monotonic seq floor (best-effort; a hash
    mismatch is irrelevant here -- an over-high floor only skips seq numbers, never
    reuses them), so read it without the full verify."""
    if not ORDER_JOURNAL_CHECKPOINT_FILE.exists():
        return None
    try:
        ckpt = json.loads(ORDER_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
        ls = ckpt.get("last_seq")
        return ls if isinstance(ls, int) else None
    except Exception:
        return None


def _read_order_checkpoint() -> "dict | None":
    """Load the order checkpoint with VERIFY-BEFORE-TRUST: returns it only if it parses,
    its versions are not from a newer writer, and its stored hash recomputes over the
    stored index/origins. Any failure is loud and returns None (full-tail rebuild)."""
    if not ORDER_JOURNAL_CHECKPOINT_FILE.exists():
        return None
    try:
        ckpt = json.loads(ORDER_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.error("order-journal checkpoint unreadable (%s) -- DISCARDING", exc)
        return None
    sv = ckpt.get("schema_version")
    cv = ckpt.get("checkpoint_version")
    if not isinstance(sv, int) or not isinstance(cv, int) or sv > ORDER_JOURNAL_SCHEMA_VERSION or cv > ORDER_JOURNAL_CHECKPOINT_VERSION:
        logging.error("order-journal checkpoint from a newer writer (schema=%r checkpoint=%r) -- DISCARDING", sv, cv)
        return None
    last_seq = ckpt.get("last_seq")
    index_records = ckpt.get("index_records")
    origins = ckpt.get("origins")
    if not isinstance(last_seq, int) or not isinstance(index_records, list) or not isinstance(origins, list):
        logging.error("order-journal checkpoint missing last_seq/index_records/origins -- DISCARDING")
        return None
    if ckpt.get("state_hash") != _order_index_hash(index_records, origins):
        logging.error("order-journal checkpoint state_hash MISMATCH -- DISCARDING corrupt checkpoint")
        return None
    return ckpt


def _rotate_order_live_segment(last_seq: int) -> None:
    """Seal the live order segment to ``order-journal.<last_seq>.jsonl`` and start a fresh
    one. Called only AFTER a verified checkpoint covering last_seq is fsync'd."""
    if not ORDER_JOURNAL_LEDGER.exists():
        return
    sealed = LOG_DIR / f"order-journal.{last_seq}.jsonl"
    os.replace(ORDER_JOURNAL_LEDGER, sealed)
    ORDER_JOURNAL_LEDGER.touch()
    _fsync_dir(LOG_DIR)


def _enforce_order_segment_retention() -> None:
    """Keep the last K sealed order segments; prune older ones (the checkpoint subsumes
    them). HERMX_JOURNAL_SEGMENT_RETENTION < 0 keeps all."""
    if HERMX_JOURNAL_SEGMENT_RETENTION < 0:
        return
    sealed = _order_sealed_segment_paths()
    excess = len(sealed) - HERMX_JOURNAL_SEGMENT_RETENTION
    for _seq, path in sealed[:max(0, excess)]:
        try:
            path.unlink()
        except OSError as exc:
            logging.warning("order-journal: could not prune sealed segment %s: %s", path, exc)


def _order_checkpoint_and_rotate() -> None:
    """Fold the full order history (sealed + live) into the latest-per-cl index + origins,
    write a verified checkpoint, then seal the live segment and prune old sealed ones.
    Simpler than the position-journal twin: the order index is a pure deterministic fold
    of records by seq (no money-state math), so the from-scratch fold IS authoritative and
    no dual-oracle equivalence check is needed. Fail-closed on any write OSError."""
    records = _read_all_order_records()
    if not records:
        return
    index = {"latest": {}, "origin": {}}
    last_seq = -1
    for rec in records:
        s = rec.get("seq")
        if isinstance(s, int) and s > last_seq:
            last_seq = s
        _order_index_apply(index, rec)
    if last_seq < 0:
        return
    index_records = list(index["latest"].values())
    origins = [[cl, seq, ts] for cl, (seq, ts) in index["origin"].items()]
    ckpt = {
        "schema_version": ORDER_JOURNAL_SCHEMA_VERSION,
        "checkpoint_version": ORDER_JOURNAL_CHECKPOINT_VERSION,
        "last_seq": last_seq,
        "index_records": index_records,
        "origins": origins,
        "state_hash": _order_index_hash(index_records, origins),
        "created_at": now_iso(),
    }
    try:
        _atomic_json_dump(ORDER_JOURNAL_CHECKPOINT_FILE, ckpt)  # fsync'd before rotate
        _rotate_order_live_segment(last_seq)
    except OSError as exc:
        _fail_closed_state_write("order-checkpoint-rotate", exc, context={"last_seq": last_seq})
        raise
    _enforce_order_segment_retention()
    # The in-memory index already reflects every folded record; rebind it to the
    # freshly-folded structure so it stays the single source of truth post-rotation.
    global _order_journal_index
    _order_journal_index = index


def _maybe_order_checkpoint_and_rotate() -> None:
    """Trigger an order-journal checkpoint+rotation once the live segment grows past
    HERMX_JOURNAL_SEGMENT_MAX_RECORDS, keeping rebuild bounded and disk capped."""
    live = _read_order_journal_tail(ORDER_JOURNAL_LEDGER)
    if len(live) < HERMX_JOURNAL_SEGMENT_MAX_RECORDS:
        return
    _order_checkpoint_and_rotate()


def record_order_state(
    cl_ord_id: "str | None",
    new_state: str,
    intent: "dict | None" = None,
    detail: "dict | None" = None,
    prev_state: "str | None" = None,
) -> dict:
    """Validate ``prev_state -> new_state`` and durably (fsync) append one order-journal
    record. Raises ValueError on an illegal transition (the caller must not persist a
    state the machine forbids). OSError from the durable append propagates to the caller,
    which fail-closes the money path (see execute_if_enabled write-ahead)."""
    if not order_state_can_transition(prev_state, new_state):
        logging.error("ILLEGAL order-state transition for %s: %s -> %s", cl_ord_id, prev_state, new_state)
        raise ValueError(f"illegal order-state transition {prev_state} -> {new_state} for {cl_ord_id}")
    with _ORDER_JOURNAL_LOCK:
        # Ensure the index is built BEFORE appending so the build reads the pre-append
        # live tail; the new record is then folded in explicitly (no double count).
        index = _order_index()
        record = {
            "schema_version": ORDER_JOURNAL_SCHEMA_VERSION,
            "seq": _order_journal_next_seq(),
            "ts": now_iso(),
            "cl_ord_id": cl_ord_id,
            "state": new_state,
            "prev_state": prev_state,
            "intent": canonicalize_decimal_fields(intent or {}),
            "detail": canonicalize_decimal_fields(detail or {}),
        }
        append_jsonl_durable(ORDER_JOURNAL_LEDGER, record)
        _order_index_apply(index, record)
        # Bound the live segment: fold into a verified checkpoint + seal once it grows
        # past the segment cap, so the journal does not grow without limit.
        _maybe_order_checkpoint_and_rotate()
    return record


def _order_intent_from_readiness(readiness: dict) -> dict:
    """The minimal, exchange-agnostic intent persisted on each order-journal record."""
    exec_intent = readiness.get("execution_intent") or {}
    instrument = readiness.get("instrument") or {}
    return {
        "symbol": readiness.get("symbol"),
        "side": readiness.get("signal_side"),
        "inst_id": readiness.get("inst_id") or instrument.get("inst_id"),
        "planned_notional_usd": exec_intent.get("planned_notional_usd"),
        "policy": exec_intent.get("policy"),
        # Issue #20a: persist the resolved (venue, mode) the order was submitted to so
        # the order-state reconciler queries the SAME account -- not the global OKX-demo
        # default. Venue comes from the strategy instrument (strategy_instrument); mode /
        # simulated_trading are the readiness-resolved effective mode (Phase 0). Orders
        # journalled before this field existed simply lack it -> reconcile falls back to
        # the OKX-demo default (unchanged pre-#20a behavior).
        "venue": instrument.get("exchange"),
        "mode": readiness.get("execution_mode"),
        "simulated_trading": readiness.get("simulated_trading"),
    }


def _cl_ord_id_from_readiness(readiness: dict) -> "str | None":
    exec_intent = readiness.get("execution_intent") or {}
    fill = readiness.get("okx_fill") or {}
    return exec_intent.get("client_order_id") or fill.get("client_order_id")


def load_open_orders() -> list[dict]:
    """Restart-recovery reader consumed by Task 4 reconciliation: the LATEST record
    (highest seq) per cl_ord_id whose state is still non-terminal
    (PLANNED/SUBMITTED/UNKNOWN). Reads the in-memory order index (verified checkpoint +
    live-segment tail) rather than re-folding the journal, so records that have rotated
    into sealed segments are still seen. Terminal (FILLED/REJECTED) orders are omitted.

    Each returned record is a COPY of the latest with an added ``origin_ts`` -- the ts of
    the order's FIRST (lowest-seq) journal record. The lifecycle backstop measures age
    from origin_ts so re-recording (e.g. UNKNOWN->UNKNOWN) can never reset the clock.

    Reads from the bounded in-memory index (checkpoint + live tail), never the full
    journal -- so it stays O(open orders) regardless of total journal length."""
    with _ORDER_JOURNAL_LOCK:
        index = _order_index()
        latest = index["latest"]
        origin = index["origin"]
        out: list[dict] = []
        for cl, rec in latest.items():
            if rec.get("state") not in ORDER_NON_TERMINAL_STATES:
                continue
            enriched = dict(rec)
            enriched["origin_ts"] = origin.get(cl, (None, rec.get("ts")))[1]
            out.append(enriched)
    return out


def latest_order_record(cl_ord_id: str | None) -> dict | None:
    """Latest journal record for a clOrdId (the idempotency/dedupe authority). Reads the
    in-memory index -- O(1) -- instead of re-folding the whole journal on every submit."""
    cl = str(cl_ord_id or "").strip()
    if not cl:
        return None
    with _ORDER_JOURNAL_LOCK:
        return _order_index()["latest"].get(cl)


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

def _reconcile_float(value, default=0.0):
    """Tolerant float coercion for normalized query fields (PURE)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def map_order_outcome(order: "dict | None", ordered: "float | None" = None) -> tuple:
    """PURE: map a normalized order (or None) to a reconciliation outcome.

    Returns ``(state, partial, reason)`` per the :211-:212 mapping rules:
      * state=partially_filled OR (0 < accFillSz < ordered) -> FILLED, partial=True
      * state=filled (and not a known partial)              -> FILLED, partial=False
      * state=canceled with accFillSz=0                      -> REJECTED (canceled)
      * state=canceled with accFillSz>0                      -> FILLED, partial=True
      * not-found (absent from get_order/pending/archive)    -> UNKNOWN (not_found)
      * state=live (non-terminal)                            -> SUBMITTED (keep polling)
      * any other / inconclusive                             -> UNKNOWN

    MONEY-SAFETY: absence is NEVER an auto-rejection. A not_found order may have filled
    and aged out of the queryable windows, or the query layer may be transiently
    failing; concluding REJECTED there would drop a live position as flat. Absence maps
    to UNKNOWN so the order stays tracked (backoff re-polls it; the periodic resolver +
    lifecycle backstop chase it). The ONLY venue-confirmed rejection is canceled with
    zero fill.
    """
    if order is None:
        return ORDER_STATE_UNKNOWN, False, "not_found"
    state = str(order.get("state") or "").lower()
    acc = _reconcile_float(order.get("acc_fill_sz"), 0.0)
    is_partial_by_size = ordered is not None and ordered > 0 and 0.0 < acc < ordered
    if state == "not_found":
        return ORDER_STATE_UNKNOWN, False, "not_found"
    if state == "partially_filled":
        return ORDER_STATE_FILLED, True, "partially_filled"
    if state == "filled":
        if is_partial_by_size:
            return ORDER_STATE_FILLED, True, "partial_by_size"
        return ORDER_STATE_FILLED, False, "filled"
    if state == "canceled":
        if acc > 0.0:
            return ORDER_STATE_FILLED, True, "canceled_after_partial_fill"
        # canceled_zero_fill is the ONLY venue-confirmed rejection -- but only when the
        # zero is REAL. A missing/malformed acc_fill_sz must NOT be coerced to 0 and
        # rejected: a canceled-after-partial would then be dropped as flat. Keep it
        # UNKNOWN (report-driven reconcile) so the backoff/resolver chases the true size.
        if _reconcile_float(order.get("acc_fill_sz"), None) is None:
            return ORDER_STATE_UNKNOWN, False, "canceled_fill_size_unavailable"
        return ORDER_STATE_REJECTED, False, "canceled_zero_fill"
    if state == "live":
        # Non-terminal: still working. partial flag only informs the caller's logging.
        return ORDER_STATE_SUBMITTED, is_partial_by_size, "live"
    # error / not_implemented / unknown / empty -> inconclusive, keep it UNKNOWN.
    return ORDER_STATE_UNKNOWN, False, f"inconclusive:{state or 'empty'}"


def _order_is_present(order: "dict | None") -> bool:
    """A normalized order genuinely exists on the venue (vs not_found/error/...)."""
    return isinstance(order, dict) and str(order.get("state") or "").lower() in _PRESENT_ORDER_STATES


def _order_matches(order: dict, ord_id: "str | None", cl_ord_id: "str | None") -> bool:
    """Does a list-returned (pending/archive) order match the keys we are chasing?
    Match by ordId or clOrdId when provided; with neither, accept the first present
    order for the instrument."""
    if not _order_is_present(order):
        return False
    if ord_id and order.get("ord_id") == ord_id:
        return True
    if cl_ord_id and order.get("cl_ord_id") == cl_ord_id:
        return True
    return not ord_id and not cl_ord_id


def reconcile_order_once(executor, lookup: dict) -> dict:
    """One pass of the OKX v5 fallback chain (:209):
       1. GET /trade/order              (instId + ordId preferred, else clOrdId)
       2. GET /trade/orders-pending     (instId) if 1 returns not-found
       3. GET /trade/orders-history-archive (instId, limit) if still absent
    Returns the normalized outcome dict consumed by the backoff driver."""
    inst_id = lookup.get("inst_id")
    ord_id = lookup.get("ord_id")
    cl_ord_id = lookup.get("cl_ord_id")
    ordered = lookup.get("ordered")
    limit = int(lookup.get("history_limit") or RECONCILE_HISTORY_LIMIT)

    matched: "dict | None" = None
    source: "str | None" = None

    if inst_id:
        order = executor.get_order(inst_id, ord_id=ord_id, cl_ord_id=cl_ord_id)
        if _order_is_present(order):
            matched, source = order, "get_order"
    if matched is None and inst_id:
        for cand in executor.get_open_orders(inst_id) or []:
            if _order_matches(cand, ord_id, cl_ord_id):
                matched, source = cand, "orders_pending"
                break
    if matched is None and inst_id:
        for cand in executor.get_order_history_archive(inst_id, limit=limit) or []:
            if _order_matches(cand, ord_id, cl_ord_id):
                matched, source = cand, "orders_history_archive"
                break

    state, partial, reason = map_order_outcome(matched, ordered=ordered)
    return {
        "state": state,
        "partial": partial,
        "reason": reason,
        "matched_order": matched,
        "source": source,
        "acc_fill_sz": _reconcile_float((matched or {}).get("acc_fill_sz"), 0.0),
        "avg_px": (matched or {}).get("avg_px") if matched else None,
        "ord_id": (matched or {}).get("ord_id") if matched else ord_id,
        "cl_ord_id": (matched or {}).get("cl_ord_id") if matched else cl_ord_id,
    }


def reconcile_order_with_backoff(
    executor,
    lookup: dict,
    *,
    max_attempts: int = RECONCILE_MAX_ATTEMPTS,
    base_delay: float = RECONCILE_BASE_DELAY_SECONDS,
    cap_delay: float = RECONCILE_CAP_DELAY_SECONDS,
    wall_clock_budget: float = RECONCILE_WALL_CLOCK_BUDGET_SECONDS,
    sleep=time.sleep,
    clock=time.time,
) -> dict:
    """Bounded exponential-backoff reconciliation (:213). Terminal outcomes
    (FILLED/REJECTED, incl. not_found) return immediately; a non-terminal (live)
    order is re-polled with delays 0.5s,1s,2s,4s (capped 8s). When the attempt or
    wall-clock bound is exhausted while still non-terminal, the outcome is UNKNOWN
    and a RECONCILE_MISMATCH is the caller's responsibility. ``sleep``/``clock`` are
    injectable so tests exercise the bound with no real waiting. The submission is
    NEVER retried -- only the read-only status query is."""
    start = clock()
    last: "dict | None" = None
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        last = reconcile_order_once(executor, lookup)
        if last["state"] in ORDER_TERMINAL_STATES:
            last["attempts"] = attempts
            last["elapsed_s"] = round(clock() - start, 3)
            return last
        if attempts >= max_attempts:
            break
        delay = min(cap_delay, base_delay * (2 ** attempt))
        if (clock() - start) + delay > wall_clock_budget:
            break
        sleep(delay)

    outcome = dict(last) if last else {
        "matched_order": None, "source": None, "acc_fill_sz": 0.0, "avg_px": None,
        "ord_id": lookup.get("ord_id"), "cl_ord_id": lookup.get("cl_ord_id"), "partial": False,
    }
    prior_reason = (last or {}).get("reason") or "no_result"
    outcome["state"] = ORDER_STATE_UNKNOWN
    outcome["reason"] = f"deadline_exhausted:{prior_reason}"
    outcome["attempts"] = attempts
    outcome["elapsed_s"] = round(clock() - start, 3)
    return outcome


# emit_operator_alert / emit_auth_failure_alert / maybe_emit_queue_saturation_alert
# were extracted to src/alerts.py (Phase 0) and are re-exported at the top of this
# module. They read ALERTS_LEDGER / HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS /
# ALERT_AUTH_FAILURE / ALERT_QUEUE_SATURATION / QUEUE_SATURATION_ALERT_DEPTH (still
# defined here) lazily via `import webhook_receiver as _wr`.


def emit_reconcile_alert(kind: str, detail: dict) -> dict:
    """Reconcile alert row in the unified ledger (kind="reconcile") + Task 6 operator
    transport (emit_operator_alert writes a paired kind="operator" row)."""
    record = {"ts": now_iso(), "kind": "reconcile", "alert": kind, "detail": detail or {}}
    try:
        append_jsonl(ALERTS_LEDGER, record)
    except OSError as exc:
        logging.error("failed to write reconcile alert %s: %s", kind, exc)
    emit_operator_alert(kind, detail or {}, severity="warning")
    return record


def reconcile_position_drift(executor, journal_positions: dict, venue: str, mode: str) -> list:
    """OBSERVE-ONLY (B1): detect journal-vs-venue position drift and alert. NEVER
    auto-corrects, cancels, or submits.

    Delegates detection to the adapter's pure ``detect_position_drift`` (which reads
    ``executor.get_positions()`` and degrades to ``[]`` on any venue error), then logs
    each drift as a WARNING and emits a ``RECONCILE_MISMATCH`` (type=position_drift).
    Returns the drift list (also useful for tests / the dashboard snapshot)."""
    from executors.ccxt_adapter import detect_position_drift
    drifts = detect_position_drift(executor, journal_positions, venue, mode)
    for d in drifts:
        logging.warning(
            "position_drift inst_id=%s journal_qty=%s venue_qty=%s drift=%s venue=%s mode=%s",
            d.get("inst_id"), d.get("journal_qty"), d.get("venue_qty"), d.get("drift"), venue, mode,
        )
        emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, {
            "stage": "position_drift",
            "type": "position_drift",
            "inst_id": d.get("inst_id"),
            "journal_qty": d.get("journal_qty"),
            "venue_qty": d.get("venue_qty"),
            "drift": d.get("drift"),
            "venue": venue,
            "mode": mode,
        })
    return drifts


def _effective_execution_config(order_intent: "dict | None" = None) -> dict:
    """The execution config the write path actually resolves: the adapter selector
    (EXEC_BACKEND, which already honors HERMX_EXEC_BACKEND) plus the venue+mode to
    query.

    Issue #20a: the order-state reconciler must query the SAME (venue, mode) the order
    was submitted to. When an ``order_intent`` (from the order-journal record) is given,
    its persisted ``venue`` / ``simulated_trading`` override the OKX-demo default so a
    Bybit-live order is checked on Bybit-live, not OKX-demo. Absent an intent (or a
    legacy intent without those fields) it falls back to EXECUTION_DEFAULTS'
    ``ccxt_exchange`` (okx) and leaves ``simulated_trading`` unset -> the adapter
    defaults to the demo sandbox (the safe pre-#20a fallback)."""
    exec_cfg = {"exchange": EXEC_BACKEND, "ccxt_exchange": EXECUTION_DEFAULTS["ccxt_exchange"]}
    if isinstance(order_intent, dict):
        venue = order_intent.get("venue")
        if venue:
            exec_cfg["ccxt_exchange"] = str(venue).strip().lower()
        simulated = order_intent.get("simulated_trading")
        if simulated is not None:
            exec_cfg["simulated_trading"] = bool(simulated)
    return {"execution": exec_cfg}


def _reconciliation_executor(order_intent: "dict | None" = None):
    """Build the read-only query executor for an order's (venue, mode), or None if
    unavailable. Constructed lazily so a missing factory / bad config simply disables
    reconciliation rather than crashing the receiver (fail closed to observe-only).

    Uses _effective_execution_config(order_intent) so reconcile always queries the
    venue+account the order was submitted to (#20a). Called with no argument it yields
    the OKX-demo default executor -- the pre-#20a global executor and the fallback for
    orders whose journal record predates venue/mode persistence."""
    if ExecutorFactory is None:
        return None
    try:
        return ExecutorFactory.create(_effective_execution_config(order_intent), ROOT)
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("reconciliation executor unavailable: %s", exc)
        return None


def _executor_for_order(intent: "dict | None", cache: dict, default_executor):
    """Resolve the read-only reconcile executor for ONE order from the (venue, mode)
    persisted on its journal intent (#20a).

    Orders journalled before venue/mode persistence carry neither field -> return the
    caller's ``default_executor`` (OKX-demo), i.e. unchanged pre-#20a behavior. Built
    executors are cached by ``(venue, simulated)`` so N orders sharing one account
    reuse a single authenticated client rather than opening N duplicates."""
    if not isinstance(intent, dict):
        return default_executor
    venue = intent.get("venue")
    simulated = intent.get("simulated_trading")
    if not venue and simulated is None:
        return default_executor  # legacy order: OKX-demo default, unchanged
    key = (
        str(venue or EXECUTION_DEFAULTS["ccxt_exchange"]).strip().lower(),
        True if simulated is None else bool(simulated),
    )
    if key not in cache:
        cache[key] = _reconciliation_executor(intent)
    return cache[key]


def reconcile_startup(executor=None) -> dict:
    """One-time startup reconcile bootstrap (:215, acceptance :236). OBSERVE-ONLY:
    reconcile every still-open order (load_open_orders) against the exchange and,
    where the venue reports a terminal outcome and the journal state legally allows
    it, write the authoritative terminal transition.

    Sets RECONCILE_STARTUP_COMPLETE + RECONCILE_STARTUP_AT for FUTURE enforcement; it
    does NOT auto-trade and does NOT hard-block submission in this task. ``summary``
    keeps an (always-empty) ``position_mismatches`` list for backward-compatible shape.
    Returns a summary dict (also useful for tests)."""
    global RECONCILE_STARTUP_COMPLETE, RECONCILE_STARTUP_AT
    # When a caller passes an executor (tests / injected), use it for every order
    # (unchanged behavior). In production (executor is None) resolve a per-order
    # executor from each order's persisted (venue, mode) so a Bybit-live order is
    # checked on Bybit-live, not OKX-demo (#20a). ``default_executor`` is the OKX-demo
    # fallback for legacy orders that predate venue/mode persistence.
    explicit_executor = executor is not None
    default_executor = executor if explicit_executor else _reconciliation_executor()
    _exec_cache: dict = {}
    summary = {"open_orders": [], "position_mismatches": [], "executor_available": default_executor is not None, "errors": []}

    if default_executor is not None:
        try:
            open_orders = load_open_orders()
        except Exception as exc:  # pragma: no cover - tolerant
            open_orders = []
            summary["errors"].append(f"load_open_orders: {exc}")
        for rec in open_orders:
            cl = rec.get("cl_ord_id")
            cur_state = rec.get("state")
            intent = rec.get("intent") or {}
            lookup = {"inst_id": intent.get("inst_id"), "cl_ord_id": cl}
            order_executor = default_executor if explicit_executor else _executor_for_order(intent, _exec_cache, default_executor)
            if order_executor is None:
                summary["errors"].append(f"executor_unavailable[{cl}]")
                continue
            try:
                outcome = reconcile_order_once(order_executor, lookup)
            except Exception as exc:  # pragma: no cover - tolerant
                summary["errors"].append(f"reconcile_order_once[{cl}]: {exc}")
                continue
            recon_state = outcome["state"]
            wrote = False
            # Observe-only: only persist a LEGAL terminal transition (e.g. SUBMITTED/
            # UNKNOWN -> FILLED/REJECTED). PLANNED->FILLED etc. is illegal and skipped.
            if recon_state in ORDER_TERMINAL_STATES and order_state_can_transition(cur_state, recon_state):
                try:
                    record_order_state(
                        cl, recon_state, intent=intent,
                        detail={"startup_reconcile": True, "reason": outcome["reason"], "source": outcome["source"]},
                        prev_state=cur_state,
                    )
                    wrote = True
                except (ValueError, OSError) as exc:  # pragma: no cover - tolerant
                    summary["errors"].append(f"record_order_state[{cl}]: {exc}")
            if recon_state != cur_state and not (recon_state in ORDER_TERMINAL_STATES and wrote):
                # Non-persisted divergence (e.g. still UNKNOWN) is still worth alerting.
                emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, {
                    "stage": "startup_open_order", "cl_ord_id": cl,
                    "journal_state": cur_state, "reconciled_state": recon_state, "reason": outcome["reason"],
                })
            summary["open_orders"].append({
                "cl_ord_id": cl, "from": cur_state, "outcome": recon_state,
                "reason": outcome["reason"], "wrote_transition": wrote,
            })

    RECONCILE_STARTUP_COMPLETE = True
    RECONCILE_STARTUP_AT = now_iso()
    logging.info(
        "RECONCILE_STARTUP_COMPLETE at=%s executor_available=%s open_orders=%d position_mismatches=%d errors=%d",
        RECONCILE_STARTUP_AT, summary["executor_available"], len(summary["open_orders"]),
        len(summary["position_mismatches"]), len(summary["errors"]),
    )
    return summary


def unknown_resolver_enabled() -> bool:
    """Observe-only gate for the PERIODIC background resolver (unknown_resolver_loop).

    Defaults ON. INTERVAL_SECONDS is the single knob (merged the former
    HERMX_UNKNOWN_RESOLVER_ENABLED bool per the flag audit): <= 0 disables the daemon
    thread. Like the other two paths it only updates the order journal / emits alerts
    and never submits, cancels, or auto-trades.
    """
    return UNKNOWN_RESOLVER_INTERVAL_SECONDS > 0


def _order_age_seconds(order_record: dict, now_ts: "str | None" = None) -> "float | None":
    order_ts = parse_tv_time(order_record.get("ts"))
    if order_ts is None:
        return None
    now_dt = parse_tv_time(now_ts) if now_ts else datetime.now(timezone.utc)
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    return max(0.0, (now_dt - order_ts).total_seconds())


def _resolve_planned_orphan(executor, rec: dict, lookup: dict, age_seconds: "float | None", summary: dict) -> None:
    """Lifecycle backstop for a crash-orphaned PLANNED order (closes the gap where a
    PLANNED order could never be resolved: the SUBMITTED/UNKNOWN resolver excluded it and
    PLANNED->UNKNOWN is illegal).

    Write-ahead ordering guarantees SUBMITTED is journalled BEFORE executor.execute() is
    called, so a record stuck at PLANNED crashed BEFORE the submit -- it was never sent.
    Once it is older than PLANNED_ORDER_TIMEOUT_SECONDS we confirm the venue has no record
    (single read-only pass, no backoff sleeps) and then take the LEGAL PLANNED->REJECTED
    transition with reason ``never_submitted`` + an operator alert. Idempotency is
    preserved: the rejected record is terminal, so the deterministic cl_ord_id stays
    deduped. A still-fresh PLANNED order may be an in-process submit between the two
    write-ahead writes, so it is left untouched. OBSERVE-ONLY: never submits/cancels."""
    cl_ord_id = rec.get("cl_ord_id")
    intent = rec.get("intent") or {}
    symbol = intent.get("symbol")

    if age_seconds is None or age_seconds <= PLANNED_ORDER_TIMEOUT_SECONDS:
        summary["pending"] += 1  # still within the in-flight submit window
        return

    try:
        outcome = reconcile_order_once(executor, lookup)
    except Exception as exc:  # pragma: no cover - defensive
        summary["errors"].append(f"reconcile_planned[{cl_ord_id}]: {exc}")
        emit_operator_alert(
            "PLANNED_RESOLVER_ERROR",
            {"cl_ord_id": cl_ord_id, "symbol": symbol, "error": str(exc)},
            severity="error",
        )
        return

    if _order_is_present(outcome.get("matched_order")):
        # ANOMALY: the venue knows an order we believe was never sent. Do NOT reject --
        # promote PLANNED->SUBMITTED (legal) so the standard reconciliation resolves it,
        # and alert loudly.
        if order_state_can_transition(ORDER_STATE_PLANNED, ORDER_STATE_SUBMITTED):
            try:
                record_order_state(
                    cl_ord_id,
                    ORDER_STATE_SUBMITTED,
                    intent=intent,
                    detail={"planned_backstop": True, "reason": "planned_found_on_venue", "source": outcome.get("source")},
                    prev_state=ORDER_STATE_PLANNED,
                )
            except (ValueError, OSError) as exc:
                summary["errors"].append(f"record_planned_submitted[{cl_ord_id}]: {exc}")
        emit_operator_alert(
            RECONCILE_ALERT_PLANNED_ON_VENUE,
            {"cl_ord_id": cl_ord_id, "symbol": symbol, "age_s": round(age_seconds, 3), "source": outcome.get("source")},
            severity="error",
        )
        summary["pending"] += 1
        return

    # Venue has no record -> never submitted. Legal PLANNED -> REJECTED, idempotency-safe.
    if order_state_can_transition(ORDER_STATE_PLANNED, ORDER_STATE_REJECTED):
        try:
            record_order_state(
                cl_ord_id,
                ORDER_STATE_REJECTED,
                intent=intent,
                detail={
                    "planned_backstop": True,
                    "reason": "never_submitted",
                    "age_s": round(age_seconds, 3),
                    "timeout_s": PLANNED_ORDER_TIMEOUT_SECONDS,
                },
                prev_state=ORDER_STATE_PLANNED,
            )
            summary["resolved"] += 1
            summary["never_submitted"] += 1
            emit_operator_alert(
                RECONCILE_ALERT_PLANNED_ABANDONED,
                {
                    "cl_ord_id": cl_ord_id,
                    "symbol": symbol,
                    "age_s": round(age_seconds, 3),
                    "timeout_s": PLANNED_ORDER_TIMEOUT_SECONDS,
                    "reason": "never_submitted",
                },
                severity="warning",
            )
        except (ValueError, OSError) as exc:
            summary["errors"].append(f"record_never_submitted[{cl_ord_id}]: {exc}")


def resolve_unknown_orders_once(executor=None, *, now_ts: "str | None" = None, max_orders: "int | None" = None) -> dict:
    """Task 6 periodic resolver pass for open SUBMITTED/UNKNOWN orders.

    Re-runs reconciliation until terminal or per-order timeout budget expiry. On
    budget expiry emits alerts and persists a per-symbol pause artifact.
    """
    # Per-order (venue, mode) executor resolution mirrors reconcile_startup (#20a): an
    # explicitly-passed executor is used for every order; otherwise each order is checked
    # on the account it was submitted to, with default_executor as the OKX-demo fallback.
    explicit_executor = executor is not None
    default_executor = executor if explicit_executor else _reconciliation_executor()
    _exec_cache: dict = {}
    summary = {
        "checked": 0,
        "resolved": 0,
        "pending": 0,
        "expired": 0,
        "never_submitted": 0,
        "paused_symbols": [],
        "errors": [],
        "executor_available": default_executor is not None,
    }
    if default_executor is None:
        return summary

    limit = max_orders if max_orders is not None else UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK
    candidates = [
        rec
        for rec in load_open_orders()
        if rec.get("state") in {ORDER_STATE_PLANNED, ORDER_STATE_SUBMITTED, ORDER_STATE_UNKNOWN}
    ]
    candidates.sort(key=lambda r: r.get("seq", 0))
    for rec in candidates[: max(0, int(limit))]:
        summary["checked"] += 1
        cl_ord_id = rec.get("cl_ord_id")
        cur_state = rec.get("state")
        intent = rec.get("intent") or {}
        symbol = intent.get("symbol")
        # Age from ORIGIN (first journal record), not the latest -- re-recording must not
        # reset the lifecycle clock. Falls back to the latest ts if origin is missing.
        age_seconds = _order_age_seconds({"ts": rec.get("origin_ts") or rec.get("ts")}, now_ts=now_ts)
        lookup = {"inst_id": intent.get("inst_id"), "cl_ord_id": cl_ord_id}
        order_executor = default_executor if explicit_executor else _executor_for_order(intent, _exec_cache, default_executor)
        if order_executor is None:
            summary["errors"].append(f"executor_unavailable[{cl_ord_id}]")
            continue

        if cur_state == ORDER_STATE_PLANNED:
            _resolve_planned_orphan(order_executor, rec, lookup, age_seconds, summary)
            continue

        if age_seconds is not None and age_seconds > UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS:
            # Lifecycle backstop: alert + pause the symbol, but NEVER auto-close the order
            # (no terminal write -- absence/ambiguity is not proof of any outcome). Dedupe
            # so one stuck order does not re-pause/re-alert every tick: the pause reason is
            # STABLE per (symbol, cl_ord_id, state), so pause_symbol() collapses repeats and
            # only a genuinely NEW pause emits the operator alerts. A symbol-less order
            # cannot be deduped via the pause store, so it always alerts (never swallowed).
            summary["expired"] += 1
            sym_norm = str(symbol or "").strip()
            pause_reason = (
                f"unknown resolver timeout: order {cl_ord_id} stuck {cur_state} "
                f"> {UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS}s"
            )
            newly_paused = pause_symbol(symbol, pause_reason) if sym_norm else False
            if newly_paused:
                summary["paused_symbols"].append(sym_norm)
            if newly_paused or not sym_norm:
                emit_reconcile_alert(
                    RECONCILE_ALERT_MISMATCH,
                    {
                        "stage": "unknown_resolver_timeout",
                        "cl_ord_id": cl_ord_id,
                        "symbol": symbol,
                        "state": cur_state,
                        "age_s": round(age_seconds, 3),
                        "timeout_s": UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS,
                        "reason": pause_reason,
                    },
                )
                emit_operator_alert(
                    RECONCILE_ALERT_RESOLVER_TIMEOUT,
                    {
                        "cl_ord_id": cl_ord_id,
                        "symbol": symbol,
                        "state": cur_state,
                        "age_s": round(age_seconds, 3),
                        "timeout_s": UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS,
                    },
                    severity="error",
                )
            continue

        try:
            outcome = reconcile_order_with_backoff(order_executor, lookup)
        except Exception as exc:  # pragma: no cover - defensive
            summary["errors"].append(f"reconcile[{cl_ord_id}]: {exc}")
            emit_operator_alert(
                "UNKNOWN_RESOLVER_ERROR",
                {"cl_ord_id": cl_ord_id, "symbol": symbol, "error": str(exc)},
                severity="error",
            )
            continue

        next_state = outcome.get("state")
        if next_state in ORDER_TERMINAL_STATES and order_state_can_transition(cur_state, next_state):
            try:
                record_order_state(
                    cl_ord_id,
                    next_state,
                    intent=intent,
                    detail={
                        "unknown_resolver": True,
                        "reason": outcome.get("reason"),
                        "source": outcome.get("source"),
                        "attempts": outcome.get("attempts"),
                        "elapsed_s": outcome.get("elapsed_s"),
                    },
                    prev_state=cur_state,
                )
                summary["resolved"] += 1
                continue
            except (ValueError, OSError) as exc:
                summary["errors"].append(f"record_order_state[{cl_ord_id}]: {exc}")

        # Record the SUBMITTED->UNKNOWN transition ONCE. An already-UNKNOWN order that
        # re-resolves to UNKNOWN is NOT re-recorded: a no-op state change would only bloat
        # the journal, and the backstop measures age from origin_ts (not the latest record)
        # so re-recording would buy nothing.
        if (
            next_state == ORDER_STATE_UNKNOWN
            and cur_state != ORDER_STATE_UNKNOWN
            and order_state_can_transition(cur_state, ORDER_STATE_UNKNOWN)
        ):
            try:
                record_order_state(
                    cl_ord_id,
                    ORDER_STATE_UNKNOWN,
                    intent=intent,
                    detail={
                        "unknown_resolver": True,
                        "reason": outcome.get("reason"),
                        "source": outcome.get("source"),
                        "attempts": outcome.get("attempts"),
                        "elapsed_s": outcome.get("elapsed_s"),
                    },
                    prev_state=cur_state,
                )
                cur_state = ORDER_STATE_UNKNOWN
            except (ValueError, OSError) as exc:
                summary["errors"].append(f"record_unknown[{cl_ord_id}]: {exc}")

        emit_reconcile_alert(
            RECONCILE_ALERT_MISMATCH,
            {
                "stage": "unknown_resolver_pending",
                "cl_ord_id": cl_ord_id,
                "symbol": symbol,
                "journal_state": cur_state,
                "reconciled_state": next_state,
                "reason": outcome.get("reason"),
                "attempts": outcome.get("attempts"),
            },
        )
        summary["pending"] += 1
    return summary


def unknown_resolver_loop(stop_event: "threading.Event | None" = None, sleep=time.sleep) -> None:
    # INTERVAL_SECONDS <= 0 disables the resolver. Short-circuit BEFORE the max(1.0, ...)
    # floor below so 0 means "off", not "poll every 1s".
    if UNKNOWN_RESOLVER_INTERVAL_SECONDS <= 0:
        return
    interval = max(1.0, UNKNOWN_RESOLVER_INTERVAL_SECONDS)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        _set_resolver_heartbeat()
        try:
            summary = resolve_unknown_orders_once()
            if summary["checked"] or summary["expired"] or summary["errors"]:
                logging.info(
                    "UNKNOWN resolver tick checked=%d resolved=%d pending=%d expired=%d never_submitted=%d errors=%d",
                    summary["checked"],
                    summary["resolved"],
                    summary["pending"],
                    summary["expired"],
                    summary.get("never_submitted", 0),
                    len(summary["errors"]),
                )
        except Exception as exc:  # pragma: no cover - defensive
            emit_operator_alert("UNKNOWN_RESOLVER_ERROR", {"error": str(exc)}, severity="error")

        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            sleep(interval)


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
    """Per-(symbol, strategy) close id at 1-second granularity. Idempotent within the
    same UTC second: an accidental resubmit collides on the order-journal dedupe key
    and is refused ``duplicate_cl_ord_id``, but two DISTINCT closes for the same
    symbol/strategy later the same day no longer collide (a full-day id silently
    dropped the second one). The trailing ``_{YYYYMMDD}_{HHMMSS}`` keeps the 8-digit
    UTC-day token intact for the fallback attribution parser
    (``pnl_ledger._parse_operator_close_strategy_id``)."""
    now = datetime.now(timezone.utc)
    return f"operator_close_{symbol}_{strategy_id}_{now.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}"


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
        record_pipeline_event("execution", _signal_id_of(record), {"received_at": record.get("received_at"), "okx_execution": result})
        return result
    return _execute_via_service(record)


# --- Phase 8: optional pre-execution advisor -------------------------------
# The advisor is a SAFETY OVERSEER, never a trader. It sees the (already fully
# determined) trade intent and may only return action="proceed" or "skip", plus
# a free-text risk_note and an optional 0-100 score. It can NEVER change symbol,
# side, size, leverage, or strategy -- those are locked in code upstream. When
# enabled, a "skip" is a veto and blocks the trade.
# Any timeout / transport error / malformed reply FAILS OPEN to deterministic
# execution: the deterministic front door is never down because of the LLM.
#
# Transport: the Hermes Agent run as a one-shot with our skills loaded
# (`hermes -z "<prompt>" --skills hermx-control`). This runs the full agent loop
# through Hermes (its configured provider + credentials) so the agent can use the
# hermx-control skill to read the live local API before deciding -- it is NOT a
# bare LLM passthrough.
ADVISOR_SYSTEM_PROMPT = (
    "You are HermX's pre-execution risk overseer. You are given a trading signal "
    "whose symbol, side, size, leverage and strategy are ALREADY FIXED by code and "
    "a sanctioned strategy file. You cannot change any of them. Your ONLY job is to "
    "decide whether this already-sanctioned trade should still be allowed to "
    "execute right now, or skipped on risk grounds. "
    "Respond with STRICT JSON only, no prose, no code fences, exactly: "
    '{"action": "proceed" | "skip", "risk_note": "<short reason>", "score": <0-100 risk score>}. '
    "Default to \"proceed\" unless you see a concrete, specific risk. Never invent "
    "sizes or prices."
)


def _advisor_state_snapshot(record: dict) -> dict:
    """Minimal, read-only context the advisor reasons over. Intentionally small:
    the trade intent + the sanctioned strategy params + planned notional. Sizing
    is shown for context ONLY; the advisor cannot alter it."""
    normalized = record.get("normalized") or {}
    strategy = record.get("strategy_config") or {}
    readiness = record.get("execution_readiness") or {}
    intent = readiness.get("execution_intent") or {}
    return {
        "symbol": normalized.get("symbol"),
        "side": normalized.get("side"),
        "timeframe": normalized.get("timeframe"),
        "signal_price": normalized.get("tv_signal_price"),
        "strategy_id": normalized.get("strategy_id"),
        "budget_usd": strategy_budget_usd(strategy),
        "leverage": strategy.get("leverage"),
        "planned_notional_usd": intent.get("planned_notional_usd") or readiness.get("planned_notional_usd"),
        "live_execution_enabled": readiness.get("live_execution_enabled"),
    }


def _advisor_build_prompt(record: dict) -> str:
    snapshot = _advisor_state_snapshot(record)
    return (
        ADVISOR_SYSTEM_PROMPT
        + "\n\nYou may use the hermx-control skill to read current positions, PnL and "
        "arm state from the local API before deciding.\n"
        "Trade intent (FIXED, do not change):\n"
        + json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        + "\n\nOutput ONLY the strict JSON object."
    )


def _advisor_agent_query(prompt: str) -> str:
    """Transport seam (monkeypatched in tests). Runs the Hermes Agent as a one-shot
    with our skills loaded and returns its stdout (ONLY the agent's response).
    Raises on a missing binary / non-zero exit / timeout so the caller fails open.
    This goes THROUGH Hermes (its configured provider + skills), not a bare LLM."""
    cmd = [HERMX_ADVISOR_COMMAND, "-z", prompt, "--skills", HERMX_ADVISOR_SKILLS]
    if HERMX_ADVISOR_MODEL:
        cmd += ["-m", HERMX_ADVISOR_MODEL]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=HERMX_ADVISOR_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"hermes one-shot exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def _advisor_parse(content: str) -> dict:
    """Tolerant strict-JSON parse of the advisor reply. Accepts a bare JSON object
    or one embedded in surrounding text/code fences. Raises if no valid object or
    if ``action`` is not one of proceed/skip."""
    text = (content or "").strip()
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("advisor reply is not a JSON object")
    action = str(obj.get("action") or "").strip().lower()
    if action not in {"proceed", "skip"}:
        raise ValueError(f"advisor action invalid: {action!r}")
    score = obj.get("score")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "action": action,
        "risk_note": str(obj.get("risk_note") or "")[:500],
        "score": score,
    }


def run_execution_advisor(record: dict) -> "dict | None":
    """Consult the pre-execution advisor. Returns None when disabled (caller then
    behaves byte-identically to before). Otherwise returns a decision dict that
    ALWAYS includes ``veto_applied`` (bool). FAILS OPEN (veto_applied=False) on any
    error so a down/slow/garbage LLM can never block a sanctioned trade."""
    if not HERMX_ADVISOR_ENABLED:
        return None
    started = time.monotonic()
    decision = {
        "enabled": True,
        "ok": False,
        "action": "proceed",
        "risk_note": "",
        "score": None,
        "veto_applied": False,
        "model": HERMX_ADVISOR_MODEL or "(hermes default)",
        "skills": HERMX_ADVISOR_SKILLS,
    }
    try:
        parsed = _advisor_parse(_advisor_agent_query(_advisor_build_prompt(record)))
        decision.update(ok=True, action=parsed["action"], risk_note=parsed["risk_note"], score=parsed["score"])
        decision["veto_applied"] = bool(parsed["action"] == "skip")
    except Exception as exc:  # fail OPEN -> proceed deterministically
        decision["error"] = str(exc)[:300]
        logging.warning("execution advisor failed open (proceeding): %s", exc)
    decision["latency_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    try:
        record_pipeline_event("advisor", _signal_id_of(record), {"received_at": record.get("received_at"), "advisor": decision, "snapshot": _advisor_state_snapshot(record)})
    except Exception as exc:  # advisory logging must never block execution
        logging.warning("advisor ledger append failed: %s", exc)
    return decision


def execute_with_advisor(record: dict) -> dict:
    """Single wrapper used by the execution paths: consult the advisor, honor a
    veto if granted, otherwise delegate to the authoritative submission path. With
    the advisor disabled (default) this is exactly ``execute_if_enabled``."""
    decision = run_execution_advisor(record)
    if decision is not None:
        record["advisor"] = decision
        if decision.get("veto_applied"):
            result = {
                "ok": True,
                "mode": "not_submitted",
                "reason": "vetoed_by_advisor",
                "advisor": {"risk_note": decision.get("risk_note"), "score": decision.get("score")},
            }
            record_pipeline_event("execution", _signal_id_of(record), {"received_at": record.get("received_at"), "okx_execution": result})
            return result
    return execute_if_enabled(record)


def _build_close_record(payload: dict, normalized: dict, received_at_override: str | None = None) -> tuple[int, dict]:
    """Intake path for a webhook-driven close (action=close). Runs the SAME
    source / schema / strategy-match / dedupe gates as an open signal, then routes
    through the operator-close executor (close_only=True). A close carries no side,
    so it deliberately bypasses the ALLOWED_SIDES gate that guards open signals."""
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

    # PR2 conflict gate: reject when BOTH an explicit `action` and an explicit
    # `side` are present as *opposing* open sides (e.g. action=buy, side=sell).
    # A matching pair (buy/buy) or a lone field is fine and falls through.
    raw_action_in = str(first(payload, "action", default="") or "").lower().strip()
    raw_side_in = str(first(payload, "side", default="") or "").lower().strip()
    if (raw_action_in in {"buy", "sell"} and raw_side_in in {"buy", "sell"}
            and raw_action_in != raw_side_in):
        return 400, {
            "ok": False,
            "error": "action_side_conflict",
            "mode": "action_side_conflict",
            "reason": f"action={raw_action_in!r} conflicts with side={raw_side_in!r}",
            "normalized": normalized,
        }

    # PR2 close branch: action=close reduces risk, so it reuses the operator-close
    # path (close_only=True → bypasses the kill switch + symbol pause). It carries
    # no side, so it must return BEFORE the ALLOWED_SIDES gate below.
    if normalized.get("action") == "close":
        return _build_close_record(payload, normalized, received_at_override)

    # `.get`: a close has already returned above; a malformed open alert (invalid
    # side, no valid action) has its `side` key dropped by normalize → None → 400.
    if normalized.get("side") not in ALLOWED_SIDES:
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
        direction = "long" if normalized.get("side") == "buy" else "short"
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
                "Shadow async processed symbol=%s side=%s tv_time=%s status=%s",
                normalized.get("symbol"),
                normalized.get("side"),
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
        else:
            self._send(404, {"ok": False, "error": "not_found"})

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
        auth_ok, auth_status, auth_error = authenticate_webhook_request(self, raw_body)
        if not auth_ok:
            self._send(auth_status, {"ok": False, "error": auth_error})
            return
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
