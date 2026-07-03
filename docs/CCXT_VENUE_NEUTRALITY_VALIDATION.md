# CCXT Venue-Neutrality Validation — P&L Accounting Plan

**Method.** Validated against the *installed* CCXT source (`4.5.61`, `.venv/.../site-packages/ccxt`),
which is authoritative and more reliable than web docs. Line numbers below are from that tree.
Scope: OKX, Binance, Bybit, Hyperliquid, Coinbase.

**Bottom line.** The plan's venue-neutrality section (`PNL_ACCOUNTING_EXECUTION_PLAN.md` §55-79) is
**directionally correct** — it already flags `info.pnl` / `info.reduceOnly` as OKX-specific and demands
adapter-level normalization. But it contains **two concrete inaccuracies** and **one unstated
assumption** that must be folded in before the claim reaches 9.6/10.

---

## Ground truth: the CCXT unified order structure

`base/exchange.py::safe_order` (the function every `parse_order` funnels through) returns a fixed dict
(lines 4403-4429). It contains `reduceOnly` (4422) and `fee` (4428) as **top-level unified keys**.
It contains **NO `pnl` / `realizedPnl` key.** Per-order realized P&L is therefore *not* a unified CCXT
concept — it only ever lives in each venue's raw `info` blob (or a separate endpoint).

---

## Breaker 1 — `info.get("pnl")` (per-order realized P&L)

**Verdict: CONFIRMED** — `info.pnl` is OKX-specific; there is no cross-exchange unified per-order P&L.

Per-venue reality:

| Venue | Where per-order/-fill realized P&L lives | Field |
|---|---|---|
| OKX | order `info` blob | `info.pnl` (string); `info.fillPnl` WS-only |
| Hyperliquid | order/fill `info` blob | **`info.closedPnl`** (string) — *different key* |
| Binance USDⓈ-M | `fetch_my_trades` → trade `info` (NOT lifted to unified) | `trade.info.realizedPnl`; or income history `fapiPrivateGetIncome` type `REALIZED_PNL` |
| Bybit | **NOT in trades** — `fetch_positions_history` (`/v5/position/closed-pnl`) | position `closedPnl` / `curRealisedPnl` |
| Coinbase | none (spot) | — |

Evidence: `okx.py:3827,3904` (`info.pnl`); `hyperliquid.py:3075,3294` (`info.closedPnl`);
`binance.py:4859,4935` (trade `realizedPnl`, and `parse_trade` return at 5025 does **not** lift it —
stays in `info`); `bybit.py:6397` `closedPnl` appears in **`parse_position`** (6274), never in
`parse_trade` (2895); `bybit.py:8486 fetch_positions_history`.

**Revised fix.** The plan's normalization mandate is right, but its example (§113) — *"Binance/Bybit from
`fetchMyTrades` / income endpoints"* — is **wrong for Bybit**. Correct mapping the adapter must implement:

- OKX → `info.pnl`
- Hyperliquid → `info.closedPnl`  ← **missing from the plan; a bare `info.get("pnl")` returns `None` here (silent $0), not an error**
- Binance → `trade.info.realizedPnl` (fetchMyTrades) or income history
- Bybit → `fetch_positions_history` / closed-pnl endpoint (**not** fetchMyTrades)

---

## Breaker 2 — `info.get("reduceOnly")` (reduce-only flag)

**Verdict: CONFIRMED** — `reduceOnly` *is* a unified top-level field; reading it via `order.get("reduceOnly")`
is the correct venue-neutral access.

`safe_order` always emits `reduceOnly` (base 4422). It is populated by `parse_order` for the derivatives
venues: OKX (`okx.py:4059`), Binance (`binance.py:6159`), Bybit (`bybit.py:3786`), Hyperliquid
(`hyperliquid.py:3196`). **Coinbase `parse_order` does not set it → `None`** (`coinbase.py:3204`; its
`reduceOnly` handling at 2912 is request-side only) — expected, Coinbase is spot.

**Current-code gap.** `ccxt_adapter.py:831` still does a **bare `info.get("reduceOnly")`** — it does NOT yet
read the unified top-level field. It works on OKX only because OKX's raw `info` key is coincidentally also
`reduceOnly` (a string `'true'`/`'false'`). The plan's prescribed fix — `order.get("reduceOnly")` with an
`info.reduceOnly` fallback (§98) — is correct and still **needs to be applied** (line 831 is not yet
venue-neutral). Note OKX's value is a string, so downstream truthiness must handle `"false"` (the adapter
passes it through raw today).

---

## Breaker 3 — position-level `realizedPnl`

**Verdict: DISPROVEN as a unified field / PARTIALLY CONFIRMED as available.**

`realizedPnl` is **not** part of the base position structure (`base/exchange.py::parse_position` computes
only `unrealizedPnl`). It is set only by exchanges that opt in:

- OKX `parse_position` → `realizedPnl` (`okx.py:5969`)
- Bybit `parse_position` → `realizedPnl` from `curRealisedPnl`/`closedPnl` (`bybit.py:6493`)
- Binance `parse_position` → **absent** (only `unrealizedProfit`)
- Hyperliquid `parse_position` → **absent** (only `unrealizedPnl`; closedPnl is per-fill)

**Impact on the plan.** Phase 2's verification step (§166-171) compares the ledger sum against the
"position-level `realizedPnl` reported by `health()`". Two problems:
1. Not venue-neutral — only OKX/Bybit expose it, so the check is OKX/Bybit-only (fine for the OKX-first
   verification, but must be documented as such, not implied as portable).
2. **`get_positions()` / `health()` do not currently surface it.** `ccxt_adapter.py:852-876` maps only
   `unrealizedPnl → upl`; it never reads `row.get("realizedPnl")`. The Phase 2 verification is
   **not executable today** without a one-line adapter change.

---

## #4 — `fetch_positions_history` / closed-P&L endpoints

**Supported by OKX and Bybit only** (of the five): `has['fetchPositionsHistory']=True` and a real
`def fetch_positions_history` exist in `okx.py` and `bybit.py`. **Binance, Hyperliquid, Coinbase do not
implement it.** Venue-specific equivalents: Binance → income history (`fapiPrivateGetIncome`,
`REALIZED_PNL`; `binance.py:10848`); Hyperliquid → user-fills `closedPnl`. This is **not** a portable
primitive — do not build the ledger's realized-P&L path on `fetch_positions_history`.

## #5 — OKX `info` blob structure (verified)

- `info.pnl` — present, **string** (`okx.py:3827,3904,4130,4287`).
- `info.reduceOnly` — present, **string** `'true'`/`'false'` (`okx.py:4030`).
- `order['fee']` (top-level, unified) — **dict** `{cost, currency}` via `safe_value(order,'fee')`; OKX
  populates it. The adapter's `isinstance(fee, dict)` guard (`ccxt_adapter.py:828-829`) is correct.
  OKX `fee.cost` is **negative** for paid fees (`okx.py:2405`) — the plan's `net = pnl + fee` (§155) is the
  right sign convention *for OKX*; it must be re-verified per venue (Binance fee sign differs by endpoint).

---

## New breakers discovered

- **N1 (highest).** Hyperliquid per-order P&L key is **`info.closedPnl`**, not `info.pnl`. A bare
  `info.get("pnl")` returns `None` → the ledger would silently record **$0 realized P&L** on every
  Hyperliquid close (no error, no log). This is the concrete cross-venue trap the plan's abstract
  "abstract it in the adapter" language does not yet spell out.
- **N2.** `get_positions()`/`health()` do not expose position `realizedPnl` — Phase 2's verification is
  blocked until the adapter reads `row.get("realizedPnl")` into the position dict.
- **N3.** The ledger's **reduce-only gate is a derivatives-only assumption.** Coinbase (and any spot venue)
  has `reduceOnly=None`, so `reconcile_from_order_history`'s "row is reduce-only" filter (§95-99) drops
  **every** spot close. The gate must be documented as derivatives-scoped, or spot venues need a different
  close-detection signal.
- **N4 (cosmetic).** CCXT OKX has a latent bug: the reduce-only null-guard tests the wrong variable
  (`okx.py:4031` checks `reduceOnly` not `reduceOnlyRaw`). Harmless for us; noted for completeness.

---

## Revised confidence

**Venue-neutrality claim: 9.3 / 10 as written; 9.6 achievable after four edits.**

The architecture (adapter normalizes; ledger consumes one field) is sound and already the plan's design.
The score is held below 9.6 by three factual/coverage gaps, not by a design flaw. To reach 9.6, fold in:

1. **Fix the Bybit realized-P&L source** in §113 → `fetch_positions_history` (closed-pnl), not fetchMyTrades.
2. **Add the Hyperliquid `info.closedPnl` mapping** to the adapter's normalization table (N1).
3. **Surface position `realizedPnl` in `get_positions()`** so Phase 2's verification is executable (N2),
   and mark the check OKX/Bybit-only.
4. **Document the reduce-only gate as derivatives-only** and note spot venues need a separate close signal (N3).

Breaker 2's prescribed fix is correct but **still unapplied** at `ccxt_adapter.py:831` — apply it as part of Phase 1.
