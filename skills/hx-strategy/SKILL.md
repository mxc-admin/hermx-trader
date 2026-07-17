---
name: hx-strategy
description: "Use when the operator wants to ADD, UPDATE, or ARCHIVE a HermX strategy definition file (strategies/*.json). add/update are INTERACTIVE: the agent collects fields conversationally (add offers auto-generated or manual strategy_id), always dry-runs first, requires explicit 'yes' before any write, and after a successful add/update prints the strategy's BUY/SELL/CLOSE TradingView alert Message templates (same payload shape as /hx-tv-alerts). Mutating, filesystem-only — no dashboard/receiver endpoint exists for strategy CRUD. Validates against the strategy_v2 schema and archives by moving to strategies/_archive/ (never deletes). Files load at import time: NO change is live until /hx-restart."
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: HERMX_SECRET
    prompt: "HermX dashboard shared secret (X-Dashboard-Token header)"
    help: "Set in HermX .env on this host. Required when HERMX_DASH_AUTH=true; without it the archive position pre-check reads UNKNOWN and archive refuses."
    required_for: "Authenticated dashboard /api read for the archive open-position pre-check"
metadata:
  hermes:
    tags: [trading, hermx, strategies, mutating, crud, operations]
    related_skills: [hx-strategy-mode, hx-strategy-list, hx-restart, hx-close, hx-tv-alerts]
    config:
      - key: hermx.strategies_dir
        description: "Directory of strategy files"
        default: "strategies"
      - key: hermx.control_state
        description: "Per-strategy mode/pause overrides"
        default: "control-state.json"
---

# /hx-strategy — add / update / archive a HermX strategy file

**Mutating.** `/hx-strategy <add|update|archive> ...`

Full CRUD for `strategies/*.json` definition files, validated against the
`strategy_v2` shape in `schemas/strategy.schema.json`. This is a **pure filesystem**
skill — there is no dashboard/receiver endpoint for strategy CRUD. File shape and the
write contract live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## ⚠️ CRITICAL: changes are inert until restart

`webhook_receiver.py` builds `STRATEGIES = load_strategy_files()` **at import time**
(`src/webhook_receiver.py:500`) and `dashboard.py` globs `STRATEGIES_DIR` the same way
(`src/dashboard.py:144` `load_strategy_files()`). Strategy files are **NOT
hot-reloaded**. Every add/update/archive is inert until the receiver AND dashboard
restart. **Every success report from this skill MUST end with:
"not live until /hx-restart — run `/hx-restart` to apply."**

## Territory boundaries
- This skill is distinct from `/hx-strategy-mode` (which POSTs a control override and
  **never** touches `strategies/*.json`) and `/hx-strategy-list` (read-only). This
  skill **never edits `control-state.json`** — mode overrides stay in
  hx-strategy-mode's territory.
- `strategies/*.json` is **git-tracked**. This skill NEVER runs `git add`/`git commit`
  — committing is the operator's/`/git-commit`'s step. Report the uncommitted change.
- Never invent or suggest an order size — `capital.budget_usd` is configuration;
  sizing is derived by the execution layer.

## The one rule that matters: never archive what you can't confirm is flat

Archiving removes a strategy from the live set while its position may still be open —
the engine would keep the exposure but lose the strategy that manages it. So: if the
position read is **UNKNOWN** (executor down/stale/degraded, `/api` unreachable or
401), **refuse to archive** — we cannot confirm it is safe. If the position is **OPEN**,
refuse and tell the operator to `/hx-close` first. Only a healthy, non-degraded read
showing genuinely **FLAT** may proceed. (Same UNKNOWN-never-flat framing as
`/hx-close`.)

## Two-step pattern (all subcommands)

Every subcommand's block is dry-run by default: it runs all gates, prints the full
preview, and **writes nothing**. Only after the operator's explicit `yes` (mandatory
for any write; a `live` execution_mode additionally requires the `yes` to explicitly
acknowledge LIVE), re-run the **same block** prefixed with `HX_CONFIRM=yes`. Dry-run
and apply share one script so they can never diverge.

## Procedure — `add`

`/hx-strategy add` — **interactive**. No longer a single-shot
`add <strategy_id> <field=value ...>` invocation: the agent collects the fields
conversationally (below), then builds the argv `<strategy_id|AUTO> <field=value ...>`
itself and runs the **same** dry-run / `HX_CONFIRM=yes` script as before.

Required fields: `name`, `exchange`, `inst_id`, `timeframe`, `indicator`,
`budget_usd`, `leverage`. Defaulted: `instrument_type=swap`, `margin_mode=isolated`,
`execution_mode=demo`. Optional: `reinvest`, `max_notional_usd`, `notes`.

### Interactive collection (before running anything)
Ask the operator, one topic at a time (closely related asks may be batched):
1. `name` — display name.
2. `exchange` (e.g. `okx`) and `inst_id` (e.g. `BTC-USDT-SWAP`) — or equivalently
   symbol + venue, from which you state the exact `inst_id` back and get it confirmed.
3. `timeframe` — one of `30m` / `1h` / `2h` / `3h` / `4h`.
4. `indicator` — free text describing the signal source.
5. `budget_usd` and `leverage`.
6. `margin_mode` — default `isolated`; confirm the default or take an override.
7. `execution_mode` — default `demo`; confirm explicitly. If the operator wants
   `live`, flag **REAL MONEY** immediately and remind them that the write-time `yes`
   must explicitly acknowledge LIVE (the existing live gate — unchanged).

Never guess a value the operator hasn't provided, and never apply a default
silently — state each default and get it confirmed before building the argv.

### Strategy ID: auto-generate or manual
Ask: "Would you like me to auto-generate a strategy_id, or would you like to provide
your own?"
- **Manual:** validate against `^[a-z0-9]+(?:_[a-z0-9]+)*$` (the script's Gate 1).
  If it doesn't match, tell the operator why and ask again — never silently mutate
  their input. Pass the id as the first argv.
- **Auto-generate:** pass the literal `AUTO` as the first argv. The script derives
  `<symbol_lowercase>_<indicator_slug>_<timeframe>` (symbol via
  `h._symbol_from_inst_id(inst_id)` lowercased; indicator slug = indicator text
  lowercased, non-alphanumeric runs collapsed to single `_`, leading/trailing `_`
  stripped) and appends `_2`, `_3`, … until it collides with no existing file and no
  declared strategy_id. The generated id is printed in the dry-run preview so the
  operator sees it **before** confirming the write — never silently.

### 1. Dry-run (no write), then 2. Apply with `HX_CONFIRM=yes` after explicit `yes`
```bash
rtk python3 - "$@" <<'PY'
import json, os, re, sys, tempfile
sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
import jsonschema

CONFIRM = os.environ.get("HX_CONFIRM", "").lower() == "yes"
# Mirror of the schema's no_inline_credentials list — defense in depth.
FORBIDDEN = {"api_key", "apiKey", "secret", "secret_key", "secretKey", "passphrase",
             "password", "private_key", "privateKey", "wallet", "wallet_address",
             "walletAddress", "token", "credentials"}

sid = sys.argv[1]
kv = dict(a.split("=", 1) for a in sys.argv[2:])

if sid == "AUTO":
    # Auto-generate <symbol>_<indicator_slug>_<timeframe>; _2, _3... on collision.
    symbol_lc = h._symbol_from_inst_id(kv["inst_id"]).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", kv["indicator"].lower()).strip("_")
    base = f"{symbol_lc}_{slug}_{kv['timeframe'].lower()}"
    declared = {r["id"] for r in h.list_strategies(str(h.STRATEGIES_DIR))}
    sid, n = base, 2
    while (h.STRATEGIES_DIR / f"{sid}.json").exists() or sid in declared:
        sid = f"{base}_{n}"; n += 1
    print(f"auto-generated strategy_id: {sid}")

# Gate 1: strategy_id pattern — before anything else (auto-generated ids included).
if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", sid):
    print("outcome: rejected | reason: bad_pattern | strategy_id must match "
          "^[a-z0-9]+(?:_[a-z0-9]+)*$"); sys.exit(1)
# Gate 2: no credential fields, ever.
bad = {k.split(".")[-1] for k in kv} & FORBIDDEN
if bad:
    print(f"outcome: rejected | reason: forbidden_credential_field | {sorted(bad)}"); sys.exit(1)
# Gate 3: duplicates — target file AND any file declaring this strategy_id.
dest = h.STRATEGIES_DIR / f"{sid}.json"
if dest.exists():
    print(f"outcome: rejected | reason: duplicate_id | file exists: {dest}"); sys.exit(1)
if any(r["id"] == sid for r in h.list_strategies(str(h.STRATEGIES_DIR))):
    print(f"outcome: rejected | reason: duplicate_id | another strategy file already "
          f"declares strategy_id={sid}"); sys.exit(1)

def num(v):
    f = float(v)
    return int(f) if f.is_integer() else f

doc = {
    "schema_version": 2,
    "strategy_id": sid,
    "name": kv["name"],
    "indicator": kv["indicator"],
    "timeframe": kv["timeframe"],
    "instrument": {
        "exchange": kv["exchange"],
        "inst_id": kv["inst_id"],
        "type": kv.get("instrument_type", "swap"),
    },
    "capital": {"budget_usd": num(kv["budget_usd"])},
    "execution_mode": kv.get("execution_mode", "demo"),
    "leverage": num(kv["leverage"]),
    "margin_mode": kv.get("margin_mode", "isolated"),
}
if "reinvest" in kv:
    doc["capital"]["reinvest"] = kv["reinvest"].lower() == "true"
if "max_notional_usd" in kv:
    doc["capital"]["max_notional_usd"] = num(kv["max_notional_usd"])
if "notes" in kv:
    doc["notes"] = kv["notes"]

# Validate against $defs.strategy_v2 directly, wrapped with the root $defs so its
# internal "#/$defs/no_inline_credentials" $ref still resolves. Validating the whole
# file's top-level oneOf would blur v1+v2 branch errors into one useless message;
# this skill only ever writes v2, so pin the v2 branch for a precise error.
schema, serr = h.safe_json_load(h.REPO_ROOT / "schemas" / "strategy.schema.json")
if serr:
    print(f"outcome: UNKNOWN | reason: schema_unreadable:{serr} — refusing to write"); sys.exit(2)
try:
    jsonschema.validate(doc, {"$ref": "#/$defs/strategy_v2", "$defs": schema["$defs"]})
except jsonschema.ValidationError as e:
    print(f"outcome: rejected | reason: schema_invalid | {e.json_path}: {e.message}"); sys.exit(1)

print(json.dumps(doc, indent=2))
print(f"validation: OK (strategy_v2) | target: {dest}")
if doc["execution_mode"] == "live":
    print("!! execution_mode=live = REAL MONEY. The 'yes' must explicitly acknowledge LIVE.")
if not CONFIRM:
    print("DRY-RUN — nothing written. After operator 'yes', re-run with HX_CONFIRM=yes."); sys.exit(0)

# Atomic write: tmp file in the same dir + os.replace — never a partial file.
if dest.exists():  # re-check at write time; never silently overwrite
    print(f"outcome: rejected | reason: duplicate_id | file appeared: {dest}"); sys.exit(1)
try:
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, str(dest))
except OSError as e:
    print(f"outcome: UNKNOWN | reason: io_error:{e} — verify {dest} state manually"); sys.exit(2)
back, rerr = h.safe_json_load(dest)  # confirm re-read
print(f"outcome: written | file: {dest} | confirm re-read: "
      f"{'OK id=' + back.get('strategy_id', '?') if not rerr else 'FAILED:' + rerr}")

# TradingView alert templates — identical payload shape to /hx-tv-alerts's tmpl().
symbol = h._symbol_from_inst_id(doc["instrument"]["inst_id"])
def tmpl(action):
    payload = {
        "strategy_id": sid,
        "symbol": symbol,
        "timeframe": doc["timeframe"],
        "action": action,
        "tv_signal_price": "{{close}}",
        "tv_time": "{{time}}",
        "source": "tradingview",
    }
    return json.dumps(payload, separators=(",", ":"))   # compact single-line
print()
print("# BUY (long) — paste into the BUY signal alert's Message box")
print(tmpl("buy"))
print()
print("# SELL (short) — paste into the SELL signal alert's Message box")
print(tmpl("sell"))
print()
print("# CLOSE (flatten position) — paste into the CLOSE signal alert's Message box")
print(tmpl("close"))
print()
print("Webhook: POST https://<host>/webhook with the X-Webhook-Secret header — "
      "see /hx-tv-alerts for the full wiring guide.")
print("NOT LIVE until /hx-restart — receiver+dashboard load strategies at import time.")
PY
```

## Procedure — `update`

`/hx-strategy update <name-or-id>` — **interactive**. The name/id/symbol stays a
positional argument (resolution is NOT interactive), but the fields to change are no
longer supplied upfront: the agent shows the current file, collects the changes
conversationally (below), then builds dotted-path `field=value` pairs
(`capital.budget_usd=2000`, `instrument.inst_id=ETH-USDT-SWAP`) and runs the **same**
dry-run / `HX_CONFIRM=yes` script as before.

### 0. Resolve and show the current file (read-only, no write possible)
```bash
rtk python3 - "$@" <<'PY'
import json, sys
sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h

arg = sys.argv[1]
res = h.resolve_strategy(arg, str(h.STRATEGIES_DIR))
if not res["resolved"]:
    print(f"outcome: rejected | reason: unresolved | {res['reason']} "
          f"| candidates: {res['candidates']}"); sys.exit(1)
row = {r["id"]: r for r in h.list_strategies(str(h.STRATEGIES_DIR))}[res["resolved"]]
doc, err = h.safe_json_load(h.STRATEGIES_DIR / row["file"])
if err or not isinstance(doc, dict):
    print(f"outcome: UNKNOWN | reason: read_failed:{err} | file: {row['file']}"); sys.exit(2)
print(f"resolved: {res['resolved']} (via {res['match']}) | file: {row['file']}")
print(json.dumps(doc, indent=2))
PY
```
Show this current content to the operator before asking what to change.

### Interactive field selection
ASK the operator which field(s) they want to change — present the realistic editable
fields: `name`, `capital.budget_usd`, `capital.reinvest`, `capital.max_notional_usd`,
`leverage`, `margin_mode`, `execution_mode`, `timeframe`, `instrument.exchange`,
`instrument.inst_id`, `notes`. Collect each new value conversationally and confirm it
before adding its dotted-path `field=value` pair to the argv.

**`strategy_id` and `schema_version` are immutable here** — the script's
`immutable_field` gate rejects them, but explain this upfront (rename/re-version =
`archive` + `add`) the moment the operator asks, before even attempting the write.

### 1. Dry-run (no write), then 2. Apply with `HX_CONFIRM=yes` after explicit `yes`
```bash
rtk python3 - "$@" <<'PY'
import copy, json, os, sys, tempfile
sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h
import jsonschema

CONFIRM = os.environ.get("HX_CONFIRM", "").lower() == "yes"
FORBIDDEN = {"api_key", "apiKey", "secret", "secret_key", "secretKey", "passphrase",
             "password", "private_key", "privateKey", "wallet", "wallet_address",
             "walletAddress", "token", "credentials"}
BOOL_LEAVES = {"reinvest", "submit_orders"}
NUM_LEAVES = {"budget_usd", "max_notional_usd", "leverage"}

arg, pairs = sys.argv[1], sys.argv[2:]
res = h.resolve_strategy(arg, str(h.STRATEGIES_DIR))
if not res["resolved"]:
    print(f"outcome: rejected | reason: unresolved | {res['reason']} "
          f"| candidates: {res['candidates']}"); sys.exit(1)
sid = res["resolved"]
row = {r["id"]: r for r in h.list_strategies(str(h.STRATEGIES_DIR))}[sid]
path = h.STRATEGIES_DIR / row["file"]
doc, err = h.safe_json_load(path)
if err or not isinstance(doc, dict):
    print(f"outcome: UNKNOWN | reason: read_failed:{err} | file: {path}"); sys.exit(2)

def coerce(leaf, raw):
    if leaf in BOOL_LEAVES:
        return raw.lower() == "true"
    if leaf in NUM_LEAVES:
        f = float(raw)
        return int(f) if f.is_integer() else f
    return raw  # strings stay strings; schema validation is the backstop

new, changes = copy.deepcopy(doc), []
for pair in pairs:
    key, sep, raw = pair.partition("=")
    if sep != "=":
        print(f"outcome: rejected | reason: bad_arg | expected field=value: {pair}"); sys.exit(1)
    parts = key.split(".")
    if set(parts) & FORBIDDEN:
        print(f"outcome: rejected | reason: forbidden_credential_field | {key}"); sys.exit(1)
    if key in ("strategy_id", "schema_version"):
        print(f"outcome: rejected | reason: immutable_field | {key} "
              "(rename = archive + add)"); sys.exit(1)
    node = new
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            print(f"outcome: rejected | reason: bad_path | {key}"); sys.exit(1)
    before = node.get(parts[-1], "<absent>")
    node[parts[-1]] = coerce(parts[-1], raw)
    changes.append((key, before, node[parts[-1]]))

schema, serr = h.safe_json_load(h.REPO_ROOT / "schemas" / "strategy.schema.json")
if serr:
    print(f"outcome: UNKNOWN | reason: schema_unreadable:{serr} — refusing to write"); sys.exit(2)
try:
    jsonschema.validate(new, {"$ref": "#/$defs/strategy_v2", "$defs": schema["$defs"]})
except jsonschema.ValidationError as e:
    print(f"outcome: rejected | reason: schema_invalid | {e.json_path}: {e.message}"); sys.exit(1)

print(f"resolved: {sid} (via {res['match']}) | file: {path}")
for key, before, after in changes:   # diff of ONLY the changed fields
    print(f"  {key}: {before!r} -> {after!r}")
if new.get("execution_mode") == "live" and doc.get("execution_mode") != "live":
    print("!! transition to execution_mode=live = REAL MONEY. "
          "The 'yes' must explicitly acknowledge LIVE.")
if not CONFIRM:
    print("DRY-RUN — nothing written. After operator 'yes', re-run with HX_CONFIRM=yes."); sys.exit(0)

try:  # atomic write back to the SAME path
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, str(path))
except OSError as e:
    print(f"outcome: UNKNOWN | reason: io_error:{e} — verify {path} state manually"); sys.exit(2)
back, rerr = h.safe_json_load(path)
print(f"outcome: updated | file: {path} | confirm re-read: "
      f"{'OK' if not rerr and isinstance(back, dict) else 'FAILED:' + str(rerr)}")

# Refreshed TradingView alert templates — identical shape to /hx-tv-alerts's tmpl().
symbol = h._symbol_from_inst_id((new.get("instrument") or {}).get("inst_id"))
def tmpl(action):
    payload = {
        "strategy_id": sid,
        "symbol": symbol,
        "timeframe": new.get("timeframe"),
        "action": action,
        "tv_signal_price": "{{close}}",
        "tv_time": "{{time}}",
        "source": "tradingview",
    }
    return json.dumps(payload, separators=(",", ":"))   # compact single-line
print()
print("# BUY (long) — paste into the BUY signal alert's Message box")
print(tmpl("buy"))
print()
print("# SELL (short) — paste into the SELL signal alert's Message box")
print(tmpl("sell"))
print()
print("# CLOSE (flatten position) — paste into the CLOSE signal alert's Message box")
print(tmpl("close"))
print()
if any(k in ("instrument.inst_id", "timeframe") for k, _b, _a in changes):
    print("!! symbol/timeframe changed — every existing TradingView alert for this "
          "strategy MUST be updated to the templates above, or the receiver rejects "
          "it (strategy_symbol_mismatch / strategy_timeframe_mismatch).")
print("Webhook: POST https://<host>/webhook with the X-Webhook-Secret header — "
      "see /hx-tv-alerts for the full wiring guide.")
print("NOT LIVE until /hx-restart — receiver+dashboard load strategies at import time.")
PY
```

## Procedure — `archive`

`/hx-strategy archive <name-or-id>` — moves the file to
`strategies/_archive/<file>.json` (never deletes). `_archive/` sits **outside** the
non-recursive `strategies/*.json` glob used by both `webhook_receiver.py` and
`dashboard.py`, so the strategy drops out of the live set with history preserved.

### 1. Dry-run (no write), then 2. Apply with `HX_CONFIRM=yes` after explicit `yes`
```bash
rtk python3 - "$@" <<'PY'
import os, sys
sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h

CONFIRM = os.environ.get("HX_CONFIRM", "").lower() == "yes"
arg = sys.argv[1]
res = h.resolve_strategy(arg, str(h.STRATEGIES_DIR))
if not res["resolved"]:
    print(f"outcome: unresolved | {res['reason']} | candidates: {res['candidates']}"); sys.exit(1)
sid = res["resolved"]
row = {r["id"]: r for r in h.list_strategies(str(h.STRATEGIES_DIR))}[sid]
symbol, src = row["symbol"], h.STRATEGIES_DIR / row["file"]
dest = h.STRATEGIES_DIR / "_archive" / row["file"]

# Safety gate: never archive a position we can't confirm is flat.
st = h.read_state(secret=os.environ.get("HERMX_SECRET"))
pos = h.read_position_for_symbol(st, symbol)
print(f"resolved: {sid} (via {res['match']}) | symbol: {symbol} "
      f"| freshness: {st['freshness']} | position: {pos['status']}")
if pos["status"] == h.UNKNOWN:
    print("outcome: refused_unknown_position | executor read failed/stale — cannot "
          "confirm flat; REFUSING to archive."); sys.exit(2)
if pos["status"] == "OPEN":
    print(f"outcome: refused_open_position | {pos['side']} {pos['size']} open on "
          f"{symbol} — /hx-close it first."); sys.exit(3)

print(f"position genuinely FLAT on a healthy read. Will move:\n  {src}\n  -> {dest}")
if not CONFIRM:
    print("DRY-RUN — nothing moved. After operator 'yes', re-run with HX_CONFIRM=yes."); sys.exit(0)

try:
    dest.parent.mkdir(exist_ok=True)
    if dest.exists():
        print(f"outcome: refused | reason: archive_collision | {dest} already exists "
              "— resolve manually, never overwrite history."); sys.exit(1)
    src.rename(dest)
except OSError as e:
    print(f"outcome: UNKNOWN | reason: io_error:{e} — verify {src} / {dest} manually"); sys.exit(2)
still = any(r["id"] == sid for r in h.list_strategies(str(h.STRATEGIES_DIR)))
print(f"outcome: archived | {src} -> {dest} | confirm re-read: "
      f"{'FAILED — still in live set!' if still or src.exists() else 'OK — out of live set'}")
print("NOT LIVE until /hx-restart — the running receiver/dashboard still hold the "
      "old strategy set in memory.")
PY
```

## Outcomes
- **add**: `written` | `rejected` (`duplicate_id`, `schema_invalid`, `bad_pattern`,
  `forbidden_credential_field`) | `UNKNOWN` (I/O or schema read failure). An existing
  file is **never** silently overwritten.
- **update**: `updated` | `rejected` (`schema_invalid`, `unresolved`,
  `immutable_field`, `forbidden_credential_field`) | `UNKNOWN` only when the file
  read/write itself fails — report it, never silently succeed.
- **archive**: `archived` | `refused_open_position` | `refused_unknown_position` |
  `unresolved` | `UNKNOWN` (I/O failure mid-move — verify both paths manually).

## Required log (every mutation)
Record: time, operator, reason, subcommand, `strategy_id` affected, before→after —
for `update` the field diff; for `add` the new file path; for `archive` the
source→destination move — plus the confirm re-read result. `live` execution_mode
writes additionally log the explicit `yes`.

## Reporting
- Lead with resolved id + the exact change (new file / field diff / move) so the
  operator sees precisely what changed on disk.
- After a successful `add`/`update`, relay the printed BUY/SELL/CLOSE TradingView
  templates (compact single-line JSON, labeled per alert) and the one-line webhook /
  `X-Webhook-Secret` reminder — full wiring lives in `/hx-tv-alerts`, don't duplicate it.
- **Every successful add/update/archive report ends with the restart caveat:**
  "not live until /hx-restart — run `/hx-restart` to apply." The alert-template
  printout is in addition to this caveat, never a replacement.
- Note that the change is git-tracked and uncommitted; committing is the operator's step.
- Never restate `budget_usd`/`leverage` as an order size — sizing is owned by the
  execution layer.
- A `rejected`/`refused_*`/`UNKNOWN` outcome must never be reported as "done".

## Verification checklist
- [ ] `add` with an existing `strategies/<id>.json`, or an id declared inside another
      file, → `rejected: duplicate_id`; the existing file is byte-for-byte unchanged.
- [ ] `add` with an id violating `^[a-z0-9]+(?:_[a-z0-9]+)*$` (e.g. `My-Strat`) →
      `rejected: bad_pattern` before any other work.
- [ ] Schema-invalid input (bad timeframe, missing `budget_usd`, extra key) →
      `rejected: schema_invalid` with the failing json path; nothing written.
- [ ] Any `api_key`/`secret`/`passphrase`-style field → `rejected:
      forbidden_credential_field`, independent of the schema check.
- [ ] `execution_mode=live` (add) or a transition to `live` (update) → refuses without
      an explicit `yes` acknowledging LIVE.
- [ ] `update` prints the before→after diff of only the changed fields in the dry-run,
      before any write.
- [ ] `archive` with the executor down/stale (position UNKNOWN) →
      `refused_unknown_position`, file not moved.
- [ ] `archive` with an open position on the symbol → `refused_open_position`,
      operator pointed at `/hx-close`, file not moved.
- [ ] `archive` succeeds only on a genuinely FLAT, healthy read; the file lands in
      `strategies/_archive/` and disappears from `list_strategies` output.
- [ ] Archived file is excluded from both loaders' non-recursive `strategies/*.json`
      glob (confirm: `/hx-strategy-list` after restart no longer shows it).
- [ ] A crash mid-write leaves no partial `strategies/<id>.json` (tmp + `os.replace`);
      at worst a stray `*.tmp` file remains.
- [ ] `git status` shows the add/update/archive as an uncommitted change — the skill
      ran no git command.
- [ ] Every success report printed the "not live until /hx-restart" caveat.
- [ ] `add` with `AUTO`: the generated id is unique (file + declared-id check, `_2`/`_3`
      suffix on collision) and shown in the dry-run preview **before** any write.
- [ ] Manual id is still validated against `^[a-z0-9]+(?:_[a-z0-9]+)*$`; an invalid id
      is reported back and re-asked, never silently mutated.
- [ ] Every default (`margin_mode`, `execution_mode`, `instrument_type`) was explicitly
      confirmed by the operator during interactive collection — none applied silently.
- [ ] `update` explained that `strategy_id`/`schema_version` are immutable
      (archive + add) **before** attempting a write, when the operator asked to change one.
- [ ] Successful `add` printed the BUY, SELL, and CLOSE TradingView alert templates.
- [ ] Successful `update` printed the refreshed BUY/SELL/CLOSE templates and, if
      `instrument.inst_id` or `timeframe` changed, flagged that existing TradingView
      alerts must be updated (`strategy_symbol_mismatch`/`strategy_timeframe_mismatch`).
- [ ] Templates match the exact `/hx-tv-alerts` payload shape: parseable compact JSON,
      all required fields (`strategy_id`, `symbol`, `timeframe`, `action`,
      `tv_signal_price="{{close}}"`, `tv_time="{{time}}"`, `source="tradingview"`);
      direction is carried by `action` only — no `side` field, no `exchange` field.
