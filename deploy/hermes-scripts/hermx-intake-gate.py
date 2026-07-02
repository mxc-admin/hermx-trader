#!/usr/bin/env python3
"""HermX intake-recency gate (Hermes cron, every 30m).

Closes the biggest silent observability hole: TradingView stops sending but the
receiver stays up, so every other monitor keeps reporting "healthy". This gate reads
``logs/raw-webhooks.jsonl`` — the durable intake WAL — for ``phase=="intake"`` rows,
takes the newest ``received_at``, and wakes ONCE when it is older than the max-age
threshold (default 3 days). Absence detection: a synthetic condition emitted when the
expected rows *stop* appearing, not when a row appears.

Fail-open by construction: a missing/empty WAL, no parseable intake timestamp, or a
recent-enough intake all yield ``[]`` (no alert). Per-line JSON corruption is skipped
by ``_iter_jsonl``. The only write is the intake sidecar (suppression window 3600s).

READ-ONLY: never mutates HermX state.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402

# Wake when the newest intake is older than this many seconds (default 3 days).
MAX_AGE_SECONDS = float(os.environ.get("HERMX_INTAKE_MAX_AGE_SECONDS", str(3 * 24 * 3600)))

# TODO(per-strategy frequency): evolution path — INTENTIONALLY NOT IMPLEMENTED YET.
# Prerequisites (all must hold before this is built):
#   1. >=5 signals per strategy of history (enough for a meaningful baseline).
#      Today only BTC qualifies (~222 intake rows); ETH/SOL/XRP have ~1 signal
#      each, so per-strategy baselines are un-trainable right now.
#   2. A sidecar SCHEMA EXTENSION to persist a rolling per-strategy baseline.
#      This is a shared-lib change (hermx_gate_lib sidecar format) affecting all
#      gates — it must be designed and validated before implementation.
#   3. Answers to the open design questions below (paused strategy? WAL rotation?
#      a brand-new strategy with no history?).
# Until then the global 3-day check in intake_conditions() is the ONLY active
# condition.
#
# Planned evolution once the prerequisites are met — extend this gate to ALSO
# check per-strategy cadence:
#   - Read logs/raw-webhooks.jsonl for phase=="intake" rows, group by
#     payload.strategy_id.
#   - For each strategy, compute the average inter-signal interval from the
#     historical received_at timestamps.
#   - If a strategy's last signal is older than avg_interval * 1.25, emit a
#     `frequency:silent:{strategy_id}` condition.
#   - Persist a rolling per-strategy baseline in the signal_late sidecar (the
#     gate's only writable state) — requires the schema extension in (2).
#   - The baseline must be trainable purely from the existing WAL — no external
#     state store.
# Open design questions to resolve first: a paused strategy (don't page on an
# intentional stop), WAL rotation (baseline must survive segment sealing), and a
# newly-added strategy (no history → must fail-open, never page).


def intake_conditions(ops, repo, now_epoch):
    """One ``frequency:zero_intake:global`` condition when the newest intake row is
    older than ``MAX_AGE_SECONDS``; ``[]`` for every fail-open case (no WAL, no
    intake row, no parseable timestamp, or a recent-enough intake)."""
    path = os.path.join(repo, "logs", "raw-webhooks.jsonl")
    latest = None
    for row in ops._iter_jsonl(path):
        if not isinstance(row, dict) or row.get("phase") != "intake":
            continue
        epoch = g.row_epoch(row.get("received_at"))
        if epoch is None:
            continue
        if latest is None or epoch > latest:
            latest = epoch
    if latest is None:
        return []  # no parseable intake ever seen → fail-open (don't page)
    age = now_epoch - latest
    if age <= MAX_AGE_SECONDS:
        return []
    return [{
        "fingerprint": "frequency:zero_intake:global",
        "severity": "error",
        "category": "frequency",
        "title": "no TradingView intake",
        "detail": {"last_intake_age_seconds": round(age),
                   "max_age_seconds": MAX_AGE_SECONDS},
    }]


def main():
    ops = g.import_hermx_ops()
    now = time.time()
    conds = intake_conditions(ops, g.repo_root(), now)
    g.run_gate("signal_late", conds, now)


if __name__ == "__main__":
    main()
