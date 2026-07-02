---
name: hermx-trace
description: "Use when the operator wants to trace one HermX signal end-to-end — intake → dedupe → pipeline stages → execution outcome. Read-only. Joins logs/raw-webhooks.jsonl, signals.jsonl, pipeline.jsonl, executions.jsonl on received_at. Accepts a received_at or a symbol (shows recent matches, defaults to latest). Never re-derives ids for time-less payloads. Never relays or executes."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, hermx, trace, diagnostics, read-only]
    related_skills: [hermx-control, hermx-status, hermx-positions, signal-memory]
    config:
      - key: hermx.logs_dir
        description: "HermX JSONL log directory"
        default: "logs"
---

# /hx-trace — follow one signal through the pipeline

Read-only diagnostic. Joins the four log stages on the **`received_at`** key:

`raw-webhooks.jsonl` (intake WAL) → `signals.jsonl` (dedupe ledger) →
`pipeline.jsonl` (per-stage) → `executions.jsonl` (outcome).

Join key, WAL semantics, and time-less handling live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- "what happened to that BTC alert?", "why didn't the 14:00 signal fill?"
- "trace received_at 2026-06-30T16:07:05.020168+00:00"

## Input handling
- **`received_at` (microsecond ISO):** correlate directly.
- **Symbol (e.g. `BTCUSDT`):** list recent intake rows for that symbol (newest first)
  and default the trace to the latest; let the operator pick another `received_at`.

## Time-less payloads (correctness invariant)
A payload with no `tv_time` gets a wall-clock-derived, **non-deterministic**
`signal_id` (`normalize()` falls back to `now_iso()`). **Never re-derive or match on
that id** — correlation stays keyed on the stable `received_at`. The helper flags such
rows `time_less: true`; surface that flag rather than trusting/deriving an id.

## Procedure

Symbol → recent matches, then trace the latest:
```bash
rtk python3 - <<'PY'
import sys; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
arg = "BTCUSDT"   # or a received_at value
if "T" in arg and ":" in arg:            # looks like received_at
    key = arg
else:
    hits = h.find_traces_by_symbol(arg, "logs", limit=10)
    for x in hits:
        print(x["received_at"], x["symbol"], x["side"], "time_less" if x["time_less"] else "")
    key = hits[0]["received_at"] if hits else None
if key:
    t = h.correlate_trace(key, "logs")
    print("--- trace", key, "time_less=", t["time_less"], "---")
    print("intake  :", "yes" if t["raw"] else "MISSING")
    print("dedupe  :", (t["signal"] or {}).get("signal_id", "MISSING"))
    print("pipeline:", [p.get("stage") for p in t["pipeline"]] or "none")
    print("exec    :", [ (e.get("okx_execution") or {}).get("mode") for e in t["executions"]] or "none")
PY
```

## Reporting
- Show the stage chain and where it stopped (e.g. intake present but no execution ⇒
  queued/blocked, not filled).
- `signals.jsonl` is written **after** dequeue: an intake row with no dedupe row means
  "received but not yet dequeued" — a real state, not a bug.
- Read `pipeline.jsonl` `okx_execution.reason`/`gate` to explain a `not_submitted`
  (e.g. `duplicate_cl_ord_id` / idempotency).
- Never re-submit or relay to fix a stuck trace; report findings only.

## Verification checklist
- [ ] Trace by `received_at` joins all four stages on the same key.
- [ ] Trace by symbol lists recent matches and defaults to the latest.
- [ ] A time-less intake row is flagged `time_less` and its id is NOT re-derived.
- [ ] Intake-without-dedupe reported as "queued/not dequeued", not an error.
- [ ] Corrupt log lines are skipped, not fatal; no log file is written.
