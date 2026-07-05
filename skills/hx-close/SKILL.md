---
name: hx-close
description: "Use when the operator wants to CLOSE (flatten) one open HermX position by symbol or strategy. Mutating, reduce-only. Confirms the position exists via /api first, previews side+size, and on explicit 'yes' POSTs /api/close on the receiver. Never sends a size; never routes via /webhook. A failed/stale read is UNKNOWN — it refuses to close rather than assume flat."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. The /api/close endpoint fails closed without a matching token."
    required_for: "Authenticated receiver /api/close write"
metadata:
  hermes:
    tags: [trading, hermx, positions, close, mutating, operations]
    related_skills: [hx-positions, hermx-control, hx-strategy-mode, hx-emergency-stop]
    config:
      - key: hermx.dashboard_base
        description: "HermX dashboard base URL (loopback)"
        default: "http://127.0.0.1:8098"
      - key: hermx.receiver_base
        description: "HermX receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
---

# /hx-close — flatten one HermX position (reduce-only)

**Mutating, reduce-only.** `/hx-close <symbol|strategy>`

Closes ONE open position via `POST {receiver}/api/close`. The receiver derives a
reduce-only close from the live position — this skill **never sends a size** and
**never routes via `/webhook`** (the only order-creation path). Endpoint shapes, auth,
and the UNKNOWN-never-flat rule live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## The one rule that matters: UNKNOWN, never "flat"
If the `/api` read fails or the executor is stale/degraded, the position is **UNKNOWN**
— **refuse to close** (we cannot confirm what we would be closing). Only a healthy,
non-degraded read may say "nothing to close" (genuinely FLAT).

## Procedure

### 1. Resolve + confirm position exists (dry-run, no write)
```bash
rtk python3 - "$1" <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
arg = sys.argv[1]
res = h.resolve_strategy(arg, "strategies")
if not res["resolved"]:
    print("UNRESOLVED:", res["reason"], "| candidates:", res["candidates"]); sys.exit(1)
sid = res["resolved"]
row = {r["id"]: r for r in h.list_strategies("strategies")}[sid]
symbol = row["symbol"]
st = h.read_state(secret=os.environ.get("HERMX_SECRET"))
pos = h.read_position_for_symbol(st, symbol)
print(f"resolved: {sid} | symbol: {symbol} | freshness: {st['freshness']}")
if pos["status"] == h.UNKNOWN:
    print("position: UNKNOWN (read failed / stale) — REFUSING to close."); sys.exit(2)
if pos["status"] == "FLAT":
    print("nothing to close (FLAT)."); sys.exit(3)
print(f"Will close {symbol} ({pos['side']} {pos['size']}) via strategy {sid} "
      f"— reduce-only. Confirm? [yes]")
PY
```
- Exit 2 (UNKNOWN) or 3 (FLAT) → stop; do not proceed to step 2.
- Note the `(symbol, strategy_id)` pair; the confirm line shows side+size for review.

### 2. Close (only after explicit `yes`)
```bash
rtk python3 - "$1" <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
arg = sys.argv[1]
sid = h.resolve_strategy(arg, "strategies")["resolved"]
symbol = {r["id"]: r for r in h.list_strategies("strategies")}[sid]["symbol"]
r = h.post_close(h.RECEIVER_BASE, os.environ.get("HERMX_SECRET"),
                 symbol, sid, operator="operator", reason="manual /hx-close")
print("outcome:", r["outcome"], "| reason:", r["reason"], "| cl_ord_id:", r["cl_ord_id"])
# Re-read to see the resulting position (may still be settling).
st = h.read_state(secret=os.environ.get("HERMX_SECRET"))
print("post-close:", h.read_position_for_symbol(st, symbol)["status"])
PY
```
Fill `operator`/`reason` from the invocation context.

## Reduce-only invariants
- Body is exactly `{"symbol", "strategy_id", "operator", "reason"}` — **no size field**.
- Uses `{receiver}/api/close`, never `/webhook`.
- The receiver's close path bypasses only the kill switch + symbol pause (a close
  reduces risk); it does not open or increase exposure.

## Outcomes
- `submitted` — close order sent; report the `cl_ord_id`.
- `not_submitted` — a control gate blocked it (e.g. `symbol_paused`, duplicate
  `cl_ord_id`) or the server rejected it (`http_401` unauthorized, `http_404` unknown
  strategy). Report the reason; nothing was sent.
- `UNKNOWN` — transport failure or 5xx: the order may or may not have been sent. Do
  **not** claim it closed or that we are flat; re-read `/api` before any retry.

## Required log (every mutation)
Record: time, operator, reason, symbol + `strategy_id`, position before (side/size),
outcome + `cl_ord_id`, and position after the confirm re-read.

## Reporting
- Never say "flat" off a UNKNOWN read.
- Never state or imply an order size — the close is reduce-only and server-sized.
- Report `submitted`/`not_submitted`/`UNKNOWN` verbatim with the reason.

## Verification checklist
- [ ] Executor down / stale (`positions == UNKNOWN`) → refuses to close (exit UNKNOWN).
- [ ] Healthy + no open position → "nothing to close" (FLAT), no POST.
- [ ] Open position → preview shows `<side> <size>` and asks to confirm before any POST.
- [ ] Confirmed close body contains no size field and hits `/api/close`, not `/webhook`.
- [ ] 401 (missing/wrong secret) → `not_submitted`/unauthorized, never "closed".
- [ ] Transport failure / 5xx → `UNKNOWN`; skill re-reads, never asserts flat.
- [ ] Result reports `cl_ord_id` on `submitted`.
