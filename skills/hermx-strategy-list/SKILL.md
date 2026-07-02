---
name: hermx-strategy-list
description: Use when the operator asks which HermX strategies exist and what mode each is in — demo/live/paused, symbol, timeframe. Read-only. Reads strategies/*.json merged with control-state.json strategy_overrides and symbol_pauses. Renders a table (id, name, symbol, tf, file_mode, effective_mode, paused). Never edits a strategy or changes a mode.
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, hermx, strategies, read-only, operations]
    related_skills: [hermx-control, hermx-status, hermx-positions, hermx-trace]
    config:
      - key: hermx.strategies_dir
        description: "Directory of strategy files"
        default: "strategies"
      - key: hermx.control_state
        description: "Per-strategy mode/pause overrides"
        default: "control-state.json"
---

# /strategy-list — HermX strategies & effective modes

Read-only. Lists `strategies/*.json` and folds in `control-state.json`
(`strategy_overrides`, `symbol_pauses`) to show each strategy's **effective** mode.

Field shapes and the `effective_mode` resolution live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- "what strategies do we have?", "which are live vs demo?", "is anything paused?"
- Do NOT use to change a mode or pause a strategy (Phase-2 mutating command).

## effective_mode (override > pause > file)
1. `strategy_overrides[sid].mode` present → that mode.
2. else strategy `submit_orders` explicitly `false` → `pause`.
3. else strategy `execution_mode` (default `demo`).

`paused` is true when `effective_mode == "pause"` **or** the strategy's symbol is
paused in `control-state.json` `symbol_pauses`.

## Procedure
```bash
rtk python3 - <<'PY'
import sys; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
rows = h.list_strategies("strategies", str(h.CONTROL_STATE_PATH))
hdr = ["ID","NAME","SYMBOL","TF","FILE_MODE","EFF_MODE","PAUSED"]
tbl = [hdr] + [[r["id"], r["name"], r["symbol"], r["timeframe"],
                r["file_mode"], r["effective_mode"], str(r["paused"])] for r in rows]
w = [max(len(str(row[i])) for row in tbl) for i in range(len(hdr))]
for row in tbl:
    print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(row)))
PY
```

## Reporting
- Show `file_mode` and `effective_mode` distinctly — an override that differs from the
  file is the interesting signal (e.g. file `demo` but override `pause`).
- Flag anything `live` explicitly; this host is demo/paper by default.
- Do not restate `budget_usd`/`leverage` as an order size — sizing is derived by the
  execution layer, never set here.

## Verification checklist
- [ ] All `strategies/*.json` listed; count matches `ls strategies/*.json`.
- [ ] A control-state override changes `effective_mode` but leaves `file_mode` intact.
- [ ] A `symbol_pauses` entry flips `paused` to `True` for the matching symbol.
- [ ] A corrupt/missing strategy file surfaces as UNKNOWN fields, not a crash.
- [ ] No file written; no mode changed.
