#!/usr/bin/env python3
"""HermX reconcile-lag pre-check gate (Hermes cron, every 15m, non-LLM).

Surfaces reconcile lag: ``now - max(recorded_at_ms)`` across the durable ledger
(``closed-trades.jsonl``, schema v3+, P1-1). When the newest local observation time
falls more than ``MAX_RECONCILE_LAG_MS`` behind wall-clock, the ledger feed has
stalled and an operator should look. The ``stuck_unknown_count`` and ``ageout_fires``
metrics are reserved (see TODOs) until the ``/api`` fields and a counter exist.

Fail-open like ``hermx-ledger-reconcile.py``: a missing ledger or unreachable
dashboard yields no wake (the health watchdog owns reachability), never a false
trading-state signal. READ-ONLY against HermX state; the only write is the gate's own
sidecar. Non-LLM job: it never wakes an agent, so no ``--provider``/``--model`` pin.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402

# Lag beyond this (ms) wakes the operator. Default 20 minutes (> the 15m cadence, so a
# single missed run does not trip it). Overridable for tuning without a code change.
MAX_RECONCILE_LAG_MS = int(
    os.environ.get("HERMX_MAX_RECONCILE_LAG_MS", str(20 * 60 * 1000))
)


def _ledger_path(repo: str) -> str:
    return os.path.join(repo, "closed-trades.jsonl")


def max_recorded_at_ms(path: str):
    """Max ``recorded_at_ms`` across ledger rows (schema v3+), or ``None``.

    Corrupt-tolerant (skips torn/garbage lines) and fail-open on a missing file. v1/v2
    rows have no ``recorded_at_ms`` and are ignored (back-compat), so a ledger with no
    v3 rows yet returns ``None`` -> the caller skips the metric.
    """
    best = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts = row.get("recorded_at_ms")
                if ts is None:
                    continue
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    continue
                if best is None or ts > best:
                    best = ts
    except OSError:
        return None
    return best


def lag_conditions(repo: str, now_ms: int) -> list:
    """One ``reconcile:lag`` condition when the ledger's newest recorded_at_ms is more
    than ``MAX_RECONCILE_LAG_MS`` behind ``now_ms``; empty otherwise (and when no v3
    row exists yet -> nothing to measure)."""
    best = max_recorded_at_ms(_ledger_path(repo))
    if best is None:
        return []
    lag = now_ms - best
    if lag <= MAX_RECONCILE_LAG_MS:
        return []
    return [{
        "fingerprint": "reconcile:lag",
        "severity": "warning",
        "category": "reconcile",
        "title": "reconcile lag exceeds threshold",
        "detail": {
            "reconcile_lag_ms": lag,
            "max_recorded_at_ms": best,
            "threshold_ms": MAX_RECONCILE_LAG_MS,
        },
    }]


def run(conds, now_epoch):
    sp = g.state_path("reconcile")
    state = g.load_state(sp)
    fresh, new_state = g.evaluate(conds, state, g.WINDOW["reconcile"], now_epoch)
    for c in fresh:
        print(c["title"])
    if fresh:
        g.save_state(sp, new_state)
    return fresh


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-agent", action="store_true", help="non-LLM job marker")
    parser.parse_known_args()

    now = time.time()
    now_ms = int(now * 1000)
    conds = lag_conditions(g.repo_root(), now_ms)
    # TODO(P1-2): stuck_unknown_count — consume api['reconcile_health']['stuck_unknown']
    #   once the /api field carries the journal UNKNOWN census.
    # TODO(P1-2): ageout_fires — needs a P0-1 alert counter over the window.
    run(conds, now)


if __name__ == "__main__":
    main()
