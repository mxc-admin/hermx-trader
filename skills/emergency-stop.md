# Skill: Emergency Stop

Use this when execution must be paused immediately. The system has two operative
controls — a per-strategy `execution_mode` (`demo` or `live`) and the global
`HERMX_LIVE_TRADING` switch — so a stop can be global, per-strategy, or per-symbol.

## Stop Levels

### Level 0: Global live kill switch (fastest, no redeploy)

A single environment variable, `HERMX_LIVE_TRADING`, gates **all** real-money
(`execution_mode: "live"`) submission. It is fail-closed and a positive enable
flag — live trading is permitted ONLY when it is explicitly truthy:

- **Unset** — live trading DISABLED (safe default).
- **Falsey** (`false`, `0`, `no`, `""`/blank, case-insensitive) — DISABLED.
- **Truthy** (`true`, `1`, `yes`) — live trading enabled.

To stop all live submission instantly, set it false (or unset it) and restart the
receiver:

```
HERMX_LIVE_TRADING=false
```

```
python src/webhook_receiver.py   # or the start script
```

A `live` strategy then returns mode `not_submitted` (`reason: "live_trading_disabled"`)
and writes that record to `logs/executions.jsonl` — no order is sent to the real
account. (`live_trading_enabled()` in `src/hermx_shared.py` is the single source of
truth, read by `ExecutionService.execute()` and mirrored in the dashboard `/health`
`arm` block as `kill_switch_engaged`.)

> Note: `demo` strategies route to the exchange **sandbox** and do not consult this
> switch. To stop one of those too, use Level 1 (per-strategy) below.

### Level 1: Stop a single strategy

Switch the strategy's `execution_mode` from `"live"` to `"demo"` in its
`strategies/<id>.json` and restart the receiver. The strategy still validates and
ledgers, but routes to the sandbox instead of the real account.

To fully stop a strategy from submitting (even to sandbox), remove its strategy file
from `strategies/` or use the per-symbol pause in `control-state.json`.

### Level 2: Pause a single symbol

Use the per-symbol pause registry in `control-state.json` (`symbol_pauses`) — unchanged.
A paused symbol returns `not_submitted` (`reason: "symbol_paused"`) regardless of mode.

### Level 3: Stop webhook processing

Stop the receiver service. The dashboard may remain online for read-only status.

### Level 4: Flatten exchange

- close open positions on the venue,
- verify flat,
- set `HERMX_LIVE_TRADING=false` and/or switch each strategy's `execution_mode` to `"demo"` to keep
  it flat.

## Commands

All emergency commands share three invariants: **dry-run preview first**, **explicit
`yes` before any mutation**, and **UNKNOWN never renders as flat**. They reuse the
audited wrappers in [`hermx-ops/lib/hermx_ops.py`](hermx-ops/lib/hermx_ops.py); the
contract is [`hermx-ops/references/api-contract.md`](hermx-ops/references/api-contract.md).
None of them set an order size, and none route via `/webhook`.

### `kill` — global live kill (cannot be done over HTTP)

There is **no HTTP endpoint** for the kill switch — it is an env-gated process control.
Output these exact operator steps, then confirm:

```
# 1. On the HermX host, set the kill switch false (or unset it):
HERMX_LIVE_TRADING=false

# 2. Restart the receiver so it re-reads the env:
rtk python src/webhook_receiver.py   # or the systemd start script
```

Confirm afterward via `/health` — `arm.kill_switch_engaged` must be `true`:
```bash
rtk python3 - <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
st = h.read_state(secret=os.environ.get("HERMX_SECRET"))
print("kill_switch_engaged:", st["kill_switch_engaged"], "| armed:", st["armed"])
PY
```
`kill_switch_engaged == UNKNOWN` (health unreachable) → the kill is **unconfirmed**;
do not report the system as safe until `/health` returns `true`.

### `flatten` — close every open position

1. **Dry-run:** list every open position from `/api` (UNKNOWN → refuse, never "flat"):
```bash
rtk python3 - <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
st = h.read_state(secret=os.environ.get("HERMX_SECRET"))
print("freshness:", st["freshness"], "| status:", st["positions_status"])
if st["positions"] == h.UNKNOWN:
    print("positions UNKNOWN — REFUSING to flatten."); sys.exit(2)
print(h.format_positions(st["positions"]))
print("Will close ALL of the above, reduce-only. Confirm? [yes]")
PY
```
2. **On `yes`:** iterate `POST /api/close` once per open position (map each symbol to
   its `strategy_id`), then **re-read `/api` to verify flat**:
```bash
rtk python3 - <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
secret = os.environ.get("HERMX_SECRET")
st = h.read_state(secret=secret)
if st["positions"] == h.UNKNOWN:
    print("positions UNKNOWN — aborting."); sys.exit(2)
byid = {r["symbol"]: r["id"] for r in h.list_strategies("strategies")}
for sym, p in st["positions"].items():
    if str((p or {}).get("side", "")).upper() in ("", "FLAT"):
        continue
    sid = byid.get(sym)
    if not sid:
        print(f"{sym}: no strategy maps to it — skipping (close manually)"); continue
    r = h.post_close(h.RECEIVER_BASE, secret, sym, sid,
                     operator="operator", reason="emergency flatten")
    print(f"{sym}: {r['outcome']} ({r['reason']}) cl_ord_id={r['cl_ord_id']}")
st2 = h.read_state(secret=secret)
print("post-flatten:", h.format_positions(st2["positions"]))
PY
```
   Any `UNKNOWN` outcome means that position's state is indeterminate — verify it
   individually; do not report the book as flat.

### `demo <id>` — force one strategy to sandbox

Dry-run preview then `POST /api/control/strategy/{id}` with `{"mode":"demo"}` (uses
`post_strategy_mode`). This is the mutating twin of `/strategy-mode <id> demo`:
```bash
rtk python3 - "$1" <<'PY'
import sys, os; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
sid = h.resolve_strategy(sys.argv[1], "strategies")["resolved"]
cur = {r["id"]: r for r in h.list_strategies("strategies", str(h.CONTROL_STATE_PATH))}.get(sid, {})
print(f"resolved: {sid} | current effective: {cur.get('effective_mode','UNKNOWN')} -> demo")
print("Confirm? [yes]")
# on yes:
# r = h.post_strategy_mode(h.DASHBOARD_BASE, os.environ.get("HERMX_SECRET"), sid, "demo")
PY
```

### `pause-symbol <sym>` — pause one symbol in control-state.json

Add the symbol to `control-state.json` `symbol_pauses` via the **safe updater** (atomic
read-modify-write; refuses on a corrupt file; bumps `updated_at`). Dry-run shows the
JSON diff first:
```bash
rtk python3 - "$1" <<'PY'
import sys; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
sym = sys.argv[1].upper()
def mut(state):
    state.setdefault("symbol_pauses", {})[sym] = {"paused": True, "reason": "emergency pause-symbol"}
# Resolve the control-state path via HERMX_DATA_DIR so we write the file the server reads.
pv = h.preview_control_state_update(str(h.CONTROL_STATE_PATH), mut)  # DRY-RUN, no write
print("changed:", pv["changed"]); print(pv["diff"] or "(no change)")
print("Confirm? [yes]")
# on yes:
# res = h.safe_update_control_state(str(h.CONTROL_STATE_PATH), mut)
# print("written:", res["ok"], "| changed:", res["changed"])
PY
```
A paused symbol returns `not_submitted` (`reason: "symbol_paused"`) for any new order,
regardless of strategy mode.

## Execution control model

Two controls decide whether and where an order is placed:

1. **Per-strategy** — `execution_mode` (`demo` or `live`) in `strategies/<id>.json`.
   **Only `live` is real-money** and routes to the real account; `demo` routes to the
   exchange sandbox (treated as `simulated_trading`).
2. **Global** — `HERMX_LIVE_TRADING` (env). Required truthy for any `live` order;
   irrelevant to non-`live` (sandbox) modes.

`ExecutionService.execute()` blocks submission (fail-safe `not_submitted`) on any of:
strategy without valid `execution_mode` / auth unhealthy / watchdog paused; a `live`
strategy when `HERMX_LIVE_TRADING` is not truthy (`live_trading_disabled`); a paused
symbol; or a duplicate `cl_ord_id`. The system never submits on uncertainty.

## Required Log

Every emergency action (`kill`, `flatten`, `demo`, `pause-symbol`) must log:

- time
- operator
- reason
- strategies affected (and/or symbols)
- exchange position **before**
- exchange position **after** (the confirm re-read)

For `flatten`, record each per-position outcome + `cl_ord_id`. For `kill`, record the
`/health` `kill_switch_engaged` value observed after the restart. If a re-read is
`UNKNOWN`, log it as UNKNOWN — never backfill it as "flat".

## Verification checklist

- [ ] `kill` outputs the env + restart steps and confirms via `/health`
      `kill_switch_engaged == true`; UNKNOWN health → reported unconfirmed, not safe.
- [ ] Every mutating command previews first and requires an explicit `yes`.
- [ ] `flatten` dry-run lists open positions; `positions == UNKNOWN` → refuses.
- [ ] `flatten` closes reduce-only per position (no size, `/api/close` not `/webhook`)
      and re-reads `/api` to verify flat; UNKNOWN outcomes flagged individually.
- [ ] `demo <id>` resolves the id, previews current→demo, POSTs the control override
      (never edits `strategies/*.json`).
- [ ] `pause-symbol <sym>` previews the JSON diff, writes via the atomic safe updater,
      bumps `updated_at`, and refuses on a corrupt control-state file.
- [ ] Every action writes the required log record (before/after positions included).
