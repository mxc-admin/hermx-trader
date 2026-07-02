---
name: hermx-tv-alerts
description: Use when the operator wants copy-paste-ready TradingView alert Message templates for a HermX strategy — "give me the BUY/SELL alert JSON for SOL", "what do I paste into TradingView for btcusdt_duo_base_dev_2h". Read-only. Resolves a name/id/symbol to a strategy_id via hermx_ops.resolve_strategy, reads the strategy file, and emits two schema-valid alert payloads (long/short) plus the webhook URL + X-Webhook-Secret guidance. Never sends an alert, never mutates, never calls /webhook.
version: 0.1.0
author: HermX
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [trading, hermx, tradingview, alerts, templates, read-only, operations]
    related_skills: [hermx-strategy-list, hermx-status, hermx-trace, hermx-help]
    config:
      - key: hermx.strategies_dir
        description: "Directory of strategy files"
        default: "strategies"
      - key: hermx.receiver_base
        description: "HermX receiver base URL (loopback)"
        default: "http://127.0.0.1:8891"
---

# /tv-alerts — copy-paste TradingView alert templates for a strategy

**Read-only.** `/tv-alerts <name-or-id>`. Resolves the arg to a `strategy_id`, reads
`strategies/<id>.json`, and prints two ready-to-paste **Message** payloads — one BUY
(long) and one SELL (short) — that satisfy the alert contract and schema. Emits text
only: **no HTTP request, no file write, no mutation, and it never routes via `/webhook`.**

The payload shape, the venue/mode/sizing split, and every validation gate live in
[`../../docs/ALERT_CONTRACT.md`](../../docs/ALERT_CONTRACT.md) and the schema
[`../../schemas/tradingview-alert.schema.json`](../../schemas/tradingview-alert.schema.json).
Resolution semantics live in
[`../hermx-ops/references/api-contract.md`](../hermx-ops/references/api-contract.md).

## When to use
- "what's the TradingView alert JSON for SOLUSDT 3H?", "give me the buy + sell message
  box for `btcusdt_duo_base_dev_2h`", "give me the close alert template for BTCUSDT".
- Do NOT use to send, arm, or execute an alert — this only prints templates. To verify a
  received alert flowed through, use `/trace`.

## Argument resolution (never guess)
`resolve_strategy(arg)` precedence, first match wins:
1. exact `strategy_id`
2. exact file basename (with/without `.json`)
3. exact `symbol` — **only if unique** across strategies
4. fuzzy — **never auto-applied**; returns candidates to confirm

If `resolved` is `None` (ambiguous symbol or fuzzy-only), **stop** and print the
candidates for the operator to disambiguate. Never emit a template for a guessed id.

## What gets extracted from the strategy file
- `strategy_id` — the resolved id (payload `strategy_id`).
- `symbol` — derived from `instrument.inst_id` (`SOL-USDT-SWAP` → `SOLUSDT`; payload `symbol`).
- `timeframe` — the strategy's bar (payload `timeframe`, **hard-coded**, never `{{interval}}`).
- `execution_mode` — `demo` / `live`. **Context only** — it is *not* an alert field; the
  receiver reads venue, mode, and sizing from the strategy file, never from the alert.

## Payload contract (must match exactly)
BUY/SELL (long/short):
```json
{
  "strategy_id": "<resolved-id>",
  "symbol": "<resolved-symbol>",
  "timeframe": "<from-strategy>",
  "action": "buy",
  "side": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```
CLOSE (flatten position):
```json
{
  "strategy_id": "<resolved-id>",
  "symbol": "<resolved-symbol>",
  "timeframe": "<from-strategy>",
  "action": "close",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "source": "tradingview"
}
```
- `action` is the primary field (`buy`/`sell`/`close`). `side` is derived/legacy
  (`buy`/`sell` only, present for back-compat) and is included in the buy/sell templates
  during the transition; the close template carries **only** `action` and no `side`.
- BUY vs SELL differ only in `action`/`side` (`buy` (long) / `sell` (short)). The schema
  enums are exactly `buy`/`sell` for `side` and `buy`/`sell`/`close` for `action` — not
  `long`/`short`.
- `tv_signal_price` = `{{close}}` and `tv_time` = `{{time}}` are Pine Script placeholders
  TradingView substitutes at fire time; leave them literal.
- **The alert carries no `exchange` field.** Venue routing comes entirely from
  `strategy.instrument.exchange`, matched on `strategy_id` — never from the payload.
- **`symbol` is hard-coded to the resolved symbol**, not `{{ticker}}`. `{{ticker}}` also
  validates (the receiver uppercases + strips `-`/`/`), but hard-coding guarantees the
  `strategy_symbol_mismatch` gate passes regardless of which chart the alert sits on.

## Procedure
```bash
rtk python3 - <<'PY'
import sys, json; sys.path.insert(0, "skills/hermx-ops/lib")
import hermx_ops as h

arg = "SOLUSDT"   # <-- operator arg: name / strategy_id / basename / symbol
res = h.resolve_strategy(arg, str(h.STRATEGIES_DIR))
if not res["resolved"]:
    print("NO UNIQUE MATCH —", res["reason"])
    if res["candidates"]:
        print("candidates:", ", ".join(res["candidates"]))
        print("re-run with an exact strategy_id.")
    sys.exit(0)

sid = res["resolved"]
strat = next((s for s in h.list_strategies(str(h.STRATEGIES_DIR)) if s["id"] == sid), None)
data, err = h.safe_json_load(h.STRATEGIES_DIR / strat["file"])
if not isinstance(data, dict):
    print("UNREADABLE strategy file:", err)
    sys.exit(0)

inst = data.get("instrument") or {}
symbol = h._symbol_from_inst_id(inst.get("inst_id"))
timeframe = data.get("timeframe") or h.UNKNOWN
venue = inst.get("exchange") or "okx"             # context only; not an alert field
mode = str(data.get("execution_mode") or "demo").lower()

def tmpl(action, side=None):
    payload = {
        "strategy_id": sid,
        "symbol": symbol,
        "timeframe": timeframe,
        "action": action,
        "tv_signal_price": "{{close}}",
        "tv_time": "{{time}}",
        "source": "tradingview",
    }
    if side:
        payload["side"] = side
    return json.dumps(payload, separators=(",", ":"))   # compact single-line, matches the contract

print(f"strategy : {sid}  ({data.get('name') or sid})")
print(f"symbol   : {symbol}   timeframe: {timeframe}   venue: {venue}   mode: {mode}")
print()
print("# BUY (long) — paste into the BUY signal alert's Message box")
print(tmpl("buy", side="buy"))
print()
print("# SELL (short) — paste into the SELL signal alert's Message box")
print(tmpl("sell", side="sell"))
print()
print("# CLOSE (flatten position) — paste into the CLOSE signal alert's Message box")
print(tmpl("close"))
PY
```

## Webhook wiring (report alongside the templates)
- **Webhook URL:** the receiver's `/webhook` endpoint. Local/loopback default is
  `http://127.0.0.1:8891/webhook`; from TradingView (which is remote) use the host's
  public form `https://<host>/webhook` or the **Tailscale Funnel** URL that fronts it
  (see [`../../setup/04-tradingview-alerts.md`](../../setup/04-tradingview-alerts.md) and
  [`../../setup/08-webhook-hmac-relay.md`](../../setup/08-webhook-hmac-relay.md)).
- **Auth header:** set `X-Webhook-Secret: <HERMX_SECRET>` in the alert's webhook settings
  (TradingView Pro+ custom headers). The secret is **never** in the Message body or the
  URL — header only. Requests without it are rejected. No custom-header plan → use the
  HMAC relay path.
- **Strategy must be loaded:** the receiver quarantines `unknown_strategy_id` for any id
  with no matching file. Confirm the strategy is live first with
  `rtk claude -p "/strategy-list" --permission-mode dontAsk`.
- **TradingView alert settings:** condition = the strategy's BUY/SELL signal;
  frequency = **once per bar close**; expiration = open-ended/max; leave `timeframe`
  hard-coded to the strategy's bar so a wrong-chart alert is caught as
  `strategy_timeframe_mismatch` rather than silently accepted.
- **CLOSE alert:** its condition = the strategy's exit/close signal (the Pine strategy's
  exit condition if you have one) or a separate manual alert. Paste the CLOSE template into
  its Message box — it needs **no `side`**, only `action=close`.

## Reporting
- Print both templates as compact single-line JSON so a copy-paste drops cleanly into the
  Message box. Label which goes in the BUY alert vs the SELL alert.
- Surface `execution_mode` as context, but state plainly it is **not** an alert field —
  mode/venue/sizing come from the strategy file, not the payload.
- If the arg is ambiguous, show candidates and stop — do not emit a guessed template.
- Never suggest adding a size/budget/leverage field; any such field is ignored by the
  receiver and sizing is owned by the execution layer.

## Verification checklist
- [ ] `resolve_strategy` returns a unique `strategy_id`; an ambiguous symbol prints
      candidates and emits **no** template.
- [ ] All templates parse as JSON and validate against
      `schemas/tradingview-alert.schema.json` (6 base required fields + `anyOf` satisfied by
      `side` or `action`; `side` ∈ `buy|sell`; `action` ∈ `buy|sell|close`; `timeframe` ∈
      the schema enum).
- [ ] `symbol` matches the strategy's `instrument.inst_id`-derived asset and `timeframe`
      matches the strategy file (so neither `strategy_symbol_mismatch` nor
      `strategy_timeframe_mismatch` would fire).
- [ ] BUY, SELL, CLOSE: buy/sell include both `action` and `side`; close has
      `action=close` and **no** `side`.
- [ ] CLOSE template validates against the schema without `side` (schema `anyOf` satisfied
      by `action`).
- [ ] No `exchange` field is present in any payload; `timeframe` is
      hard-coded, not `{{interval}}`.
- [ ] Webhook URL, `X-Webhook-Secret` header, and "strategy must be loaded" are stated.
- [ ] No HTTP request issued, no file written, no `/webhook` call — read-only throughout.
