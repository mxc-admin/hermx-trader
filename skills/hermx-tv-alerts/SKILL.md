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
  box for `btcusdt_duo_base_dev_2h`".
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
- `exchange` — `instrument.exchange` (payload `exchange`; see the note below).
- `execution_mode` — `demo` / `live`. **Context only** — it is *not* an alert field; the
  receiver reads venue, mode, and sizing from the strategy file, never from the alert.

## Payload contract (must match exactly)
```json
{
  "strategy_id": "<resolved-id>",
  "symbol": "<resolved-symbol>",
  "timeframe": "<from-strategy>",
  "side": "buy",
  "tv_signal_price": "{{close}}",
  "tv_time": "{{time}}",
  "exchange": "<strategy-venue>",
  "source": "tradingview"
}
```
- `side` is the only field that differs between the two templates: `buy` (long) / `sell`
  (short). The schema enum is exactly `buy`/`sell` — not `long`/`short`.
- `tv_signal_price` = `{{close}}` and `tv_time` = `{{time}}` are Pine Script placeholders
  TradingView substitutes at fire time; leave them literal.
- **`exchange` is hard-coded to the strategy's `instrument.exchange` (e.g. `okx`) — do NOT
  use `{{exchange}}`.** TradingView's `{{exchange}}` emits the chart feed's venue name in
  **uppercase** (e.g. `OKX`) and can name an unwired venue; the schema enum is lowercase
  `okx|kucoin|bybit|hyperliquid`, so `{{exchange}}` fails `alert_schema_invalid` under
  enforcement. The alert `exchange` is advisory anyway — routing comes from
  `strategy.instrument.exchange` — so hard-coding the strategy's venue is both accurate
  and schema-valid.
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
exchange = inst.get("exchange") or "okx"          # advisory; routing = strategy venue
mode = str(data.get("execution_mode") or "demo").lower()

def tmpl(side):
    return json.dumps({
        "strategy_id": sid,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "tv_signal_price": "{{close}}",
        "tv_time": "{{time}}",
        "exchange": exchange,
        "source": "tradingview",
    }, separators=(",", ":"))          # compact single-line, matches the contract

print(f"strategy : {sid}  ({data.get('name') or sid})")
print(f"symbol   : {symbol}   timeframe: {timeframe}   venue: {exchange}   mode: {mode}")
print()
print("# BUY (long) — paste into the BUY alert's Message box")
print(tmpl("buy"))
print()
print("# SELL (short) — paste into the SELL alert's Message box")
print(tmpl("sell"))
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
- [ ] Both templates parse as JSON and validate against
      `schemas/tradingview-alert.schema.json` (all 8 required fields; `side` ∈ `buy|sell`;
      `exchange` ∈ the four wired venues; `timeframe` ∈ the schema enum).
- [ ] `symbol` matches the strategy's `instrument.inst_id`-derived asset and `timeframe`
      matches the strategy file (so neither `strategy_symbol_mismatch` nor
      `strategy_timeframe_mismatch` would fire).
- [ ] BUY and SELL payloads differ **only** in `side`.
- [ ] `exchange` is the hard-coded strategy venue, not `{{exchange}}`; `timeframe` is
      hard-coded, not `{{interval}}`.
- [ ] Webhook URL, `X-Webhook-Secret` header, and "strategy must be loaded" are stated.
- [ ] No HTTP request issued, no file written, no `/webhook` call — read-only throughout.
