#!/usr/bin/env python3
"""Parallel MXC shadow webhook receiver.

Safe by design: receives TradingView alerts, answers quickly, enriches with
available MXC context, writes append-only ledgers, and only calls OKX when the
active config explicitly enables sandbox/demo execution.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
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
from strategy.decision_math import (  # noqa: E402  pure decision math (re-exported)
    POLICY_LABELS,
    as_float,
    regime_from_acc,
    phase_from_acc_vel,
    pulse_label,
    risk_on,
    no_pulse_score,
    extract_jrsx,
    rsi_caution,
    base_context,
    policy_result,
    decide_duo_raw,
    direction_confirms,
    decide_v3_r75,
    step_weight,
    convert_weight_policy,
    mtf_summary,
    context_opposes,
    single_tf_summary,
    decide_v52_fast_1h,
    decide_v6_regime_duo,
    regime_rsi_points,
    rsi_quality_points,
    decide_duo_regime_rsi_sized,
    decide_duo_regime_rsi_30m,
    metric_align_points,
    both_acc_vel_against,
    decide_duo_conviction_sized,
    decide_conviction_v2_candidate,
    fmt_float,
)

PORT = int(os.environ.get("SHADOW_PORT", "8891"))
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
HERMX_REPLAY_WINDOW_SECONDS = float(os.environ.get("HERMX_REPLAY_WINDOW_SECONDS", "300") or "300")
HERMX_MAX_BODY_BYTES = int(os.environ.get("HERMX_MAX_BODY_BYTES", "262144") or "262144")
HERMX_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("HERMX_RATE_LIMIT_WINDOW_SECONDS", "60") or "60")
HERMX_RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("HERMX_RATE_LIMIT_MAX_REQUESTS", "120") or "120")
HERMX_QUEUE_MAXSIZE = int(os.environ.get("HERMX_QUEUE_MAXSIZE", "200") or "200")
HERMX_SUBMIT_TIMEOUT_SECONDS = float(os.environ.get("HERMX_SUBMIT_TIMEOUT_SECONDS", "45") or "45")
HERMX_WORKER_POOL_SIZE = int(os.environ.get("HERMX_WORKER_POOL_SIZE", "1") or "1")
HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS = float(os.environ.get("HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS", "86400") or "86400")
HERMX_WATCHDOG_ENABLED = (os.environ.get("HERMX_WATCHDOG_ENABLED") or "true").strip().lower() not in {"0", "false", "no", ""}
HERMX_WATCHDOG_STALE_SECONDS = float(os.environ.get("HERMX_WATCHDOG_STALE_SECONDS", "120") or "120")
HERMX_QUEUE_LAG_SLO_SECONDS = float(os.environ.get("HERMX_QUEUE_LAG_SLO_SECONDS", "30") or "30")
ROOT = Path(os.environ.get("SHADOW_ROOT", Path(__file__).resolve().parents[1]))
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
PAPER_STATE_FILE = DATA_DIR / "paper-state.json"
CONTROL_STATE_FILE = DATA_DIR / "control-state.json"
# Phase 1 task 1/2 (REFACTOR_PLAN.md:202-206): durable, append-only position
# journal. Every paper-state transition is journaled (write-ahead, fsync'd) and,
# under the "journal" backend, in-memory state is *derived* by replaying it so a
# missing/corrupt snapshot rebuilds rather than silently resetting to empty (E5).
POSITION_JOURNAL_LEDGER = LOG_DIR / "position-journal.jsonl"
# schema_version on every record. Replay compatibility rule:
#   * A record with schema_version <= POSITION_JOURNAL_SCHEMA_VERSION is applied
#     by apply_effect (older shapes get version-dispatched here as they appear).
#   * A record with schema_version > POSITION_JOURNAL_SCHEMA_VERSION is a hard
#     error (loud log + raise): a newer writer produced a shape this reader does
#     not understand; refuse to misinterpret it. Forward migrations bump this
#     constant and add an explicit upgrade shim before changing the effect shape
#     (e.g. Decimal serialization, idempotency metadata).
POSITION_JOURNAL_SCHEMA_VERSION = 1
# State backend selector (REFACTOR_PLAN.md:225 rollback flag). Read once at import,
# like the other env-driven config. "legacy" (DEFAULT) = paper-state.json snapshot
# is authoritative, byte-identical to pre-Phase-1 behavior. "journal" = the
# position journal is authoritative and load_paper_state() rebuilds via replay.
# The journal is written in BOTH modes (forward-compatible soak data).
HERMX_STATE_BACKEND = (os.environ.get("HERMX_STATE_BACKEND") or "legacy").strip().lower() or "legacy"
# Phase 1 task 7 (REFACTOR_PLAN.md:218-221): journal lifecycle = verified
# checkpoint + segment rotation so journal-mode startup replay stays bounded and
# disk does not grow without limit. The checkpoint stores the full paper-state
# plus the last applied seq + a sha256 of the canonical state; on load it is only
# trusted if that hash recomputes (verify-before-trust) and its versions are not
# from a newer writer, otherwise it is discarded and we fall back to full replay.
POSITION_JOURNAL_CHECKPOINT_FILE = LOG_DIR / "position-journal.checkpoint.json"
POSITION_JOURNAL_CHECKPOINT_VERSION = 1
# Unified operator/reconcile/state alert ledger. Every alert row carries a ``kind``
# field ("operator", "reconcile", or "state") so the dashboard and operators can
# filter; this merges the former operator-alerts.jsonl, reconcile-alerts.jsonl, and
# state-alerts.jsonl. Fail-closed state-write errors (:221 -- a journal/checkpoint
# write that fails, e.g. ENOSPC) surface here as kind="state" AND re-raise so the
# money path is blocked rather than proceeding on lost state.
ALERTS_LEDGER = LOG_DIR / "alerts.jsonl"
# Rotate the live journal segment into a sealed file once it reaches this many
# records, AFTER writing a verified checkpoint that subsumes them. Module constant
# (env-overridable) so a test can force a checkpoint+rotation without writing
# thousands of records; an internal _checkpoint_and_rotate() helper also forces it.
HERMX_JOURNAL_SEGMENT_MAX_RECORDS = int(os.environ.get("HERMX_JOURNAL_SEGMENT_MAX_RECORDS", "1000") or "1000")
# Retention: keep the last K sealed segments for forensic replay. The verified
# checkpoint already subsumes every sealed segment (older sealed files are
# replay-unnecessary), so they are pruned beyond K. Set < 0 to keep all.
HERMX_JOURNAL_SEGMENT_RETENTION = int(os.environ.get("HERMX_JOURNAL_SEGMENT_RETENTION", "5") or "5")
# Size-based rotation for the high-volume consolidated ledgers (pipeline.jsonl,
# raw-webhooks.jsonl). Unlike the position/order journals -- which rotate by record
# count behind a verified checkpoint -- these are append-only forensic logs with no
# checkpoint, so once the live file exceeds HERMX_LEDGER_ROTATE_MAX_BYTES it is sealed
# to ``<name>.<n>.jsonl`` (monotonic n) and a fresh live file is started. The last
# HERMX_LEDGER_ROTATE_RETENTION sealed segments are kept; older ones are pruned
# (set < 0 to keep all). Default 64 MiB keeps the bounded reverse-tail dashboard
# reads cheap while retaining ample history.
HERMX_LEDGER_ROTATE_MAX_BYTES = int(os.environ.get("HERMX_LEDGER_ROTATE_MAX_BYTES", str(64 * 1024 * 1024)) or str(64 * 1024 * 1024))
HERMX_LEDGER_ROTATE_RETENTION = int(os.environ.get("HERMX_LEDGER_ROTATE_RETENTION", "5") or "5")
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
# first-class state that triggers reconciliation, NOT a failure. This journal uses a
# SEPARATE monotonic seq counter from the position journal (the two must not collide).
ORDER_JOURNAL_LEDGER = LOG_DIR / "order-journal.jsonl"
ORDER_JOURNAL_SCHEMA_VERSION = 1
# Order-journal lifecycle (mirrors the position-journal checkpoint+rotation, Phase 1
# task 7). Without it _order_journal_next_seq() and latest_order_record() re-read the
# WHOLE append-only journal on every submit -- O(n) per order, unbounded growth. The
# checkpoint folds the journal into the bounded "latest record per cl_ord_id" index
# (the dedupe/idempotency authority) plus each order's origin ts, so a load rebuilds
# from (checkpoint + live-segment tail) instead of the full history, and rotation seals
# the live segment so disk does not grow without limit. The segment-size / retention
# knobs are SHARED with the position journal (HERMX_JOURNAL_SEGMENT_*). Unlike the
# position journal this is NOT gated by HERMX_STATE_BACKEND -- the order journal is the
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
#                   gated by unknown_resolver_enabled() (HERMX_UNKNOWN_RESOLVER_ENABLED,
#                   default ON); re-reconciles still-open SUBMITTED/UNKNOWN orders.
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
UNKNOWN_RESOLVER_INTERVAL_SECONDS = float(os.environ.get("HERMX_UNKNOWN_RESOLVER_INTERVAL_SECONDS", "30") or "30")
UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS = float(os.environ.get("HERMX_UNKNOWN_RESOLVER_ORDER_TIMEOUT_SECONDS", "900") or "900")
UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK = int(os.environ.get("HERMX_UNKNOWN_RESOLVER_MAX_ORDERS_PER_TICK", "50") or "50")
# PLANNED orphan backstop: a PLANNED order older than this (and unknown to the venue) is
# resolved PLANNED->REJECTED (never_submitted). Shorter than the SUBMITTED/UNKNOWN timeout
# because a PLANNED order was never sent -- there is no in-flight venue state to wait on,
# only the small window of an in-process submit between the PLANNED and SUBMITTED writes.
PLANNED_ORDER_TIMEOUT_SECONDS = float(os.environ.get("HERMX_PLANNED_ORDER_TIMEOUT_SECONDS", "300") or "300")
# Queue saturation signaling threshold for early warning; hard rejection now uses
# PROCESS_QUEUE.maxsize and returns 503 when full.
QUEUE_SATURATION_ALERT_DEPTH = int(os.environ.get("HERMX_QUEUE_SATURATION_ALERT_DEPTH", "100") or "100")
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
# tab-health.jsonl is written by the EXTERNAL mxc-tab-health service, not by this
# process; it is read-only here (latest_tab_health). It is deliberately NOT folded
# into pipeline.jsonl -- doing so would break that out-of-process producer contract.
TAB_HEALTH_LEDGER = LOG_DIR / "tab-health.jsonl"
CONFIG_FILE = ROOT / "shadow-config.json"


def load_shadow_config() -> dict:
    default = {
        "mode": os.environ.get("SHADOW_MODE", "paper_shadow"),
        "primary_policy": os.environ.get("PRIMARY_POLICY", "not_selected"),
        "execution_timeframe": "30m",
        "chart_type": os.environ.get("DEFAULT_CHART_TYPE", "heikin_ashi"),
        "base_notional_usd": float(os.environ.get("PAPER_BASE_NOTIONAL_USD", "10000")),
        "fees": {
            "maker_rate": float(os.environ.get("OKX_PERP_MAKER_FEE_RATE", "0.0002")),
            "taker_rate": float(os.environ.get("OKX_PERP_TAKER_FEE_RATE", "0.0005")),
            "default_liquidity": os.environ.get("PAPER_DEFAULT_LIQUIDITY", "taker").lower(),
        },
        "funding": {
            "enabled": os.environ.get("PAPER_FUNDING_ENABLED", "false").lower() == "true",
            "default_rate": float(os.environ.get("PAPER_DEFAULT_FUNDING_RATE", "0")),
        },
        "policies": {"enabled": ["duo_raw", "duo_regime_rsi_30m"]},
        "execution": {
            "enabled": False,
            "mode": "dry_run",
            # Exchange key understood by ExecutorFactory. Post CCXT cutover "ccxt"
            # is the sole backend; legacy okx_* keys are aliased to it.
            "exchange": "ccxt",
            "ccxt_exchange": "okx",
            "ccxt_default_type": "swap",
            "execution_policy": "duo_raw",
            "shadow_policy": "duo_regime_rsi_30m",
            "route": "okx_api",
            "account": "sandbox",
        },
        # Assets use the canonical generic instrument id key.
        "assets": {
            "XRPUSDT": {"enabled": True, "budget_usd": 1500, "leverage": 2, "inst_id": "XRP-USDT-SWAP", "timeframe": "30m"},
            "SOLUSDT": {"enabled": True, "budget_usd": 1500, "leverage": 2, "inst_id": "SOL-USDT-SWAP", "timeframe": "30m"},
            "ETHUSDT": {"enabled": True, "budget_usd": 2000, "leverage": 2, "inst_id": "ETH-USDT-SWAP", "timeframe": "30m"},
        },
        "risk": {"allow_live_execution": False, "duplicate_protection": True, "max_slippage_pct": 0.25, "max_daily_loss_usd": 150.0},
        # Phase 8: optional pre-execution Hermes/LLM advisor. DEFAULT OFF -> when
        # disabled the strategy/shadow execution path is byte-identical to before.
        # The advisor can only VETO (skip); it can NEVER change symbol,
        # side, size, leverage, or strategy -- those stay locked in code. A single
        # switch grants veto power: when enabled, a "skip" blocks the trade. Any
        # timeout / error / malformed response FAILS OPEN to deterministic
        # execution (front door is never down because of the LLM).
        # The advisor invokes the **Hermes Agent** as a one-shot, loading the skills
        # we built (`skills/hermx-control` -- and any we add later) so the agent can
        # read the live local API (positions / PnL / arm state) before its verdict:
        #   hermes -z "<prompt>" --skills hermx-control [-m model]
        # This runs through Hermes (its configured xai provider + credentials), NOT a
        # bare LLM call. ``skills`` is a comma-separated list that will grow.
        "advisor": {
            "enabled": False,
            "command": "hermes",
            "skills": "hermx-control",
            "model": "",
            "timeout_seconds": 30.0,
        },
    }
    if not CONFIG_FILE.exists():
        return default
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        merged = default | loaded
        merged["fees"] = default["fees"] | loaded.get("fees", {})
        merged["funding"] = default["funding"] | loaded.get("funding", {})
        merged["policies"] = default["policies"] | loaded.get("policies", {})
        merged["execution"] = default["execution"] | loaded.get("execution", {})
        merged["assets"] = default["assets"] | loaded.get("assets", {})
        merged["risk"] = default["risk"] | loaded.get("risk", {})
        merged["advisor"] = default["advisor"] | loaded.get("advisor", {})
        return merged
    except Exception as exc:
        logging.warning("Failed to load shadow config: %s", exc)
        return default


CONFIG = load_shadow_config()
STRATEGY_ENGINE = CONFIG.get("strategy_engine", {}) or {}
STRATEGIES_DIR = ROOT / str(STRATEGY_ENGINE.get("strategies_dir") or "strategies")

# Phase 8 pre-execution advisor (see load_shadow_config "advisor" block). Env vars
# override config so an operator can flip it on a running VPS without editing JSON.
# Everything here is read-only config; the advisor itself never sizes or routes.
_ADVISOR_CFG = CONFIG.get("advisor", {}) or {}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


HERMX_ADVISOR_ENABLED = _env_bool("HERMX_ADVISOR_ENABLED", bool(_ADVISOR_CFG.get("enabled", False)))
HERMX_ADVISOR_COMMAND = (os.environ.get("HERMX_ADVISOR_COMMAND") or str(_ADVISOR_CFG.get("command") or "hermes")).strip()
HERMX_ADVISOR_SKILLS = (os.environ.get("HERMX_ADVISOR_SKILLS") or str(_ADVISOR_CFG.get("skills") or "hermx-control")).strip()
HERMX_ADVISOR_MODEL = (os.environ.get("HERMX_ADVISOR_MODEL") or str(_ADVISOR_CFG.get("model") or "")).strip()
HERMX_ADVISOR_TIMEOUT_SECONDS = float(os.environ.get("HERMX_ADVISOR_TIMEOUT_SECONDS") or _ADVISOR_CFG.get("timeout_seconds") or 30.0)
# advisor-decisions, strategy-alerts, and strategy-alert-quarantine were folded into
# the unified PIPELINE_LEDGER (stages "advisor", "strategy_match", "quarantine").
# Phase 6 / M2 (REFACTOR_PLAN.md): explicit alert-schema enforcement at intake.
# The JSON schema lives in the source repo (NOT under SHADOW_ROOT, which tests
# redirect to a temp dir), so resolve it relative to this file's repo root.
ALERT_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "tradingview-alert.schema.json"
# Observe-only counters for the alert-schema feature. Mutating a dict needs no
# `global` declaration, and adding/incrementing these never alters any ledger
# record or return value, so default-OFF behavior stays byte-identical.
ALERT_SCHEMA_METRICS = {"invalid": 0, "quarantined": 0}
POLICY_KEYS = tuple(CONFIG.get("policies", {}).get("enabled") or ["duo_raw", "duo_regime_rsi_30m"])
MTF_POLICY_KEYS = {"v52_fast_1h", "v6_regime_duo", "duo_conviction_sized", "conviction_v2_candidate", "duo_regime_rsi_sized"}
MTF_REQUIRED = any(key in set(POLICY_KEYS) for key in MTF_POLICY_KEYS)
PRIMARY_POLICY = str(CONFIG.get("primary_policy") or "not_selected")
PRIMARY_POLICY_SELECTED = PRIMARY_POLICY not in ("", "none", "not_selected", "observation_only")
PAPER_BASE_NOTIONAL_USD = float(CONFIG.get("base_notional_usd") or 10000)
OKX_PERP_MAKER_FEE_RATE = float(CONFIG.get("fees", {}).get("maker_rate", 0.0002))
OKX_PERP_TAKER_FEE_RATE = float(CONFIG.get("fees", {}).get("taker_rate", 0.0005))
PAPER_DEFAULT_LIQUIDITY = str(CONFIG.get("fees", {}).get("default_liquidity", "taker")).lower()
DEFAULT_CHART_TYPE = str(CONFIG.get("chart_type") or "heikin_ashi")
PAPER_FUNDING_ENABLED = bool(CONFIG.get("funding", {}).get("enabled", False))
PAPER_DEFAULT_FUNDING_RATE = float(CONFIG.get("funding", {}).get("default_rate", 0.0))


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
try:
    from cdp_indicator_reader import read_indicator_values
except Exception as exc:  # fail closed
    read_indicator_values = None
    logging.warning("CDP reader unavailable: %s", exc)

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

ALLOWED_SYMBOLS = {symbol for symbol, cfg in CONFIG.get("assets", {}).items() if cfg.get("enabled", True)}
ALLOWED_SIDES = {"buy", "sell"}
MTF_TIMEFRAMES = ("1h",)
ACTIVE_MTF_TIMEFRAMES = MTF_TIMEFRAMES if MTF_REQUIRED else ()
CORE_MXC_FIELDS = ("pp_acc", "pp_vel")
LIVE_READ_ATTEMPTS = int(os.environ.get("MXC_LIVE_READ_ATTEMPTS", "1"))
LIVE_READ_SLEEP_SECONDS = float(os.environ.get("MXC_LIVE_READ_SLEEP_SECONDS", "1.5"))
HEALTH_CACHE_MAX_AGE_SECONDS = float(os.environ.get("MXC_HEALTH_CACHE_MAX_AGE_SECONDS", "420"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_tv_time(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit():
            raw = int(text)
            if raw > 10_000_000_000:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


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


def _advance_symbol_ticket_turn(symbol: str, ticket: int) -> None:
    with _SYMBOL_TICKET_TURN:
        current = int(_SYMBOL_TICKET_RUN.get(symbol) or 0)
        if current <= int(ticket):
            _SYMBOL_TICKET_RUN[symbol] = int(ticket) + 1
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
    if not HERMX_WATCHDOG_ENABLED:
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
    return _rate_limit_key_impl(handler)


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


def latency_info(tv_time, received_at: str) -> dict:
    received_dt = parse_tv_time(received_at) or datetime.now(timezone.utc)
    tv_dt = parse_tv_time(tv_time)
    if not tv_dt:
        return {"tv_time_parse_ok": False, "latency_seconds": None, "latency_minutes": None}
    seconds = (received_dt - tv_dt).total_seconds()
    return {
        "tv_time_parse_ok": True,
        "latency_seconds": round(seconds, 3),
        "latency_minutes": round(seconds / 60.0, 3),
    }


def dedupe_key(normalized: dict) -> str:
    return "|".join(str(normalized.get(k, "")) for k in ("strategy_id", "symbol", "side", "timeframe", "tv_time"))


def _signal_identity(normalized: dict) -> str:
    return "|".join(
        str(normalized.get(k, ""))
        for k in ("strategy_id", "symbol", "side", "timeframe", "tv_time", "signal_id")
    )


def stable_client_order_id(identity: str, role: str = "base") -> str:
    digest = hashlib.sha256(f"{identity}|{role}".encode("utf-8")).hexdigest()
    return f"mxc{digest}"[:32]


def _dedupe_window_seconds() -> float:
    # BUSINESS idempotency window ONLY. Deliberately INDEPENDENT of the SECURITY replay
    # window (HERMX_REPLAY_WINDOW_SECONDS), which is enforced separately in HMAC
    # verification. Conflating them (the old max(..., replay)) let a long replay window
    # silently widen idempotency retention -- two unrelated concerns. Neither widens the
    # other now: freshness is the HMAC's job, idempotency is this ledger's job.
    return max(1.0, HERMX_SIGNAL_DEDUPE_WINDOW_SECONDS)


def _epoch_from_iso(ts: str | None) -> float | None:
    dt = parse_tv_time(ts)
    if dt is None:
        return None
    return dt.timestamp()


def _load_signal_dedupe_index(now_seconds: float | None = None) -> None:
    if _SIGNAL_DEDUPE_INDEX.get("loaded"):
        return
    now = time.time() if now_seconds is None else float(now_seconds)
    cutoff = now - _dedupe_window_seconds()
    signals: dict[str, dict] = {}
    keys: dict[str, dict] = {}
    for rec in read_jsonl_tolerant(SIGNALS_LEDGER):
        if not isinstance(rec, dict):
            continue
        first_seen_at = str(rec.get("first_seen_at") or rec.get("ts") or "")
        first_seen_epoch = rec.get("first_seen_epoch")
        if not isinstance(first_seen_epoch, (int, float)):
            first_seen_epoch = _epoch_from_iso(first_seen_at)
        if first_seen_epoch is None or first_seen_epoch < cutoff:
            continue
        entry = {
            "first_seen_at": first_seen_at,
            "first_seen_epoch": float(first_seen_epoch),
            "signal_id": str(rec.get("signal_id") or ""),
            "dedupe_key": str(rec.get("dedupe_key") or ""),
            "symbol": rec.get("symbol"),
            "side": rec.get("side"),
            "timeframe": rec.get("timeframe"),
            "tv_time": rec.get("tv_time"),
        }
        if entry["signal_id"]:
            signals[entry["signal_id"]] = entry
        if entry["dedupe_key"]:
            keys[entry["dedupe_key"]] = entry
    _SIGNAL_DEDUPE_INDEX["signals"] = signals
    _SIGNAL_DEDUPE_INDEX["keys"] = keys
    _SIGNAL_DEDUPE_INDEX["loaded"] = True


def check_and_mark_signal(normalized: dict, received_at: str) -> tuple[bool, dict]:
    sid = str(normalized.get("signal_id") or "")
    key = dedupe_key(normalized)
    received_epoch = _epoch_from_iso(received_at) or time.time()
    cutoff = received_epoch - _dedupe_window_seconds()
    with _SIGNAL_DEDUPE_LOCK:
        _load_signal_dedupe_index(now_seconds=received_epoch)
        signals = _SIGNAL_DEDUPE_INDEX.setdefault("signals", {})
        keys = _SIGNAL_DEDUPE_INDEX.setdefault("keys", {})
        for bucket in (signals, keys):
            stale_keys = [k for k, v in bucket.items() if float(v.get("first_seen_epoch") or 0.0) < cutoff]
            for stale in stale_keys:
                bucket.pop(stale, None)

        existing = None
        duplicate_by = None
        if sid and sid in signals:
            existing = signals[sid]
            duplicate_by = "signal_id"
        elif key in keys:
            existing = keys[key]
            duplicate_by = "symbol_side_timeframe_tv_time"

        meta = {
            "signal_id": sid,
            "dedupe_key": key,
            "duplicate_by": duplicate_by,
            "first_seen_at": (existing or {}).get("first_seen_at"),
            "window_seconds": _dedupe_window_seconds(),
        }
        if existing:
            return True, meta

        entry = {
            "first_seen_at": received_at,
            "first_seen_epoch": received_epoch,
            "signal_id": sid,
            "dedupe_key": key,
            "symbol": normalized.get("symbol"),
            "side": normalized.get("side"),
            "timeframe": normalized.get("timeframe"),
            "tv_time": normalized.get("tv_time"),
        }
        if sid:
            signals[sid] = entry
        keys[key] = entry
        append_jsonl(
            SIGNALS_LEDGER,
            {
                "ts": received_at,
                "kind": "signal_dedupe",
                **entry,
            },
        )
        meta["first_seen_at"] = received_at
        return False, meta


def append_jsonl(path: Path, obj: dict) -> None:
    """Append one JSONL record atomically + durably (whole-line write + fsync).

    Phase 1 task 2 remainder (REFACTOR_PLAN.md:206): all append_jsonl callers inherit
    durable writes. The whole encoded line (incl. trailing newline) is written via a
    short-write loop on a single unbuffered fd, so the OS can never wedge a HALF-written
    record in front of later appends -- a money-path PLANNED record is all-or-nothing.
    A crash mid-write can only ever leave a clean TRAILING tear, which read_jsonl_tolerant
    quarantines without bricking the ledger."""
    line = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    # buffering=0 => binary, unbuffered: f.write is a direct os.write we can complete.
    with path.open("ab", buffering=0) as f:
        fd = f.fileno()
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - defensive against a stuck fd
                raise OSError(f"append_jsonl: zero-length write to {path}")
            view = view[written:]
        os.fsync(fd)


def append_jsonl_durable(path: Path, obj: dict) -> None:
    """Compatibility alias for durable JSONL appends."""
    append_jsonl(path, obj)


# --- Consolidated-ledger writers + size rotation ------------------------------
# Valid ``stage`` values for record_pipeline_event(). Every signal-processing event
# is one row in pipeline.jsonl tagged with one of these stages; the dashboard filters
# by stage. ("tab_health" is reserved -- tab-health.jsonl is produced out-of-process
# and is NOT written here; see TAB_HEALTH_LEDGER. "intake" mirrors the raw-webhook
# phase for callers that want a pipeline-side marker.)
PIPELINE_STAGES = frozenset({
    "intake", "dedup_reject", "strategy_match", "quarantine", "decision",
    "advisor", "paper_trade", "execution", "error", "tab_health",
})
# Valid ``phase`` values for record_raw_webhook() (raw-webhooks.jsonl).
RAW_WEBHOOK_PHASES = frozenset({"intake", "webhook"})


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


def read_jsonl_tolerant(path: Path) -> list[dict]:
    """Parse a JSONL file, tolerating a truncated/partial *trailing* line
    (REFACTOR_PLAN.md:206, :234). A crash mid-append can leave a half-written
    final line; that line is dropped (and copied to ``<path>.corrupt`` for
    forensics) and reading continues — never raises. An invalid line that is NOT
    the last non-empty line is genuine mid-file corruption: log loudly and raise,
    because silently skipping it would fabricate state."""
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
    out: list[dict] = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            if i == last_idx:
                try:
                    (path.parent / (path.name + ".corrupt")).write_text(ln, encoding="utf-8")
                except Exception:
                    pass
                logging.warning("read_jsonl_tolerant: quarantined truncated trailing line in %s", path)
                break
            logging.error("read_jsonl_tolerant: corrupt non-trailing line %d in %s (mid-file corruption)", i, path)
            raise
    return out


def startup_quarantine_partial_ledgers(paths: "list[Path] | tuple[Path, ...] | None" = None) -> dict:
    """Startup sweep for trailing partial JSONL lines (Task 2 remainder / :206).

    Uses read_jsonl_tolerant() across runtime ledgers so crash-truncated tails are
    quarantined into ``*.corrupt`` sidecars instead of blowing up readers.
    """
    scan_paths = list(paths) if paths is not None else [
        RAW_WEBHOOK_LEDGER,
        PIPELINE_LEDGER,
        POSITION_JOURNAL_LEDGER,
        ORDER_JOURNAL_LEDGER,
        ALERTS_LEDGER,
        SIGNALS_LEDGER,
        TAB_HEALTH_LEDGER,
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


# Monotonic journal sequence: derived once from the journal's last seq at first
# use (REFACTOR_PLAN.md:202 "seq derived from current journal length/last seq at
# load"), then incremented in-process. Reset to None on module (re)load. Matches
# the existing single-threaded request handling; no cross-process locking.
_journal_seq_cache: "int | None" = None


def _journal_next_seq() -> int:
    global _journal_seq_cache
    if _journal_seq_cache is None:
        # After a rotation the live segment is fresh/empty, so its records alone no
        # longer reveal the true high-water mark. Derive the floor from the
        # checkpoint's last_seq and the sealed segment seqs (encoded cheaply in the
        # filenames, no file read) PLUS any live records, so seq stays monotonic
        # across rotation and restart (REFACTOR_PLAN.md:220 task 7).
        last = -1
        cl = _checkpoint_last_seq_floor()
        if cl is not None and cl > last:
            last = cl
        for seq, _path in _sealed_segment_paths():
            if seq > last:
                last = seq
        for rec in read_jsonl_tolerant(POSITION_JOURNAL_LEDGER):
            s = rec.get("seq")
            if isinstance(s, int) and s > last:
                last = s
        _journal_seq_cache = last
    _journal_seq_cache += 1
    return _journal_seq_cache


def mxc_core_ok(values: dict | None) -> bool:
    return bool(values and all(values.get(field) is not None for field in CORE_MXC_FIELDS))


def read_indicator_values_stable(symbol: str, timeframe: str) -> tuple[dict | None, str | None]:
    """Read CDP values, retrying when TradingView has not hydrated Data Window yet."""
    if read_indicator_values is None:
        return None, "cdp_reader_unavailable"
    last_values = None
    last_error = None
    attempts = max(1, LIVE_READ_ATTEMPTS)
    for attempt in range(attempts):
        try:
            last_values = read_indicator_values(symbol, timeframe)
            last_error = None
            if mxc_core_ok(last_values):
                if attempt:
                    last_values = dict(last_values)
                    last_values["_live_read_attempts"] = attempt + 1
                return last_values, None
            last_error = f"missing_core_fields:{sorted((last_values or {}).keys())}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < attempts - 1:
            time.sleep(LIVE_READ_SLEEP_SECONDS)
    if last_values is not None:
        last_values = dict(last_values)
        last_values["_live_read_attempts"] = attempts
        last_values["_live_read_warning"] = last_error
    return last_values, last_error


def health_age_seconds(health: dict | None) -> float | None:
    if not health:
        return None
    checked = parse_tv_time(health.get("checked_at"))
    if not checked:
        return None
    return (datetime.now(timezone.utc) - checked).total_seconds()


def cached_health_values(health: dict | None, symbol: str, timeframe: str) -> dict | None:
    age = health_age_seconds(health)
    if age is None or age > HEALTH_CACHE_MAX_AGE_SECONDS:
        return None
    symbol = str(symbol or "").upper()
    for row in (health or {}).get("results") or []:
        if row.get("symbol") == symbol and row.get("timeframe") == timeframe and row.get("ok"):
            fields = dict(row.get("fields") or {})
            if mxc_core_ok(fields):
                fields["_fallback_source"] = "tab_health_cache"
                fields["_fallback_age_seconds"] = round(age, 3)
                return fields
    return None


def trigger_health_repair(reason: str, symbol: str, timeframe: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "start", "--no-block", "mxc-tab-health.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        logging.warning("Triggered immediate tab health repair reason=%s symbol=%s timeframe=%s", reason, symbol, timeframe)
    except Exception as exc:
        logging.warning("Failed to trigger tab health repair reason=%s symbol=%s timeframe=%s error=%s", reason, symbol, timeframe, exc)


def first(payload: dict, *names: str, default=""):
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return default


def normalize(payload: dict) -> dict:
    strategy_id = str(first(payload, "strategy_id", "strategyId", default="")).strip()
    strategy_name = str(first(payload, "strategy_name", "strategyName", default="")).strip()
    indicator = str(first(payload, "indicator", "indicator_name", "indicatorName", default="")).strip()
    symbol = str(first(payload, "symbol", "ticker", default="")).upper()
    symbol = symbol.replace("OKX:", "").replace("/", "").replace("-", "")
    side = str(first(payload, "side", "action", default="")).lower()
    timeframe = canonical_timeframe(first(payload, "timeframe", "interval", default="30m"))
    tv_time = str(first(payload, "tv_time", "time", "timestamp", "bar_time", "candle_time", default=now_iso()))
    signal_id = str(first(payload, "signal_id", default=""))
    if not signal_id:
        raw = f"{strategy_id}|{symbol}|{side}|{timeframe}|{tv_time}"
        signal_id = hashlib.sha256(raw.encode()).hexdigest()
    return {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "indicator": indicator,
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
        "tv_signal_price": as_float(first(payload, "tv_signal_price", "tv_close", "signal_price", "price", "close", default=None)),
        "chart_type": str(first(payload, "chart_type", default=DEFAULT_CHART_TYPE)).lower(),
        "okx_mark_price": as_float(first(payload, "okx_mark_price", "mark_price", default=None)),
        "okx_last_price": as_float(first(payload, "okx_last_price", "last_price", default=None)),
        "tv_time": tv_time,
        "exchange": str(first(payload, "exchange", default="okx")).lower(),
        "strategy": str(first(payload, "strategy", default="")),
        "source": str(first(payload, "source", default="tradingview")),
        "signal_id": signal_id,
    }


def validate_strategy_alert(normalized: dict) -> tuple[bool, dict | None, str | None]:
    strategy_id = str(normalized.get("strategy_id") or "").strip()
    if not strategy_id:
        if bool(STRATEGY_ENGINE.get("require_strategy_id", False)):
            return False, None, "missing_strategy_id_required"
        indicator = str(normalized.get("indicator") or "").lower()
        strategy_name = str(normalized.get("strategy_name") or normalized.get("strategy") or "").lower()
        if "duo-base" in indicator or "duo base" in indicator or "duo-base" in strategy_name or "duo base" in strategy_name:
            return False, None, "missing_strategy_id"
        return True, None, None
    if not bool(STRATEGY_ENGINE.get("allow_strategy_alerts", True)):
        return False, None, "strategy_alerts_disabled"
    strategy = STRATEGIES.get(strategy_id)
    if not strategy:
        return False, None, "unknown_strategy_id"
    if str(strategy.get("asset") or "").upper() != str(normalized.get("symbol") or "").upper():
        return False, strategy, "strategy_symbol_mismatch"
    if canonical_timeframe(strategy.get("timeframe")) != canonical_timeframe(normalized.get("timeframe")):
        return False, strategy, "strategy_timeframe_mismatch"
    # A strategy is active when it is permitted to submit orders (submit_orders=true).
    # The legacy `status` gate is gone; `submit_orders=false` simply makes a matched
    # strategy inert (dry-run) rather than quarantining the alert.
    return True, strategy, None


# --------------------------------------------------------------------------- #
# Phase 6 / M2: explicit alert-schema validation at intake.                    #
#                                                                              #
# The schema is validated against the *normalized* alert (canonical snake_case #
# keys, lowercased exchange/source, uppercased symbol) so a raw payload's      #
# casing/aliasing (strategyId/ticker/action) never causes false rejections.    #
# jsonschema is loaded lazily and cached; if it (or the schema file) is        #
# unavailable we fail OPEN -- we never quarantine traffic we cannot evaluate.  #
# Enforcement is gated by strategy_engine.enforce_alert_schema (default OFF).  #
# --------------------------------------------------------------------------- #

_ALERT_SCHEMA_VALIDATOR = None
_ALERT_SCHEMA_LOAD_FAILED = False


def _alert_schema_validator():
    """Lazily build and cache the Draft 2020-12 validator for the alert schema.

    Returns None (and disables enforcement) if jsonschema or the schema file is
    unavailable -- import stays side-effect-free and enforcement fails open.
    """
    global _ALERT_SCHEMA_VALIDATOR, _ALERT_SCHEMA_LOAD_FAILED
    if _ALERT_SCHEMA_VALIDATOR is not None:
        return _ALERT_SCHEMA_VALIDATOR
    if _ALERT_SCHEMA_LOAD_FAILED:
        return None
    try:
        import jsonschema  # lazy: keep module import dependency-light

        schema = json.loads(ALERT_SCHEMA_PATH.read_text(encoding="utf-8"))
        _ALERT_SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(schema)
        return _ALERT_SCHEMA_VALIDATOR
    except Exception as exc:  # fail open: cannot enforce what we cannot load
        logging.warning("alert schema unavailable; schema enforcement disabled: %s", exc)
        _ALERT_SCHEMA_LOAD_FAILED = True
        return None


# Set once we have warned that enforcement is armed but unenforceable -- the alert is a
# config-level condition, so a single operator alert per process suffices (no per-webhook
# spam). Reset by tests via monkeypatch.
_ALERT_SCHEMA_UNENFORCEABLE_ALERTED = False


def _alert_schema_enforcement_status() -> tuple[bool, bool]:
    """Return (armed, enforceable) for alert-schema enforcement.

    ``armed``       = strategy_engine.enforce_alert_schema is true.
    ``enforceable`` = an alert-schema validator is actually available.

    Armed-but-not-enforceable is a fail-open-WHILE-ARMED safety hole: the operator
    believes intake is guarded but validation silently passes everything. Emit a deduped
    error-severity operator alert the first time we observe it."""
    global _ALERT_SCHEMA_UNENFORCEABLE_ALERTED
    armed = bool(STRATEGY_ENGINE.get("enforce_alert_schema", False))
    enforceable = _alert_schema_validator() is not None
    if armed and not enforceable and not _ALERT_SCHEMA_UNENFORCEABLE_ALERTED:
        _ALERT_SCHEMA_UNENFORCEABLE_ALERTED = True
        logging.error(
            "enforce_alert_schema is ARMED but the alert-schema validator is UNAVAILABLE; "
            "intake schema validation is failing OPEN."
        )
        emit_operator_alert(
            "ALERT_SCHEMA_ENFORCEMENT_UNAVAILABLE",
            {"detail": "enforce_alert_schema=true but the alert-schema validator is unavailable; "
                       "alert validation is failing OPEN (every alert passes unchecked)."},
            severity="error",
        )
    return armed, enforceable


def validate_alert_schema(normalized: dict) -> tuple[bool, str | None]:
    """Validate a normalized alert against the TradingView alert JSON schema.

    Returns (True, None) when valid or when the schema/jsonschema is unavailable
    (fail open). On failure returns (False, "<path>: <message>") for the first
    error in deterministic path order.
    """
    validator = _alert_schema_validator()
    if validator is None:
        return True, None
    errors = sorted(validator.iter_errors(normalized), key=lambda e: list(e.path))
    if not errors:
        return True, None
    first = errors[0]
    loc = "/".join(str(p) for p in first.path) or "(root)"
    return False, f"{loc}: {first.message}"


def read_mtf_values(symbol: str) -> tuple[dict, dict]:
    values_by_tf = {}
    errors = {}
    if not ACTIVE_MTF_TIMEFRAMES:
        return values_by_tf, errors
    if read_indicator_values is None:
        return values_by_tf, {tf: "cdp_reader_unavailable" for tf in ACTIVE_MTF_TIMEFRAMES}
    for tf in ACTIVE_MTF_TIMEFRAMES:
        values, error = read_indicator_values_stable(symbol, tf)
        values_by_tf[tf] = values or {}
        if error and not mxc_core_ok(values):
            errors[tf] = error
    return values_by_tf, errors


def mtf_contexts(normalized: dict, mtf_values: dict | None) -> dict:
    out = {}
    for tf in ACTIVE_MTF_TIMEFRAMES:
        out[tf] = base_context(normalized, (mtf_values or {}).get(tf))
    return out


def latest_tab_health() -> dict | None:
    if not TAB_HEALTH_LEDGER.exists():
        return None
    try:
        lines = [line for line in TAB_HEALTH_LEDGER.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def health_gate_for(symbol: str, health: dict | None, mxc_values: dict | None, mtf_values: dict | None) -> dict:
    symbol = str(symbol or "").upper()
    result = {
        "status": "unknown",
        "primary_30m_ok": bool(mxc_values and mxc_values.get("pp_acc") is not None and mxc_values.get("pp_vel") is not None),
        "mtf_ok": False,
        "mtf_available_count": 0,
        "failed_timeframes": [],
        "source": "live_read",
        "message": "Health derived from current read",
    }
    mtf_values = mtf_values or {}
    for tf in ACTIVE_MTF_TIMEFRAMES:
        vals = mtf_values.get(tf) or {}
        ok = vals.get("pp_acc") is not None and vals.get("pp_vel") is not None
        if ok:
            result["mtf_available_count"] += 1
        else:
            result["failed_timeframes"].append(tf)
    result["mtf_ok"] = result["mtf_available_count"] == len(ACTIVE_MTF_TIMEFRAMES)
    if health:
        summary = (health.get("summary") or {}).get(symbol)
        if summary:
            result["source"] = "tab_health_and_live_read"
            # Never allow stale health to override a failed current 30m read.
            result["primary_30m_ok"] = result["primary_30m_ok"] and bool(summary.get("primary_30m_ok", True))
            summary_failed = {tf for tf in (summary.get("failed_timeframes") or []) if tf in ACTIVE_MTF_TIMEFRAMES}
            result["failed_timeframes"] = sorted(set(result["failed_timeframes"]) | summary_failed)
            result["mtf_ok"] = result["mtf_ok"] and not summary_failed
            if summary_failed:
                result["mtf_available_count"] = max(0, len(ACTIVE_MTF_TIMEFRAMES) - len(summary_failed))
    if isinstance(mxc_values, dict) and mxc_values.get("_fallback_source") == "tab_health_cache":
        result["source"] = "tab_health_cache_after_live_retry"
        result["cache_fallback"] = True
        result["cache_age_seconds"] = mxc_values.get("_fallback_age_seconds")
    if not result["primary_30m_ok"]:
        result["status"] = "primary_30m_blocked"
        result["message"] = "30m MXC is unavailable; all policies blocked for this alert"
    elif not result["mtf_ok"]:
        result["status"] = "mtf_degraded"
        result["message"] = "30m MXC is available but higher timeframe context is partial"
    else:
        result["status"] = "healthy"
        if result.get("cache_fallback"):
            result["message"] = "30m live read was incomplete; used recent tab-health snapshot"
        elif ACTIVE_MTF_TIMEFRAMES:
            result["message"] = "30m and MTF MXC context available"
        else:
            result["message"] = "30m MXC context available; MTF disabled by selected policies"
    return result


def apply_health_gate(policies: dict, gate: dict) -> dict:
    status = (gate or {}).get("status")
    if status == "primary_30m_blocked":
        for key, policy in policies.items():
            if key == "duo_raw":
                policy["health_status"] = "signal_only_no_cdp"
                policy.setdefault("reasons", []).insert(0, "Health gate: 30m MXC unavailable, but Duo raw uses the TradingView alert itself")
                continue
            policy["decision"] = "SKIP"
            policy["risk_weight"] = 0
            policy["leverage"] = 0
            policy["action"] = "BLOCKED_BY_HEALTH"
            policy["health_status"] = status
            policy.setdefault("reasons", []).insert(0, "Health gate: 30m Regime unavailable, decision blocked")
        return policies
    if status == "mtf_degraded":
        failed = set((gate or {}).get("failed_timeframes") or [])
        for key in ("v5_mtf", "v51_balanced"):
            policy = policies.get(key)
            if not policy:
                continue
            current = float(policy.get("risk_weight") or 0)
            degraded = step_weight(current, -0.25)
            policy["risk_weight"] = degraded
            if degraded <= 0:
                policy["decision"] = "SKIP"
                policy["leverage"] = 0
                policy["action"] = "SKIP_MTF_DEGRADED"
            else:
                policy["decision"] = "REDUCE"
                policy["leverage"] = 1.0
                policy["action"] = "PARTIAL_ENTRY_MTF_DEGRADED"
            policy["mtf_status"] = "degraded"
            policy["health_status"] = status
            policy.setdefault("reasons", []).insert(0, "Health gate: full MTF degraded, V5/V5.1 reduced")
        fast = policies.get("v52_fast_1h")
        if fast:
            if "1h" in failed:
                current = float(fast.get("risk_weight") or 0)
                degraded = step_weight(current, -0.25)
                fast["risk_weight"] = degraded
                if degraded <= 0:
                    fast["decision"] = "SKIP"
                    fast["leverage"] = 0
                    fast["action"] = "SKIP_1H_DEGRADED"
                else:
                    fast["decision"] = "REDUCE"
                    fast["leverage"] = 1.0
                    fast["action"] = "PARTIAL_ENTRY_1H_DEGRADED"
                fast["mtf_status"] = "degraded"
                fast["health_status"] = status
                fast.setdefault("reasons", []).insert(0, "Health gate: 1H unavailable, V5.2 reduced")
            else:
                fast["health_status"] = "healthy_fast_1h"
                fast.setdefault("reasons", []).insert(0, "Health gate: full MTF partial, but 1H is available for V5.2")
    else:
        for policy in policies.values():
            policy["health_status"] = status or "unknown"
    return policies


def build_policies(normalized: dict, values: dict | None, mtf_values: dict | None = None) -> dict:
    ctx = base_context(normalized, values)
    mtf_ctxs = mtf_contexts(normalized, mtf_values)
    out = {}
    if "duo_raw" in POLICY_KEYS:
        out["duo_raw"] = decide_duo_raw(normalized, ctx)
    needs_v3 = any(key in POLICY_KEYS for key in ("v52_fast_1h", "v5_mtf", "v51_balanced"))
    v3 = decide_v3_r75(ctx) if needs_v3 else None
    if "v52_fast_1h" in POLICY_KEYS:
        out["v52_fast_1h"] = decide_v52_fast_1h(ctx, v3, mtf_ctxs)
    if "v6_regime_duo" in POLICY_KEYS:
        out["v6_regime_duo"] = decide_v6_regime_duo(ctx, mtf_ctxs)
    if "duo_conviction_sized" in POLICY_KEYS:
        out["duo_conviction_sized"] = decide_duo_conviction_sized(normalized, ctx, mtf_ctxs)
    if "conviction_v2_candidate" in POLICY_KEYS:
        out["conviction_v2_candidate"] = decide_conviction_v2_candidate(normalized, ctx, mtf_ctxs)
    if "duo_regime_rsi_sized" in POLICY_KEYS:
        out["duo_regime_rsi_sized"] = decide_duo_regime_rsi_sized(normalized, ctx, mtf_ctxs)
    if "duo_regime_rsi_30m" in POLICY_KEYS:
        out["duo_regime_rsi_30m"] = decide_duo_regime_rsi_30m(normalized, ctx, mtf_ctxs)
    return out


def empty_paper_state() -> dict:
    return {"version": 3, "updated_at": None, "policies": {}, "realistic_policies": {}, "compound_policies": {}}


def load_paper_state() -> dict:
    # journal backend: the journal is authoritative. Derive state from the latest
    # VERIFIED checkpoint + only the newer live records, keeping replay bounded
    # (REFACTOR_PLAN.md:220, :239). With no trustworthy checkpoint this falls back
    # to a full from-empty replay -- which is byte-identical in RESULT, the core
    # invariant (E5: a missing/corrupt snapshot rebuilds from the journal instead
    # of silently resetting to empty).
    if HERMX_STATE_BACKEND == "journal":
        state = _load_from_checkpoint()
        if state is not None:
            return state
        return replay_position_journal()
    # legacy backend: byte-identical to pre-Phase-1 behavior — snapshot is the
    # source of truth, and ANY read error returns empty (the E5 footgun we keep
    # ONLY in legacy mode for behavioral compatibility during the soak).
    empty = empty_paper_state()
    if not PAPER_STATE_FILE.exists():
        return empty
    try:
        state = json.loads(PAPER_STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("version", 3)
        state.setdefault("policies", {})
        state.setdefault("realistic_policies", {})
        state.setdefault("compound_policies", {})
        return state
    except Exception:
        return empty


def save_paper_state(state: dict) -> None:
    # Atomic + durable (REFACTOR_PLAN.md:206 E4): write tmp, fsync the tmp file,
    # then atomic-rename. Best-effort fsync of the directory so the rename itself
    # is durable. Content is byte-identical to the previous writer (indent=2, no
    # trailing newline) so the legacy golden snapshot is unchanged.
    with _STATE_WRITE_LOCK:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{PAPER_STATE_FILE.name}.", suffix=".tmp", dir=str(PAPER_STATE_FILE.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(PAPER_STATE_FILE)
            _fsync_dir(PAPER_STATE_FILE.parent)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

def default_control_state() -> dict:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "mode": "shadow_only",
        "live_trading": "paused",
        "manual_pause": False,
        "pause_reason": "",
        "symbol_pauses": {},
        "allowed_assets": list(ALLOWED_SYMBOLS),
        "allowed_policies": list(POLICY_KEYS),
        "risk_limits": {
            "max_daily_loss_usd": float(CONFIG.get("risk", {}).get("max_daily_loss_usd", 150.0)),
            "max_slippage_pct": float(CONFIG.get("risk", {}).get("max_slippage_pct", 0.25)),
        },
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
        merged = default | state
        merged["risk_limits"] = default.get("risk_limits", {}) | state.get("risk_limits", {})
        merged["symbol_pauses"] = state.get("symbol_pauses") if isinstance(state.get("symbol_pauses"), dict) else {}
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


def realistic_base_notional_usd(symbol: str) -> float:
    asset = (CONFIG.get("assets") or {}).get(symbol) or {}
    budget = float(asset.get("budget_usd") or PAPER_BASE_NOTIONAL_USD)
    leverage = float(asset.get("leverage") or 1.0)
    return budget * leverage


def asset_budget_usd(symbol: str) -> float:
    asset = (CONFIG.get("assets") or {}).get(symbol) or {}
    return float(asset.get("budget_usd") or PAPER_BASE_NOTIONAL_USD)


def asset_leverage(symbol: str) -> float:
    asset = (CONFIG.get("assets") or {}).get(symbol) or {}
    return float(asset.get("leverage") or 1.0)


def refresh_compound_stats(ps: dict) -> None:
    equity = ps.get("equity") or {}
    initial = ps.get("initial_equity") or {}
    total_equity = sum((D(v or 0.0) for v in equity.values()), D("0"))
    total_initial = sum((D(v or 0.0) for v in initial.values()), D("0"))
    stats = ps.setdefault("stats", empty_policy_stats())
    stats["current_equity_usd"] = float(dec_usd(total_equity))
    stats["initial_equity_usd"] = float(dec_usd(total_initial))
    stats["equity_change_usd"] = float(dec_usd(total_equity - total_initial))
    stats["equity_change_pct"] = float(dec_pct(((total_equity / total_initial) - D("1")) * D("100"))) if total_initial != 0 else 0.0


def empty_policy_stats() -> dict:
    return {
        "realized_pnl_usd": 0.0,
        "realized_gross_pnl_usd": 0.0,
        "realized_net_pnl_usd": 0.0,
        "realized_pnl_pct_weighted": 0.0,
        "realized_gross_pnl_pct_weighted": 0.0,
        "realized_net_pnl_pct_weighted": 0.0,
        "total_fees_usd": 0.0,
        "total_funding_usd": 0.0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "skips": 0,
        "entries": 0,
    }


def side_to_position(side: str) -> str:
    return "long" if side == "buy" else "short"


def D(value, default: str = "0") -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def dec_usd(value) -> Decimal:
    return D(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def dec_notional(value) -> Decimal:
    return D(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def dec_pct(value) -> Decimal:
    return D(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def dec_units(value) -> Decimal:
    return D(value).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def dec_text(value: Decimal | str | float | int) -> str:
    return format(D(value), "f")


def usd_text(value) -> str:
    return dec_text(dec_usd(value))


def notional_text(value) -> str:
    return dec_text(dec_notional(value))


def pct_text(value) -> str:
    return dec_text(dec_pct(value))


def units_text(value) -> str:
    return dec_text(dec_units(value))


_USD_KEYS = {
    "base_notional_usd",
    "notional_usd",
    "entry_fee_usd",
    "exit_fee_usd",
    "total_fees_usd",
    "funding_usd",
    "gross_pnl_usd",
    "net_pnl_usd",
    "pnl_usd",
    "realized_pnl_usd",
    "realized_net_pnl_usd",
    "realized_gross_pnl_usd",
    "current_equity_usd",
    "initial_equity_usd",
    "equity_change_usd",
    "equity_usd",
    "gross_usd",
    "net_usd",
    "entry_fee",
    "exit_fee",
    "equity_set",
}
_PCT_KEYS = {
    "risk_weight",
    "weight",
    "pnl_pct",
    "weighted_pnl_pct",
    "net_weighted_pnl_pct",
    "realized_pnl_pct",
    "realized_net_pnl_pct_weighted",
    "realized_gross_pnl_pct_weighted",
    "realized_pnl_pct_weighted",
    "alert_execution_diff_pct",
    "weighted_pct",
    "net_weighted_pct",
    "equity_change_pct",
}
_UNITS_KEYS = {"qty_units", "filled_size"}


def canonicalize_decimal_fields(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if item is None:
                out[key] = None
            elif isinstance(item, (dict, list)):
                out[key] = canonicalize_decimal_fields(item)
            elif key_text == "planned_notional_usd":
                out[key] = notional_text(item)
            elif key_text in _USD_KEYS or key_text.endswith("_usd"):
                out[key] = usd_text(item)
            elif key_text in _PCT_KEYS or key_text.endswith("_pct"):
                out[key] = pct_text(item)
            elif key_text in _UNITS_KEYS or key_text.endswith("_units"):
                out[key] = units_text(item)
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        return [canonicalize_decimal_fields(item) for item in value]
    return value


def _coerce_state_numeric_fields(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if isinstance(item, (dict, list)):
                out[key] = _coerce_state_numeric_fields(item)
                continue
            if not isinstance(item, str):
                out[key] = item
                continue
            looks_numeric = (
                key_text in _USD_KEYS
                or key_text in _PCT_KEYS
                or key_text in _UNITS_KEYS
                or key_text.endswith("_usd")
                or key_text.endswith("_pct")
                or key_text.endswith("_units")
                or key_text.endswith("_price")
                or key_text in {"weight", "entry_fee", "equity_set"}
            )
            if looks_numeric:
                try:
                    out[key] = float(D(item))
                    continue
                except Exception:
                    pass
            out[key] = item
        return out
    if isinstance(value, list):
        return [_coerce_state_numeric_fields(item) for item in value]
    return value


def pnl_pct(position_side: str, entry_price: float, exit_price: float) -> float:
    entry = D(entry_price)
    exit_ = D(exit_price)
    if entry == 0 or exit_ == 0:
        return 0.0
    if position_side == "long":
        return float(dec_pct((exit_ / entry - D("1")) * D("100")))
    return float(dec_pct((entry / exit_ - D("1")) * D("100")))


def fee_rate_for(liquidity: str | None = None) -> float:
    liq = (liquidity or PAPER_DEFAULT_LIQUIDITY or "taker").lower()
    return OKX_PERP_MAKER_FEE_RATE if liq == "maker" else OKX_PERP_TAKER_FEE_RATE


def fee_usd(notional_usd: float, liquidity: str | None = None) -> float:
    return float(dec_usd(D(notional_usd) * D(fee_rate_for(liquidity))))


def funding_rate_from_payload(payload: dict | None = None) -> float:
    payload = payload or {}
    rate = as_float(first(payload, "funding_rate", "okx_funding_rate", default=None))
    if rate is None:
        rate = PAPER_DEFAULT_FUNDING_RATE
    return float(dec_pct(rate or 0.0))


def estimate_funding_usd(position: dict, payload: dict | None = None) -> float:
    # Placeholder until OKX funding timestamps are wired. Default is 0.
    if not PAPER_FUNDING_ENABLED:
        return 0.0
    rate = D(funding_rate_from_payload(payload))
    notional = D((position or {}).get("notional_usd") or 0.0)
    side = (position or {}).get("side")
    # Positive funding rate usually means longs pay shorts.
    sign = D("-1") if side == "long" else D("1")
    return float(dec_usd(notional * rate * sign))


def executed_price_from_signal(signal_price: float, payload: dict | None = None) -> tuple[float, float]:
    payload = payload or {}
    execution_price = as_float(first(payload, "okx_execution_price", "execution_price", "fill_price", "avg_fill_price", default=None))
    if execution_price is None:
        execution_price = float(D(signal_price))
    signal = D(signal_price)
    executed = D(execution_price)
    diff_pct = D("0") if signal == 0 else dec_pct((executed / signal - D("1")) * D("100"))
    return float(executed), float(diff_pct)


# ---------------------------------------------------------------------------
# Event-sourced position state (REFACTOR_PLAN.md:202 Phase 1 task 1).
#
# ONE mutation routine, apply_effect(), is the *only* code that mutates paper
# state. Both the live path (paper_apply_policy, via _record_transition) and
# replay (replay_position_journal) drive state exclusively through it, so a
# replay of the journal is identical to the live run that produced it — the math
# is written once. An "effect" is a fully-resolved description of a single
# transition: it carries the already-computed numbers (stat deltas, the new
# position dict, the close result, the new equity), so apply_effect performs NO
# business math — only assignment/accumulation. paper_apply_policy still computes
# those numbers exactly as before and packages them into the effect.
#
# effect shape (schema_version 1):
#   {"op": "skip"}                                   # stats.skips += 1
#   {"op": "adjust", "fields": {...}}                # pos.update(fields) (same dir)
#   {"op": "close",  "gross_usd","net_usd","weighted_pct","net_weighted_pct",
#                    "exit_fee","funding_usd","win": bool,
#                    "equity_set": <float, compound only>}    # close + stat deltas
#   {"op": "open",   "position": {...}, "entry_fee": <float>} # open + entries/fees
# Any effect may also carry "compound": true and "initial_equity_seed": <float> so
# apply_effect can (idempotently) seed initial_equity/equity for the symbol the
# same way the live path's compound preamble does.
# ---------------------------------------------------------------------------

def _ensure_policy_bucket(state: dict, account_key: str, policy_key: str) -> dict:
    policies_state = state.setdefault(account_key, {})
    ps = policies_state.setdefault(policy_key, {"label": POLICY_LABELS.get(policy_key, policy_key), "symbols": {}, "stats": empty_policy_stats()})
    ps.setdefault("stats", empty_policy_stats())
    ps.setdefault("symbols", {})
    return ps


def apply_effect(state: dict, account: str, policy: str, symbol: str, effect: dict) -> None:
    """The single, shared state-mutation routine. Live and replay both call this."""
    ps = _ensure_policy_bucket(state, account, policy)
    if effect.get("compound"):
        initial = ps.setdefault("initial_equity", {})
        equity = ps.setdefault("equity", {})
        initial.setdefault(symbol, effect.get("initial_equity_seed"))
        equity.setdefault(symbol, initial[symbol])
    stats = ps["stats"]
    symbols = ps["symbols"]
    op = effect.get("op")
    if op == "skip":
        stats["skips"] = int(stats.get("skips", 0)) + 1
        return
    if op == "adjust":
        pos = symbols.get(symbol)
        if pos is not None:
            pos.update(_coerce_state_numeric_fields(effect.get("fields") or {}))
        return
    if op == "close":
        symbols.pop(symbol, None)
        stats["closed_trades"] = int(stats.get("closed_trades", 0)) + 1
        stats["realized_gross_pnl_usd"] = float(dec_usd(D(stats.get("realized_gross_pnl_usd", 0)) + D(effect["gross_usd"])))
        stats["realized_net_pnl_usd"] = float(dec_usd(D(stats.get("realized_net_pnl_usd", 0)) + D(effect["net_usd"])))
        stats["realized_pnl_usd"] = stats["realized_net_pnl_usd"]
        stats["realized_gross_pnl_pct_weighted"] = float(dec_pct(D(stats.get("realized_gross_pnl_pct_weighted", 0)) + D(effect["weighted_pct"])))
        stats["realized_net_pnl_pct_weighted"] = float(dec_pct(D(stats.get("realized_net_pnl_pct_weighted", 0)) + D(effect["net_weighted_pct"])))
        stats["realized_pnl_pct_weighted"] = stats["realized_net_pnl_pct_weighted"]
        stats["total_fees_usd"] = float(dec_usd(D(stats.get("total_fees_usd", 0)) + D(effect["exit_fee"])))
        stats["total_funding_usd"] = float(dec_usd(D(stats.get("total_funding_usd", 0)) + D(effect["funding_usd"])))
        if effect.get("compound"):
            equity = ps.setdefault("equity", {})
            equity[symbol] = float(dec_usd(effect["equity_set"]))
            refresh_compound_stats(ps)
        if effect["win"]:
            stats["wins"] = int(stats.get("wins", 0)) + 1
        else:
            stats["losses"] = int(stats.get("losses", 0)) + 1
        return
    if op == "open":
        entry_fee = effect["entry_fee"]
        stats["total_fees_usd"] = float(dec_usd(D(stats.get("total_fees_usd", 0)) + D(entry_fee)))
        symbols[symbol] = _coerce_state_numeric_fields(effect["position"])
        stats["entries"] = int(stats.get("entries", 0)) + 1
        if effect.get("compound"):
            refresh_compound_stats(ps)
        return
    raise ValueError(f"apply_effect: unknown op {op!r}")


def _record_transition(state: dict, account: str, policy: str, symbol: str, effect: dict, received_at) -> None:
    """LIVE path: write-ahead-journal the effect (durable fsync) BEFORE applying it
    to in-memory state, then apply via the shared apply_effect. The journal is
    written in BOTH backends (REFACTOR_PLAN.md:225 parallel soak data); only the
    load/authority path differs by backend."""
    record = {
        "schema_version": POSITION_JOURNAL_SCHEMA_VERSION,
        "seq": _journal_next_seq(),
        "ts": received_at,
        "kind": "transition",
        "account": account,
        "policy": policy,
        "symbol": symbol,
        "effect": canonicalize_decimal_fields(effect),
    }
    # Write-ahead, fail-closed (REFACTOR_PLAN.md:221): the durable journal append
    # must succeed BEFORE in-memory state is mutated. If it fails (e.g. ENOSPC) we
    # surface an operator alert and re-raise so submission is blocked and state is
    # NOT silently advanced past an unpersisted transition.
    try:
        append_jsonl_durable(POSITION_JOURNAL_LEDGER, record)
    except OSError as exc:
        _fail_closed_state_write("journal-append", exc, context={"account": account, "policy": policy, "symbol": symbol, "seq": record.get("seq")})
        raise
    apply_effect(state, account, policy, symbol, effect)


def _apply_records(state: dict, records: list) -> tuple:
    """Apply an ordered list of journal records to ``state`` via the shared
    apply_effect. Returns (last_ts, records_consumed, last_seq). Enforces the
    schema_version forward-compat rule (a newer writer's record is a hard error,
    never silently misread)."""
    last_ts = None
    consumed = 0
    last_seq = -1
    for rec in records:
        sv = rec.get("schema_version")
        if not isinstance(sv, int) or sv > POSITION_JOURNAL_SCHEMA_VERSION:
            logging.error("position journal: unsupported schema_version %r (reader=%d) at seq %r", sv, POSITION_JOURNAL_SCHEMA_VERSION, rec.get("seq"))
            raise ValueError(f"position journal schema_version {sv!r} > reader {POSITION_JOURNAL_SCHEMA_VERSION}")
        s = rec.get("seq")
        if isinstance(s, int) and s > last_seq:
            last_seq = s
        if rec.get("kind") != "transition":
            continue
        apply_effect(state, rec["account"], rec["policy"], rec["symbol"], rec["effect"])
        consumed += 1
        if rec.get("ts") is not None:
            last_ts = rec["ts"]
    return last_ts, consumed, last_seq


def _parse_sealed_seq(name: str):
    """The sealed-segment seq encoded in a filename ``position-journal.<seq>.jsonl``
    (the last seq that segment covers), or None if the name is not a sealed
    segment. Reads the seq from the NAME so seq derivation needs no file read."""
    prefix, suffix = "position-journal.", ".jsonl"
    if name.startswith(prefix) and name.endswith(suffix):
        mid = name[len(prefix):-len(suffix)]
        if mid.isdigit():
            return int(mid)
    return None


def _sealed_segment_paths() -> list:
    """Sealed journal segments as (seq, path), ascending by seq. The live segment
    (``position-journal.jsonl``) and the ``.corrupt`` quarantine file are excluded
    by the naming rule; the checkpoint (``.json``) is excluded by suffix."""
    out = []
    for p in LOG_DIR.glob("position-journal.*.jsonl"):
        seq = _parse_sealed_seq(p.name)
        if seq is not None:
            out.append((seq, p))
    out.sort(key=lambda t: t[0])
    return out


def _read_all_journal_records() -> list:
    """Every record across sealed segments (seq order) + the live segment, sorted
    by seq. This is the full history used by a from-empty replay; rotation seals
    contiguous seq ranges so concatenation is already seq-ordered, but we sort
    defensively to make the replay-order invariant explicit (REFACTOR_PLAN.md:220)."""
    records: list = []
    for _seq, path in _sealed_segment_paths():
        records.extend(read_jsonl_tolerant(path))
    records.extend(read_jsonl_tolerant(POSITION_JOURNAL_LEDGER))
    records.sort(key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else -1)
    return records


def replay_position_journal() -> dict:
    """Full replay from EMPTY across ALL segments (sealed + live) via the SAME
    apply_effect the live path uses (this is what guarantees replay == live).
    Tolerates a truncated trailing record. This is the authoritative fallback
    under the journal backend and the equivalence oracle the checkpoint is verified
    against (REFACTOR_PLAN.md:202, :219, :233)."""
    state = empty_paper_state()
    last_ts, _consumed, _last_seq = _apply_records(state, _read_all_journal_records())
    state["updated_at"] = last_ts
    return state


# ---------------------------------------------------------------------------
# Journal lifecycle: verified checkpoint + segment rotation (Phase 1 task 7,
# REFACTOR_PLAN.md:218-221). Keeps journal-mode startup replay bounded: a load
# starts from the latest VERIFIED checkpoint and replays only the records newer
# than it, instead of replaying the entire history from empty on every call.
# ---------------------------------------------------------------------------

def _canonical_state_json(state: dict) -> str:
    """Canonical JSON for hashing: sorted keys, compact separators. Independent of
    the pretty-printed on-disk form, so checkpoint formatting cannot affect the
    integrity hash."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _state_hash(state: dict) -> str:
    return hashlib.sha256(_canonical_state_json(state).encode("utf-8")).hexdigest()


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
    """Write JSON atomically + durably (tmp -> fsync -> replace -> dir fsync), the
    same discipline as save_paper_state. Propagates OSError so the caller can fail
    closed on a full disk (REFACTOR_PLAN.md:221)."""
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


def _read_checkpoint() -> "dict | None":
    """Load the checkpoint with VERIFY-BEFORE-TRUST (REFACTOR_PLAN.md:219). Returns
    the checkpoint dict only if (a) it parses, (b) its schema_version/
    checkpoint_version are not from a newer writer, and (c) its stored state_hash
    recomputes over the stored state. Any failure is loud and returns None, which
    makes the caller DISCARD the checkpoint and fall back to full replay -- a
    corrupt or forward-version checkpoint is never trusted."""
    if not POSITION_JOURNAL_CHECKPOINT_FILE.exists():
        return None
    try:
        ckpt = json.loads(POSITION_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.error("position-journal checkpoint unreadable (%s) -- DISCARDING, full replay", exc)
        return None
    if not isinstance(ckpt, dict):
        logging.error("position-journal checkpoint is not an object -- DISCARDING, full replay")
        return None
    sv = ckpt.get("schema_version")
    cv = ckpt.get("checkpoint_version")
    if not isinstance(sv, int) or sv > POSITION_JOURNAL_SCHEMA_VERSION or not isinstance(cv, int) or cv > POSITION_JOURNAL_CHECKPOINT_VERSION:
        logging.error("position-journal checkpoint from a newer writer (schema=%r checkpoint=%r, reader schema=%d checkpoint=%d) -- DISCARDING, full replay", sv, cv, POSITION_JOURNAL_SCHEMA_VERSION, POSITION_JOURNAL_CHECKPOINT_VERSION)
        return None
    state = ckpt.get("state")
    last_seq = ckpt.get("last_seq")
    if not isinstance(state, dict) or not isinstance(last_seq, int):
        logging.error("position-journal checkpoint missing state/last_seq -- DISCARDING, full replay")
        return None
    if ckpt.get("state_hash") != _state_hash(state):
        logging.error("position-journal checkpoint state_hash MISMATCH -- DISCARDING corrupt checkpoint, full replay")
        return None
    return ckpt


def _checkpoint_last_seq_floor() -> "int | None":
    """The checkpoint's last_seq used only as a monotonic floor for seq derivation.
    Best-effort and tolerant of a partly-corrupt checkpoint: a hash mismatch does
    not matter here (an over-high floor only skips seq numbers, never reuses them),
    so we read last_seq directly without the full verify."""
    if not POSITION_JOURNAL_CHECKPOINT_FILE.exists():
        return None
    try:
        ckpt = json.loads(POSITION_JOURNAL_CHECKPOINT_FILE.read_text(encoding="utf-8"))
        ls = ckpt.get("last_seq")
        return ls if isinstance(ls, int) else None
    except Exception:
        return None


def _load_from_checkpoint() -> "dict | None":
    """Bounded journal-mode load (REFACTOR_PLAN.md:220, :239): start from a VERIFIED
    checkpoint's state (deep-copied so the on-disk dict is never mutated) and replay
    ONLY the records newer than checkpoint.last_seq from the live segment. Sealed
    segments are subsumed by the checkpoint, so they are not replayed. Returns None
    when there is no trustworthy checkpoint, signalling a full replay.

    Result invariant: identical to replay_position_journal() (full from-empty
    replay). updated_at carries over from the checkpoint state and is only advanced
    by newer records, matching the full-replay's last-ts semantics."""
    ckpt = _read_checkpoint()
    if ckpt is None:
        return None
    state = copy.deepcopy(ckpt["state"])
    last_seq = ckpt["last_seq"]
    newer = [r for r in read_jsonl_tolerant(POSITION_JOURNAL_LEDGER) if isinstance(r.get("seq"), int) and r["seq"] > last_seq]
    newer.sort(key=lambda r: r["seq"])
    last_ts, _consumed, _last = _apply_records(state, newer)
    if last_ts is not None:
        state["updated_at"] = last_ts
    return state


def _rotate_live_segment(last_seq: int) -> None:
    """Seal the live segment to ``position-journal.<last_seq>.jsonl`` and start a
    fresh empty live segment. Atomic rename; propagates OSError to fail closed.
    Called only AFTER a verified checkpoint covering last_seq has been fsync'd, so a
    crash between the two is safe: on restart the checkpoint replays only seq >
    last_seq from the (still-unrotated) live segment and ignores the rest."""
    if not POSITION_JOURNAL_LEDGER.exists():
        return
    sealed = LOG_DIR / f"position-journal.{last_seq}.jsonl"
    os.replace(POSITION_JOURNAL_LEDGER, sealed)
    POSITION_JOURNAL_LEDGER.touch()
    _fsync_dir(LOG_DIR)


def _enforce_segment_retention() -> None:
    """Keep the last K sealed segments; prune older ones. Safe because the verified
    checkpoint subsumes every sealed segment -- they are retained only for forensic
    replay (REFACTOR_PLAN.md:221). K < 0 keeps all."""
    if HERMX_JOURNAL_SEGMENT_RETENTION < 0:
        return
    sealed = _sealed_segment_paths()
    excess = len(sealed) - HERMX_JOURNAL_SEGMENT_RETENTION
    for _seq, path in sealed[:max(0, excess)]:
        try:
            path.unlink()
        except OSError as exc:
            logging.warning("position-journal: could not prune sealed segment %s: %s", path, exc)


def _checkpoint_and_rotate(state: dict) -> None:
    """Write a verified checkpoint covering everything currently journaled, then
    rotate the live segment (REFACTOR_PLAN.md:218-221). VERIFY-BEFORE-TRUST: the
    checkpoint is only written after asserting that a full from-empty replay equals
    the state built from (previous checkpoint + newer live records); if they
    diverge we refuse to checkpoint (loud, no rotation) rather than persist a state
    we cannot prove. Fail-closed on any write OSError. ``state`` is the live
    in-memory state (used only as a sanity oracle); the checkpoint stores the
    from-empty replay, which is the exact thing a subsequent load reconstructs."""
    records = _read_all_journal_records()
    full = empty_paper_state()
    last_ts, consumed, last_seq = _apply_records(full, records)
    full["updated_at"] = last_ts
    if consumed == 0:
        return  # nothing to checkpoint yet
    incremental = _load_from_checkpoint()
    if incremental is None:
        # First checkpoint: the full from-empty replay IS the authoritative state.
        chosen = full
    else:
        # Verify-before-trust (REFACTOR_PLAN.md:219). The incremental state
        # (prev *hash-verified* checkpoint + records newer than it) is the authority
        # the load path uses. The full from-empty replay is an independent oracle to
        # cross-check it -- but it is only a VALID oracle while the on-disk history is
        # still complete from seq 0. Retention pruning deliberately discards sealed
        # segments already subsumed by a checkpoint, after which a from-empty replay
        # is necessarily incomplete and must NOT be used to "refute" the incremental
        # state. So: if history is complete (min seq == 0) assert full == incremental
        # (strong equivalence); once pruning has occurred, the prior checkpoint's
        # verified hash is the trust anchor and we persist the incremental state.
        min_seq = min((r.get("seq") for r in records if isinstance(r.get("seq"), int)), default=0)
        if min_seq == 0:
            if _state_hash(incremental) != _state_hash(full):
                logging.error("position-journal checkpoint ABORTED: incremental(prev checkpoint + newer) != full replay -- refusing to persist an unverified checkpoint (last_seq=%s)", last_seq)
                raise RuntimeError("position-journal checkpoint equivalence verification failed")
            chosen = full
        else:
            chosen = incremental
    # Sanity: the live in-memory state should also equal the chosen reconstruction
    # (the task-1 invariant that replay == live). Log if it diverges; the verified
    # reconstruction is authoritative.
    if _state_hash(state) != _state_hash(chosen):
        logging.warning("position-journal checkpoint: in-memory state != verified reconstruction (last_seq=%s); persisting the reconstruction", last_seq)
    ckpt = {
        "schema_version": POSITION_JOURNAL_SCHEMA_VERSION,
        "checkpoint_version": POSITION_JOURNAL_CHECKPOINT_VERSION,
        "last_seq": last_seq,
        # Seqs are contiguous from 0, so the checkpoint folds in last_seq+1 records.
        "records_consumed": last_seq + 1,
        "state": chosen,
        "state_hash": _state_hash(chosen),
        "created_at": now_iso(),
    }
    try:
        _atomic_json_dump(POSITION_JOURNAL_CHECKPOINT_FILE, ckpt)  # fsync'd before rotate
        _rotate_live_segment(last_seq)
    except OSError as exc:
        _fail_closed_state_write("checkpoint-rotate", exc, context={"last_seq": last_seq})
        raise
    _enforce_segment_retention()


def _maybe_checkpoint_and_rotate(state: dict) -> None:
    """Journal-mode only: trigger a checkpoint+rotation once the live segment grows
    past HERMX_JOURNAL_SEGMENT_MAX_RECORDS, keeping replay bounded and disk growth
    capped. No-op in legacy mode (no checkpoint files are ever created there)."""
    if HERMX_STATE_BACKEND != "journal":
        return
    live = read_jsonl_tolerant(POSITION_JOURNAL_LEDGER)
    if len(live) < HERMX_JOURNAL_SEGMENT_MAX_RECORDS:
        return
    _checkpoint_and_rotate(state)


def paper_apply_policy(state: dict, normalized: dict, payload: dict, policy_key: str, policy: dict, price: float, received_at: str, base_notional_usd: float, account_key: str, account_label: str, *, compound: bool = False) -> dict:
    policies_state = state.setdefault(account_key, {})
    ps = policies_state.setdefault(policy_key, {"label": POLICY_LABELS.get(policy_key, policy_key), "symbols": {}, "stats": empty_policy_stats()})
    ps.setdefault("stats", empty_policy_stats())
    sym = normalized["symbol"]
    if compound:
        initial = ps.setdefault("initial_equity", {})
        equity = ps.setdefault("equity", {})
        initial.setdefault(sym, asset_budget_usd(sym))
        equity.setdefault(sym, initial[sym])
        base_notional_usd = float(dec_usd(max(D("0"), D(equity.get(sym) or 0.0)) * D(asset_leverage(sym))))
    symbols = ps.setdefault("symbols", {})
    pos = symbols.get(sym)
    target_side = side_to_position(normalized["side"])
    weight_dec = D(policy.get("risk_weight") or 0.0)
    base_notional_dec = D(base_notional_usd)
    decision = str(policy.get("decision") or "SKIP").upper()
    event = {
        "received_at": received_at,
        "paper_account": account_key,
        "paper_account_label": account_label,
        "base_notional_usd": float(dec_usd(base_notional_dec)),
        "policy": policy_key,
        "policy_label": POLICY_LABELS.get(policy_key, policy_key),
        "symbol": sym,
        "signal_side": normalized["side"],
        "signal_price": price,
        "tv_time": normalized.get("tv_time"),
        "chart_type": normalized.get("chart_type"),
        "okx_mark_price": normalized.get("okx_mark_price"),
        "okx_last_price": normalized.get("okx_last_price"),
        "okx_execution_price": None,
        "alert_execution_diff_pct": None,
        "decision": decision,
        "risk_weight": float(dec_pct(weight_dec)),
        "actions": [],
        "realized_pnl_pct": 0.0,
        "realized_pnl_usd": 0.0,
    }
    exec_price, alert_diff_pct = executed_price_from_signal(price, payload)
    event["okx_execution_price"] = exec_price
    event["alert_execution_diff_pct"] = alert_diff_pct
    liquidity = str(first(payload or {}, "liquidity", "execution_liquidity", default=PAPER_DEFAULT_LIQUIDITY)).lower()
    entry_fee_rate = fee_rate_for(liquidity)
    # Phase 1 task 1: state mutations below are packaged as `effect`s and applied
    # via the shared apply_effect (through _record_transition, which also journals
    # write-ahead). The compound preamble above already seeded equity/initial_equity
    # in-place; carrying the seed lets apply_effect reproduce that seeding on replay.
    seed = asset_budget_usd(sym) if compound else None

    def _eff(op, **extra):
        e = {"op": op}
        if compound:
            e["compound"] = True
            e["initial_equity_seed"] = seed
        e.update(extra)
        return e

    if price is None:
        _record_transition(state, account_key, policy_key, sym, _eff("skip"), received_at)
        event["actions"].append("SKIP_NO_NEW_ENTRY")
        return event

    if (decision == "SKIP" or weight_dec <= 0) and not (pos and pos.get("side") != target_side):
        _record_transition(state, account_key, policy_key, sym, _eff("skip"), received_at)
        event["actions"].append("SKIP_NO_NEW_ENTRY")
        return event

    if pos and pos.get("side") == target_side:
        # Duo should not duplicate often, but keep the state stable if it happens.
        old_weight = D(pos.get("weight") or 0)
        fields = {
            "weight": float(dec_pct(weight_dec)),
            "last_signal_at": received_at,
            "last_signal_price": price,
            "last_execution_price": exec_price,
        }
        _record_transition(state, account_key, policy_key, sym, _eff("adjust", fields=fields), received_at)
        event["actions"].append(f"UPDATE_SAME_DIRECTION {float(old_weight):.2f}->{float(weight_dec):.2f}")
        return event

    if pos:
        entry_price_dec = D(pos.get("entry_execution_price") or pos.get("entry_price") or 0)
        qty_units_dec = D(pos.get("qty_units") or 0)
        exec_price_dec = D(exec_price)
        exit_notional_dec = qty_units_dec * exec_price_dec
        pct_dec = D(pnl_pct(pos.get("side"), float(entry_price_dec), float(exec_price_dec)))
        weighted_pct_dec = pct_dec * D(pos.get("weight") or 0)
        gross_usd_dec = D(pos.get("notional_usd") or 0) * pct_dec / D("100")
        entry_fee_dec = D(pos.get("entry_fee_usd") or 0)
        exit_fee_dec = D(fee_usd(float(exit_notional_dec), liquidity))
        total_trade_fees_dec = entry_fee_dec + exit_fee_dec
        funding_usd_dec = D(estimate_funding_usd(pos, payload))
        net_usd_dec = gross_usd_dec - total_trade_fees_dec + funding_usd_dec
        net_weighted_pct_dec = (net_usd_dec / base_notional_dec) * D("100") if base_notional_dec != 0 else D("0")
        pct = float(dec_pct(pct_dec))
        weighted_pct = float(dec_pct(weighted_pct_dec))
        gross_usd = float(dec_usd(gross_usd_dec))
        entry_fee = float(dec_usd(entry_fee_dec))
        exit_fee = float(dec_usd(exit_fee_dec))
        total_trade_fees = float(dec_usd(total_trade_fees_dec))
        funding_usd = float(dec_usd(funding_usd_dec))
        net_usd = float(dec_usd(net_usd_dec))
        net_weighted_pct = float(dec_pct(net_weighted_pct_dec))
        trade = {
            "closed_at": received_at,
            "paper_account": account_key,
            "paper_account_label": account_label,
            "base_notional_usd": float(dec_usd(base_notional_dec)),
            "policy": policy_key,
            "policy_label": POLICY_LABELS.get(policy_key, policy_key),
            "symbol": sym,
            "side": pos.get("side"),
            "entry_price": pos.get("entry_price"),
            "entry_execution_price": pos.get("entry_execution_price"),
            "exit_price": price,
            "exit_execution_price": exec_price,
            "alert_execution_diff_pct": alert_diff_pct,
            "entry_at": pos.get("entry_at"),
            "exit_tv_time": normalized.get("tv_time"),
            "weight": pos.get("weight"),
            "notional_usd": pos.get("notional_usd"),
            "pnl_pct": float(dec_pct(pct)),
            "weighted_pnl_pct": float(dec_pct(weighted_pct)),
            "net_weighted_pnl_pct": float(dec_pct(net_weighted_pct)),
            "gross_pnl_usd": float(dec_usd(gross_usd)),
            "entry_fee_usd": float(dec_usd(entry_fee)),
            "exit_fee_usd": float(dec_usd(exit_fee)),
            "total_fees_usd": float(dec_usd(total_trade_fees)),
            "funding_usd": float(dec_usd(funding_usd)),
            "chart_type": normalized.get("chart_type"),
            "okx_mark_price": normalized.get("okx_mark_price"),
            "okx_last_price": normalized.get("okx_last_price"),
            "pnl_usd": float(dec_usd(net_usd)),
            "net_pnl_usd": float(dec_usd(net_usd)),
            "fee_rate": fee_rate_for(liquidity),
            "liquidity": liquidity,
            "exit_signal_side": normalized["side"],
        }
        record_pipeline_event("paper_trade", normalized.get("signal_id"), trade)
        # The paper_trade pipeline append above is an OUTPUT ledger, not state, so it
        # stays in the live path and is NOT replayed. State changes below go through
        # the effect/apply_effect path. Compute the post-close equity here (reusing
        # the numbers above) so apply_effect just assigns it.
        close_eff = _eff(
            "close",
            gross_usd=float(dec_usd(gross_usd)),
            net_usd=float(dec_usd(net_usd)),
            weighted_pct=float(dec_pct(weighted_pct)),
            net_weighted_pct=float(dec_pct(net_weighted_pct)),
            exit_fee=float(dec_usd(exit_fee)),
            funding_usd=float(dec_usd(funding_usd)),
            win=net_usd_dec > 0,
        )
        if compound:
            cur_equity = ps.setdefault("equity", {})
            close_eff["equity_set"] = float(dec_usd(D(cur_equity.get(sym, asset_budget_usd(sym))) + net_usd_dec))
        _record_transition(state, account_key, policy_key, sym, close_eff, received_at)
        event["actions"].append("CLOSE_" + str(pos.get("side", "")).upper())
        event["closed_trade"] = trade
        event["realized_pnl_pct"] = float(dec_pct(net_weighted_pct))
        event["realized_pnl_usd"] = float(dec_usd(net_usd))
        event["gross_pnl_usd"] = float(dec_usd(gross_usd))
        event["fees_usd"] = float(dec_usd(total_trade_fees))
        event["funding_usd"] = float(dec_usd(funding_usd))
        # (symbol pop is performed by apply_effect for the close effect above.)

    if decision == "SKIP" or weight_dec <= 0:
        _record_transition(state, account_key, policy_key, sym, _eff("skip"), received_at)
        event["actions"].append("SKIP_NO_NEW_ENTRY")
        return event

    if compound:
        base_notional_dec = max(D("0"), D(ps.setdefault("equity", {}).get(sym, asset_budget_usd(sym)))) * D(asset_leverage(sym))
        base_notional_usd = float(dec_usd(base_notional_dec))
        event["base_notional_usd"] = base_notional_usd
        event["equity_usd"] = float(dec_usd(ps.setdefault("equity", {}).get(sym, 0.0)))
    notional_dec = base_notional_dec * weight_dec
    qty_units_dec = (notional_dec / D(exec_price)) if D(exec_price) != 0 else D("0")
    entry_fee_dec = D(fee_usd(float(notional_dec), liquidity))
    notional = float(dec_usd(notional_dec))
    qty_units = float(dec_units(qty_units_dec))
    entry_fee = float(dec_usd(entry_fee_dec))
    position = {
        "side": target_side,
        "entry_price": price,
        "entry_execution_price": exec_price,
        "alert_execution_diff_pct": alert_diff_pct,
        "chart_type": normalized.get("chart_type"),
        "okx_mark_price_at_entry": normalized.get("okx_mark_price"),
        "okx_last_price_at_entry": normalized.get("okx_last_price"),
        "entry_at": received_at,
        "entry_tv_time": normalized.get("tv_time"),
        "entry_signal_side": normalized.get("side"),
        "weight": float(dec_pct(weight_dec)),
        "base_notional_usd": float(dec_usd(base_notional_dec)),
        "notional_usd": notional,
        "qty_units": qty_units,
        "entry_fee_usd": entry_fee,
        "entry_fee_rate": entry_fee_rate,
        "liquidity": liquidity,
        "source_decision": decision,
        "mtf_status": policy.get("mtf_status"),
        "equity_usd": float(dec_usd((ps.get("equity") or {}).get(sym, 0.0))) if compound else None,
    }
    _record_transition(state, account_key, policy_key, sym, _eff("open", position=position, entry_fee=entry_fee), received_at)
    event["entry_fee_usd"] = float(dec_usd(entry_fee))
    event["actions"].append("OPEN_" + target_side.upper())
    return event


def apply_paper_trading(record: dict) -> list[dict]:
    normalized = record.get("normalized") or {}
    price = normalized.get("tv_signal_price")
    if price is None:
        return []
    with _STATE_WRITE_LOCK:
        state = load_paper_state()
        events = []
        realistic_base = realistic_base_notional_usd(normalized.get("symbol"))
        for key in POLICY_KEYS:
            policy = (record.get("policies") or {}).get(key) or {}
            events.append(paper_apply_policy(state, normalized, record.get("payload") or {}, key, policy, float(price), record["received_at"], PAPER_BASE_NOTIONAL_USD, "policies", "Research $10k fixed"))
            events.append(paper_apply_policy(state, normalized, record.get("payload") or {}, key, policy, float(price), record["received_at"], realistic_base, "realistic_policies", "Fixed budget x leverage"))
            events.append(paper_apply_policy(state, normalized, record.get("payload") or {}, key, policy, float(price), record["received_at"], realistic_base, "compound_policies", "Compounding paper", compound=True))
        state["updated_at"] = record["received_at"]
        save_paper_state(state)
        # Journal-mode lifecycle (REFACTOR_PLAN.md:218-221): once the live segment is
        # large enough, write a verified checkpoint and rotate, so the next load replays
        # only the bounded live tail. No-op in legacy mode. A write failure here fails
        # closed (raises) -- this runs BEFORE execute_okx_if_enabled in build_record, so
        # a blocked checkpoint also blocks submission.
        _maybe_checkpoint_and_rotate(state)
        return events


def find_policy_event(events: list[dict], policy_key: str, account_key: str = "realistic_policies") -> dict | None:
    for event in events or []:
        if event.get("paper_account") == account_key and event.get("policy") == policy_key:
            return event
    return None


def build_okx_execution_readiness(record: dict, paper_events: list[dict]) -> dict:
    """Prepare the future OKX order intent without sending any exchange order."""
    normalized = record.get("normalized") or {}
    execution_cfg = CONFIG.get("execution", {}) or {}
    risk_cfg = CONFIG.get("risk", {}) or {}
    execution_policy = str(execution_cfg.get("execution_policy") or "duo_raw")
    shadow_policy = str(execution_cfg.get("shadow_policy") or "duo_regime_rsi_30m")
    execution_event = find_policy_event(paper_events, execution_policy)
    shadow_event = find_policy_event(paper_events, shadow_policy)
    asset_cfg = CONFIG.get("assets", {}).get(normalized.get("symbol"), {}) or {}
    inst_id = asset_cfg.get("inst_id")
    # The shadow / duo_raw path is observe-only: only a per-strategy submit_orders
    # flag arms submission, and that lives on the strategy-file path. The dead config
    # arming flags (execution.enabled / risk.allow_live_execution) are gone.
    live_allowed = False
    signal_identity = _signal_identity(normalized)
    # Distinct clOrdId per leg (close vs open) so a reversal's second leg is not
    # rejected as a duplicate. ``client_order_id`` stays the OPEN-leg id.
    client_order_id_close = stable_client_order_id(signal_identity, role="close")
    client_order_id_open = stable_client_order_id(signal_identity, role="open")
    client_order_id = client_order_id_open
    weight = float((execution_event or {}).get("risk_weight") or 0.0)
    base_notional = float((execution_event or {}).get("base_notional_usd") or realistic_base_notional_usd(normalized.get("symbol")))
    planned_notional = float(dec_notional(D(base_notional) * D(weight)))
    plan = {
        "mode": "live_order_enabled" if live_allowed else "dry_run_no_order",
        "live_execution_enabled": live_allowed,
        "execution_policy": execution_policy,
        "execution_policy_label": POLICY_LABELS.get(execution_policy, execution_policy),
        "shadow_policy": shadow_policy,
        "shadow_policy_label": POLICY_LABELS.get(shadow_policy, shadow_policy),
        "exchange": execution_cfg.get("exchange", "okx"),
        "route": execution_cfg.get("route", "okx_api"),
        "account": execution_cfg.get("account", "sandbox"),
        "symbol": normalized.get("symbol"),
        "inst_id": inst_id,
        "expected_leverage": asset_cfg.get("leverage"),
        "td_mode": execution_cfg.get("td_mode", "cross"),
        "timeframe": normalized.get("timeframe"),
        "tv_time": normalized.get("tv_time"),
        "signal_side": normalized.get("side"),
        "signal_price": normalized.get("tv_signal_price"),
        "execution_intent": {
            "policy": execution_policy,
            "decision": (execution_event or {}).get("decision"),
            "risk_weight": weight,
            "actions": (execution_event or {}).get("actions", []),
            "base_notional_usd": base_notional,
            "planned_notional_usd": planned_notional,
            "paper_execution_price": (execution_event or {}).get("okx_execution_price"),
            "alert_execution_diff_pct": (execution_event or {}).get("alert_execution_diff_pct"),
            "client_order_id": client_order_id,
            "client_order_id_open": client_order_id_open,
            "client_order_id_close": client_order_id_close,
        },
        "shadow_comparison": {
            "policy": shadow_policy,
            "decision": (shadow_event or {}).get("decision"),
            "risk_weight": (shadow_event or {}).get("risk_weight"),
            "actions": (shadow_event or {}).get("actions", []),
            "paper_execution_price": (shadow_event or {}).get("okx_execution_price"),
            "realized_pnl_usd": (shadow_event or {}).get("realized_pnl_usd"),
        },
        "okx_fill": {
            "status": "not_sent_shadow" if not live_allowed else "ready_to_send_when_executor_enabled",
            "order_id": None,
            "client_order_id": client_order_id,
            "avg_fill_price": None,
            "filled_size": None,
            "fee_usd": None,
            "slippage_pct": None,
            "position_after_order": None,
        },
        "block_reason": None if live_allowed else "OKX execution disabled: dry-run shadow only",
    }
    # The separate execution-plan.jsonl ledger was removed entirely (constant + sweep
    # entry): nothing consumed it. The authoritative submission outcome is recorded to
    # pipeline.jsonl (stage="execution"), which the dashboard reads.
    return plan


def build_strategy_execution_readiness(record: dict) -> dict:
    normalized = record.get("normalized") or {}
    strategy = record.get("strategy_config") or {}
    execution_cfg = CONFIG.get("execution", {}) or {}
    risk_cfg = CONFIG.get("risk", {}) or {}
    # execution_mode is operative: ``sandbox`` is True for demo/paper/shadow and
    # False ONLY for live. The resolved ``simulated_trading`` (= sandbox) and the
    # ``execution_mode`` flow into readiness so the ExecutionService gate can require
    # HERMX_LIVE_TRADING for live submissions and the adapter sandboxes accordingly.
    # The single per-strategy ``submit_orders`` flag is what arms submission -- the old
    # config-flag arming chain (execution.enabled/submit_orders, strategy_engine.
    # submit_orders, risk.allow_live_execution) is gone.
    execution_mode = str((strategy or {}).get("execution_mode") or "demo").lower()
    sandbox = (execution_mode != "live")  # demo/paper/shadow -> True; live -> False
    live_execution_enabled = bool((strategy or {}).get("submit_orders", False))
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
    margin_mode = strategy.get("margin_mode", execution_cfg.get("td_mode", "isolated"))
    instrument = strategy_instrument(strategy)
    plan = {
        "mode": "strategy_file_live_order_enabled" if live_allowed else "strategy_file_trial_no_order",
        "live_execution_enabled": live_allowed,
        "execution_mode": execution_mode,
        "simulated_trading": sandbox,
        "execution_policy": f"strategy_file:{normalized.get('strategy_id')}",
        "execution_policy_label": strategy.get("name") or normalized.get("strategy_id"),
        "exchange": execution_cfg.get("exchange", "okx"),
        "route": execution_cfg.get("route", "okx_api"),
        "account": execution_cfg.get("account", "sandbox"),
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


# Monotonic seq for the ORDER journal -- a SEPARATE counter from _journal_next_seq
# (the position journal). Derived once from the checkpoint floor + sealed-segment seqs
# + live tail at first use, then incremented in-process; reset to None on module
# (re)load. Survives rotation+restart because the floor folds in the checkpoint and the
# sealed filenames (encoded seq, no file read), mirroring _journal_next_seq.
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
    which fail-closes the money path (see execute_okx_if_enabled write-ahead)."""
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
    return {
        "symbol": readiness.get("symbol"),
        "side": readiness.get("signal_side"),
        "inst_id": readiness.get("inst_id") or (readiness.get("instrument") or {}).get("inst_id"),
        "planned_notional_usd": exec_intent.get("planned_notional_usd"),
        "policy": exec_intent.get("policy"),
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


def emit_operator_alert(kind: str, detail: "dict | None" = None, *, severity: str = "warning") -> dict:
    """Concrete operator alert transport (Task 6): durable ledger + log + optional
    webhook POST configured by HERMX_ALERT_WEBHOOK_URL."""
    record = {
        "ts": now_iso(),
        "kind": "operator",
        "alert": kind,
        "severity": severity,
        "detail": detail or {},
    }
    try:
        append_jsonl(ALERTS_LEDGER, record)
    except OSError as exc:
        logging.error("failed to write operator alert %s: %s", kind, exc)

    webhook_url = (os.environ.get("HERMX_ALERT_WEBHOOK_URL") or "").strip()
    if webhook_url:
        timeout_seconds = float(os.environ.get("HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS", "2") or "2")
        body = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds):
                pass
        except Exception as exc:
            logging.error("operator alert webhook failed kind=%s url=%s error=%s", kind, webhook_url, exc)

    log_fn = logging.error if severity.lower() in {"error", "critical"} else logging.warning
    log_fn("%s %s", kind, json.dumps(detail or {}, ensure_ascii=False))
    return record


def emit_auth_failure_alert(path: str, client_ip: "str | None") -> dict:
    return emit_operator_alert(
        ALERT_AUTH_FAILURE,
        {"path": path, "client_ip": client_ip},
        severity="error",
    )


def maybe_emit_queue_saturation_alert(queue_depth: int) -> bool:
    if QUEUE_SATURATION_ALERT_DEPTH <= 0 or queue_depth < QUEUE_SATURATION_ALERT_DEPTH:
        return False
    emit_operator_alert(
        ALERT_QUEUE_SATURATION,
        {"queue_depth": queue_depth, "threshold": QUEUE_SATURATION_ALERT_DEPTH},
        severity="error",
    )
    return True


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


def _expected_positions_from_state(state: dict) -> dict:
    """PURE: derive the locally-EXPECTED position per symbol from paper/journal state.

    Returns ``symbol -> {direction: long|short|mixed, policies: [account:policy, ...]}``
    for every symbol any paper policy bucket currently holds. Flat symbols are absent.
    'mixed' marks symbols held both long and short across policies (sign-incomparable)."""
    out: dict = {}
    for account_key in ("policies", "realistic_policies", "compound_policies"):
        for policy_key, ps in (state.get(account_key) or {}).items():
            for symbol, pos in ((ps or {}).get("symbols") or {}).items():
                if not isinstance(pos, dict):
                    continue
                direction = pos.get("side") or pos.get("direction") or "long"
                cur = out.setdefault(symbol, {"direction": direction, "policies": []})
                if cur["direction"] != direction:
                    cur["direction"] = "mixed"
                cur["policies"].append(f"{account_key}:{policy_key}")
    return out


def _symbol_inst_map_from_orders(records: list) -> dict:
    """PURE: build symbol -> inst_id from order-journal intents (latest wins). The
    paper state keys positions by symbol; the exchange keys them by instId, so this
    bridges the two for the startup position comparison."""
    out: dict = {}
    for rec in records:
        intent = rec.get("intent") or {}
        symbol, inst = intent.get("symbol"), intent.get("inst_id")
        if symbol and inst:
            out[symbol] = inst
    return out


def reconcile_positions_once(executor, expected: dict, symbol_to_inst: dict) -> list:
    """Compare local expected positions against OKX /account/positions (:210/:215).
    Emits a RECONCILE_MISMATCH alert per divergent symbol and returns the list of
    mismatch details. OBSERVE-ONLY: detects + alerts, never trades."""
    by_inst: dict = {}
    for p in executor.get_positions() or []:
        inst = p.get("inst_id")
        if inst is not None:
            by_inst[inst] = _reconcile_float(p.get("pos"), 0.0)

    inst_to_symbol = {inst: sym for sym, inst in symbol_to_inst.items()}
    symbols = set(expected) | {inst_to_symbol[i] for i in by_inst if i in inst_to_symbol}

    mismatches: list = []
    for symbol in sorted(symbols):
        inst = symbol_to_inst.get(symbol)
        exch_pos = by_inst.get(inst, 0.0) if inst else 0.0
        exch_dir = "long" if exch_pos > 0 else "short" if exch_pos < 0 else "flat"
        local = expected.get(symbol)
        local_dir = local["direction"] if local else "flat"
        local_flat, exch_flat = local_dir == "flat", exch_dir == "flat"
        divergent = (local_flat != exch_flat) or (
            not local_flat and not exch_flat and local_dir != "mixed" and local_dir != exch_dir
        )
        if divergent:
            detail = {
                "symbol": symbol,
                "inst_id": inst,
                "local_direction": local_dir,
                "exchange_direction": exch_dir,
                "exchange_pos": exch_pos,
                "policies": (local or {}).get("policies", []),
            }
            emit_reconcile_alert(RECONCILE_ALERT_MISMATCH, detail)
            mismatches.append(detail)
    return mismatches


def _effective_execution_config() -> dict:
    """The execution config the write path actually resolves: CONFIG with an
    optional HERMX_EXEC_BACKEND override applied to execution.exchange.

    Submit (ExecutionService via _execution_config) and reconciliation
    (_reconciliation_executor) BOTH resolve through this same rule, so the backend
    used to submit an order can never diverge from the backend used to reconcile
    it -- both default to CCXT, and an explicit HERMX_EXEC_BACKEND is honored by
    both paths identically."""
    cfg = dict(CONFIG or {})
    execution_cfg = dict(cfg.get("execution") or {})
    backend = (os.environ.get("HERMX_EXEC_BACKEND") or "").strip()
    if backend:
        execution_cfg["exchange"] = backend
    cfg["execution"] = execution_cfg
    return cfg


def _reconciliation_executor():
    """Build the active venue's read-only query executor, or None if unavailable.
    Constructed lazily so a missing factory / bad config simply disables
    reconciliation rather than crashing the receiver (fail closed to observe-only).

    Uses _effective_execution_config() -- the SAME backend resolution the submit
    path uses -- so reconcile always queries the venue the order was submitted to."""
    if ExecutorFactory is None:
        return None
    try:
        return ExecutorFactory.create(_effective_execution_config(), ROOT)
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("reconciliation executor unavailable: %s", exc)
        return None


def reconcile_startup(executor=None) -> dict:
    """One-time startup reconcile bootstrap (:215, acceptance :236). OBSERVE-ONLY:

      (a) reconcile every still-open order (load_open_orders) against the exchange and,
          where the venue reports a terminal outcome and the journal state legally
          allows it, write the authoritative terminal transition;
      (b) compare local expected positions (paper/journal) vs OKX positions for traded
          symbols and emit RECONCILE_MISMATCH on divergence.

    Sets RECONCILE_STARTUP_COMPLETE + RECONCILE_STARTUP_AT for FUTURE enforcement; it
    does NOT auto-trade and does NOT hard-block submission in this task. Returns a
    summary dict (also useful for tests)."""
    global RECONCILE_STARTUP_COMPLETE, RECONCILE_STARTUP_AT
    if executor is None:
        executor = _reconciliation_executor()
    summary = {"open_orders": [], "position_mismatches": [], "executor_available": executor is not None, "errors": []}

    if executor is not None:
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
            try:
                outcome = reconcile_order_once(executor, lookup)
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

        try:
            state = load_paper_state()
            expected = _expected_positions_from_state(state)
            sym_map = _symbol_inst_map_from_orders(load_open_orders())
            summary["position_mismatches"] = reconcile_positions_once(executor, expected, sym_map)
        except Exception as exc:  # pragma: no cover - tolerant
            summary["errors"].append(f"reconcile_positions: {exc}")

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

    Defaults ON (``HERMX_UNKNOWN_RESOLVER_ENABLED`` unset => enabled); a falsey value
    disables the daemon thread. Like the other two paths it only updates the order
    journal / emits alerts and never submits, cancels, or auto-trades.
    """
    return _reconcile_flag_enabled("HERMX_UNKNOWN_RESOLVER_ENABLED", default=True)


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
    if executor is None:
        executor = _reconciliation_executor()
    summary = {
        "checked": 0,
        "resolved": 0,
        "pending": 0,
        "expired": 0,
        "never_submitted": 0,
        "paused_symbols": [],
        "errors": [],
        "executor_available": executor is not None,
    }
    if executor is None:
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

        if cur_state == ORDER_STATE_PLANNED:
            _resolve_planned_orphan(executor, rec, lookup, age_seconds, summary)
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
            outcome = reconcile_order_with_backoff(executor, lookup)
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


def _execute_okx_via_service(record: dict) -> dict:
    service = ExecutionService(
        config=CONFIG,
        root=ROOT,
        executor_factory=ExecutorFactory,
        submit_timeout_seconds=HERMX_SUBMIT_TIMEOUT_SECONDS,
        hooks={
            "append_jsonl": _record_execution_outcome,
            "execution_ledger": PIPELINE_LEDGER,
            "webhook_auth_config_healthy": webhook_auth_config_healthy,
            "watchdog_submission_state": _watchdog_submission_state,
            "live_trading_enabled": live_trading_enabled,
            "symbol_pause_info": symbol_pause_info,
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


def execute_okx_if_enabled(record: dict) -> dict:
    """Authoritative submission entry point: route through ExecutionService (CCXT)."""
    return _execute_okx_authoritative(record)


def _execute_okx_authoritative(record: dict) -> dict:
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
    return _execute_okx_via_service(record)


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


def execute_okx_with_advisor(record: dict) -> dict:
    """Single wrapper used by the execution paths: consult the advisor, honor a
    veto if granted, otherwise delegate to the authoritative submission path. With
    the advisor disabled (default) this is exactly ``execute_okx_if_enabled``."""
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
    return execute_okx_if_enabled(record)


def primary_decision_from_policies(policies: dict) -> dict:
    if PRIMARY_POLICY_SELECTED and PRIMARY_POLICY in policies:
        return policies[PRIMARY_POLICY]
    # Compatibility fallback for the legacy top-level decision field. The actual
    # dashboard and ledger keep every policy side by side while primary is unset.
    return policies.get("duo_raw") or policies.get("v52_fast_1h") or policies.get("v6_regime_duo") or {}

def build_record(payload: dict, received_at_override: str | None = None) -> tuple[int, dict]:
    normalized = normalize(payload)
    if normalized["side"] not in ALLOWED_SIDES:
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
            "mode": "strategy_file_trial" if strategy_config else "vps_parallel_shadow",
            "config_snapshot": {"mode": CONFIG.get("mode"), "primary_policy": PRIMARY_POLICY, "base_notional_usd": PAPER_BASE_NOTIONAL_USD, "fees": CONFIG.get("fees"), "funding": CONFIG.get("funding"), "asset": CONFIG.get("assets", {}).get(normalized.get("symbol"))},
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
                "mode": CONFIG.get("mode"),
                "strategy_engine": STRATEGY_ENGINE,
                "strategy": {
                    "strategy_id": strategy_config.get("strategy_id"),
                    "timeframe": strategy_config.get("timeframe"),
                    "budget_usd": strategy_budget_usd(strategy_config),
                    "leverage": strategy_config.get("leverage"),
                    "margin_mode": strategy_config.get("margin_mode"),
                    "submit_orders": strategy_config.get("submit_orders", strategy_config.get("okx_submit_orders")),
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
                "funding_enabled": PAPER_FUNDING_ENABLED,
                "funding_rate": funding_rate_from_payload(payload),
            },
            "strategy_config": strategy_config,
            "strategy_decision": decision,
            "decision": decision,
            "policies": {},
            "paper_events": [],
        }
        record["execution_readiness"] = build_strategy_execution_readiness(record)
        record["okx_execution"] = execute_okx_with_advisor(record)
        record_raw_webhook("webhook", {"received_at": record["received_at"], "payload": payload, "normalized": normalized, "strategy_id": normalized.get("strategy_id")})
        record_pipeline_event("strategy_match", normalized.get("signal_id"), record)
        record_pipeline_event("decision", normalized.get("signal_id"), record)
        LATEST_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return 200, record

    health = latest_tab_health()
    mxc_values = None
    mxc_error = None
    if read_indicator_values is not None:
        mxc_values, mxc_error = read_indicator_values_stable(normalized["symbol"], normalized["timeframe"])
        if not mxc_core_ok(mxc_values):
            cached = cached_health_values(health, normalized["symbol"], normalized["timeframe"])
            if cached:
                mxc_values = cached
                mxc_error = f"live_read_incomplete_used_health_cache:{mxc_error}"
            else:
                trigger_health_repair("primary_live_read_incomplete", normalized["symbol"], normalized["timeframe"])

    mtf_values = {}
    mtf_errors = {}
    if read_indicator_values is not None:
        mtf_values, mtf_errors = read_mtf_values(normalized["symbol"])
        for tf in ACTIVE_MTF_TIMEFRAMES:
            if not mxc_core_ok(mtf_values.get(tf)):
                cached = cached_health_values(health, normalized["symbol"], tf)
                if cached:
                    mtf_values[tf] = cached
                    mtf_errors[tf] = f"live_read_incomplete_used_health_cache:{mtf_errors.get(tf)}"

    health_gate = health_gate_for(normalized["symbol"], health, mxc_values, mtf_values)
    policies = build_policies(normalized, mxc_values, mtf_values)
    policies = apply_health_gate(policies, health_gate)
    decision = primary_decision_from_policies(policies)
    record = {
        "received_at": received_at,
        "mode": "vps_parallel_shadow",
        "config_snapshot": {"mode": CONFIG.get("mode"), "primary_policy": PRIMARY_POLICY, "base_notional_usd": PAPER_BASE_NOTIONAL_USD, "fees": CONFIG.get("fees"), "funding": CONFIG.get("funding"), "asset": CONFIG.get("assets", {}).get(normalized.get("symbol"))},
        "ok": True,
        "payload": payload,
        "normalized": normalized,
        "duplicate": False,
        "dedupe": dedupe,
        "latency": latency,
        "market_context": {"chart_type": normalized.get("chart_type"), "okx_mark_price": normalized.get("okx_mark_price"), "okx_last_price": normalized.get("okx_last_price"), "funding_enabled": PAPER_FUNDING_ENABLED, "funding_rate": funding_rate_from_payload(payload)},
        "mxc_values": mxc_values,
        "mxc_error": mxc_error,
        "mtf_values": mtf_values,
        "mtf_errors": mtf_errors,
        "health_gate": health_gate,
        "decision": decision,
        "policies": policies,
    }
    paper_events = apply_paper_trading(record)
    record["paper_events"] = paper_events
    record["execution_readiness"] = build_okx_execution_readiness(record, paper_events)
    record["okx_execution"] = execute_okx_if_enabled(record)
    record_raw_webhook("webhook", {"received_at": record["received_at"], "payload": payload, "normalized": normalized})
    record_pipeline_event("decision", normalized.get("signal_id"), record)
    LATEST_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
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


class Handler(BaseHTTPRequestHandler):
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
            if LATEST_FILE.exists():
                self._send(200, json.loads(LATEST_FILE.read_text(encoding="utf-8")))
            else:
                self._send(404, {"ok": False, "error": "no_latest_yet"})
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
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
            ok, status, reason = authenticate_webhook_request(self, raw_body)
            if not ok:
                self._send(status, {"ok": False, "error": reason})
                return
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            self._send(400, {"ok": False, "error": "invalid_json", "detail": str(exc)})
            return
        intake_received_at = now_iso()
        record_raw_webhook("intake", {"received_at": intake_received_at, "payload": payload, "path": parsed.path})
        try:
            PROCESS_QUEUE.put_nowait(_queue_work_item(payload, intake_received_at))
        except queue.Full:
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
        HERMX_WATCHDOG_ENABLED,
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
    if unknown_resolver_enabled():
        threading.Thread(target=unknown_resolver_loop, daemon=True, name="unknown-resolver").start()
    pool_size = max(1, HERMX_WORKER_POOL_SIZE)
    _WORKER_NAMES.clear()
    for i in range(pool_size):
        worker_name = f"shadow-policy-worker-{i + 1}"
        _WORKER_NAMES.append(worker_name)
        _set_worker_heartbeat(worker_name)
        threading.Thread(target=worker_loop, args=(worker_name,), daemon=True, name=worker_name).start()
    if HERMX_WATCHDOG_ENABLED:
        threading.Thread(target=liveness_watchdog_loop, daemon=True, name="watchdog").start()
    server = HTTPServer((HERMX_BIND_HOST, PORT), Handler)
    logging.info("MXC VPS shadow receiver listening on %s:%s", HERMX_BIND_HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
