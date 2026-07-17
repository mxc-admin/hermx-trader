# Plan: Rework `side` — strategy-capability gate, `action`-driven direction

> Status: **SHIPPED (2026-07-16 note)** — W2's strategy gate landed as `side_policy`
> (`long_only`/`short_only`/`long_short`) and W1's legacy removal landed: alert-level `side`
> is dropped, `action` is the sole direction field (invalid action still returns the legacy
> `side_not_allowed` error string). Historical plan below, produced via CC2 codebase analysis, 2026-07-09.
> Companion doc: `docs/EXECUTION_STRATEGY_DESIGN.md` (§3.2, §4) already specifies the strategy-level `side` gate this plan adopts and extends.

## 0. Key finding up front

The codebase **already routes direction through `action`** internally. `normalize()` (`src/signals/normalize.py:64-88`) treats `action` as primary and only *falls back* to a raw alert `side` for legacy alerts; it then writes a derived `side = action` mirror into the normalized record. So:

- **Alert-level `side` is already vestigial** — it exists only as a legacy alternate spelling of `action` (`buy`/`sell`), plus a conflict-detection gate that exists *solely because* two competing inputs exist.
- **`normalized["side"]` is consumed widely downstream** (dedupe, dashboard, journal, advisor) — but those consumers read the *derived* buy/sell mirror, not an alert field. They stay correct as long as we keep deriving that mirror from `action`.
- **There is a fully-specified design** for the *new* strategy-level `side` gate in `docs/EXECUTION_STRATEGY_DESIGN.md` (§3.2, §4, schema-v3 block at :251-256). This plan adopts that design and additionally does the legacy-removal the design doc left out.

The task therefore splits cleanly into two independent workstreams:
- **W1 — Legacy removal:** stop deriving direction from an alert `side` field; `action` becomes the sole direction input.
- **W2 — New capability gate:** add strategy-level `side ∈ {long-only, short-only, long-short}` enforced per-leg.

---

## 1. Every current `side` usage, categorized

### (a) Legacy signal-direction usage — TO REMOVE / REWORK

| Location | What it does |
|---|---|
| `src/signals/normalize.py:64-74` | Reads `raw_side` from payload; `elif raw_side in _valid_open: action = raw_side` derives **action from alert side**; `side = action if action in _valid_open else raw_side`. This is the core legacy derivation. |
| `src/signals/normalize.py:88, 106-107` | Writes `normalized["side"]`; drops it when empty. (Keep the *derived* mirror, but re-source purely from `action`.) |
| `src/webhook_receiver.py:1427-1437` | `action_side_conflict` 400 gate — exists **only** because alert `action` and alert `side` are competing inputs. Becomes dead once `side` is not a direction input. |
| `src/webhook_receiver.py:553` (`ALLOWED_SIDES = {"buy","sell"}`) + `:1447` gate | Gates open alerts on `normalized["side"] ∈ {buy,sell}`. Rework to gate on the `action`-derived direction. |
| `src/webhook_receiver.py:1523` | `direction = "long" if normalized.get("side") == "buy" else "short"` — direction from side. Re-source from `action`. |
| `src/strategy/readiness.py:95` | Same `direction = "long" if normalized.get("side")=="buy" else "short"`. Re-source from `action`. **This is the authoritative execution-direction derivation** (feeds `execution_intent.actions`). |
| `src/skills/hermes_execution.py:39, 118-119` | `_SIDE_TO_DIRECTION`; `side = signal.side or signal.action` — reads signal `side` first. Flip to `action`-only. |
| `schemas/tradingview-alert.schema.json:15-18, 34-37` | `anyOf [{required:[side]},{required:[action]}]` + `side` enum `[buy,sell]`. Rework to require `action`. |

### (b) Strategy-capability / gating usage — TO INTRODUCE (none exists yet)

- **No strategy file or loader reads `side` today** (confirmed: zero hits in `strategies/*.json`, `src/strategy/records.py`, `src/dashboard.py:load_strategy_files`). This is a pure gap — W2 is additive.
- Enforcement seam already identified by the design doc: `src/strategy/readiness.py:188-189` builds `execution_intent.actions = ["CLOSE_OPPOSITE_IF_ANY", "OPEN_<dir>"]` — the correct place to *strip* the disallowed `OPEN_*` leg.
- Ledgering seam: `src/execution/service.py` gate/`gate` field (design doc §2 row references `service.py:131`) — record `mode="side_restricted"`.

### (c) Test usage

Direction-input tests (must update):
- `tests/test_action_close_intake.py` — the whole file is about `action` vs `side` intake: `test_action_conflict_returns_400`, `test_action_buy_routes_identically_to_side_buy`, close-carries-no-side assertions (`:33-103`).
- `tests/fixtures/alerts/strategy/*.json` — `btcusdt_buy.json`, `solusdt_sell.json`, `btcusdt_sell_reverse.json`, `ethusdt_buy.json`, `xrpusdt_buy.json`, etc. all send `"side": "buy|sell"` with **no `action`**. These are the operator-template stand-ins.
- `tests/test_phase6_alert_schema_m2.py` (13 side refs) — exercises the alert schema incl. the `anyOf side|action` and enum.
- `tests/test_phase5_normalization_cleanup.py`, `test_characterization_strategy_matching.py`, `test_phase_b_robustness.py`, `test_replay_startup.py` — assert on `normalized["side"]` / direction.

Venue/position `side` tests (NOT affected — different meaning, keep as-is): `tests/test_pnl_ledger.py` (36), `tests/test_ccxt_adapter.py` (31), `tests/fixtures/okx_query/*.json`, `test_order_state_machine.py`, `test_reconciliation_observe_only.py` — these `side` values are exchange position/fill long/short, unrelated to the alert field.

### (d) Schema / docs usage

- `schemas/tradingview-alert.schema.json` — alert `side` (W1).
- `schemas/strategy.schema.json` — **no `side` today**; add it (W2).
- `docs/EXECUTION_STRATEGY_DESIGN.md` — §3.2, §4 (:251-256) canonical design for the new gate; §2 table row on reversal/per-leg. Update to mark as shipped + add `short-only`.

### Downstream consumers of `normalized["side"]` — KEEP (re-sourced from `action`, no behavior change)
`src/signals/dedupe.py:33,39,81,134`; `src/advisor.py:73`; `src/orders/journal.py:433`; `src/dashboard/snapshots.py`, `dashboard/model.py:252`, `dashboard/render.py:75-79`; `src/strategy/readiness.py:170,182,188`. These read the derived buy/sell mirror and remain correct.

---

## 2. Proposed strategy-level `side` schema

**Location:** `schemas/strategy.schema.json`, `strategy_v2` `properties` (top-level, alongside `leverage`/`margin_mode`). Top-level, **not** per-instrument — a v2 strategy is single-instrument, and directional capability is a strategy property.

```jsonc
"side": {
  "type": "string",
  "enum": ["long-only", "short-only", "long-short"],
  "default": "long-short",
  "description": "Directions this strategy may OPEN. 'long-short' (default, = today's behavior): both. 'long-only'/'short-only': the disallowed OPEN_* leg is STRIPPED from execution_intent.actions before submission and ledgered as mode='side_restricted' — never rejected. The CLOSE_OPPOSITE_IF_ANY leg and all action=close/sl/tp exits ALWAYS execute regardless of this field."
}
```

**Validation rules:**
- Optional; absence ⇒ `long-short` ⇒ byte-identical to current behavior (zero migration for existing files).
- `additionalProperties:false` already holds in `strategy_v2`; adding the property is sufficient. Do **not** add to `required`.
- Adds `short-only` to the doc's `[long-only, long-short]` — symmetric and free.

**What happens when `action` conflicts with allowed side** (recommended — matches design doc §3.2 and the never-block-a-close invariant):

> **Not a rejection. A ledgered per-leg strip, HTTP 200.**
> A `long-only` strategy receiving `action=sell`:
> - **Keeps** `CLOSE_OPPOSITE_IF_ANY` (flattens an existing long — an exit, always allowed).
> - **Drops** `OPEN_SHORT` from `execution_intent.actions`.
> - Records `decision.mode="side_restricted"` / a `gate` entry; row persists (observe-not-drop).
> - Returns **200** with the trade processed as a close-only (or a no-op if flat).

Rationale for **not** using a 4xx/quarantine: a disallowed-direction alert is a *policy* outcome, not a malformed alert; and hard-rejecting it would block the close leg, violating the "never block a close / `reducing`-not-`HALTED`" invariant recorded in the code-quality rules. This is exactly the per-leg semantics the design doc prescribes (`readiness.py:189`, ledgered skip at `service.py`).

*(Alternative considered and rejected: 202 quarantine like `strategy_symbol_mismatch`. Rejected because it would discard the risk-reducing close leg.)*

---

## 3. Exact legacy-removal plan (W1)

| Call site | Current | Replacement |
|---|---|---|
| `normalize.py:64-74` | `action` primary, `raw_side` fallback derives action; `side` derived from either | Derive `action` from `action` **only** (`buy`/`sell`/`close`). Keep a thin edge alias `action = action or raw_side` **only** for the deprecation window (see §5), emitting a deprecation log. `side` field of the normalized record = `action if action in {buy,sell} else ""` (pure mirror of action). |
| `normalize.py:79-82` | signal_id hashes on `action` | No change (already action-based ✅). |
| `webhook_receiver.py:1427-1437` | `action_side_conflict` 400 gate | **Delete.** With `side` no longer an independent input there is nothing to conflict. (Removes the whole branch + its test.) |
| `webhook_receiver.py:1447` + `ALLOWED_SIDES` | gate on `normalized["side"]` | Gate on the `action`-derived open direction: reject open alerts whose `action ∉ {buy,sell}` (after the `action=close` branch at `:1442` returns). Rename gate/error to reflect action (`action_not_open`), or keep `side_not_allowed` string for API stability (call out in §4). |
| `webhook_receiver.py:1523` | `direction = ... side=="buy"` | `direction = "long" if normalized["action"]=="buy" else "short"`. |
| `strategy/readiness.py:95` | `direction = ... side=="buy"` | `direction = "long" if normalized["action"]=="buy" else "short"`. |
| `skills/hermes_execution.py:118-119` | `side = signal.side or signal.action` | `action = str(signal.get("action") or "").lower(); direction = _SIDE_TO_DIRECTION.get(action,"")`. Rename `_SIDE_TO_DIRECTION`→`_ACTION_TO_DIRECTION` (keep buy→long, sell→short). |
| `schemas/tradingview-alert.schema.json:15-18,34-37` | `anyOf[side|action]`, `side` enum | Require `action` (`["buy","sell","close"]`). During deprecation window keep `side` as an accepted-but-deprecated alias property (still enum `[buy,sell]`) and keep `anyOf`; after the window, drop `side` and move `action` into top-level `required`. |

Everything reading `normalized["side"]` downstream is untouched — it keeps receiving the action-derived buy/sell mirror.

---

## 4. Backward compatibility

1. **Existing `strategies/*.json`** — none has a `side` field; the schema default `long-short` reproduces today's behavior exactly. The four live duo-base strategies *reverse* (long↔short), so `long-short` is the correct default for them. **Zero file migration.** ✅
2. **Operator TradingView templates sending `"side":"buy|sell"`** — cannot be edited atomically across all operators. Do **not** hard-break them. Keep an edge alias (`action = action or side`) + deprecation warning for a bounded window (§5), so a `side`-only alert still routes as the equivalent `action`. Track a metric/log of `side`-only alerts to know when it's safe to drop.
3. **Existing tests / fixtures referencing alert `side`** — `tests/fixtures/alerts/strategy/*.json` are `side`-only. Either (a) keep them as the *deprecation-path* fixtures asserting the alias still works, and add parallel `action`-based fixtures; or (b) migrate them to `action` and add one explicit legacy-alias regression fixture. Recommended: (a) — retains coverage of the compat shim you're relying on.
4. **API response strings** — some tests assert `error=="side_not_allowed"` / `"action_side_conflict"`. Deleting the conflict gate removes `action_side_conflict` (update `test_action_close_intake.py`). Decide whether to preserve `"side_not_allowed"` string for external stability; recommend keeping the string, changing only its trigger to action.
5. **`normalized["side"]` in persisted ledgers/pipeline rows** — unchanged key/values (still buy/sell), so replay of historical `raw-webhooks.jsonl` and dashboards keep working.

---

## 5. Migration / rollout strategy — **recommended: additive field + bounded deprecation alias (no big-bang)**

Two schemas, two different moves:

**Strategy schema (W2): additive, no version bump needed.**
`side` is optional with a behavior-preserving default. Adding an optional property to `strategy_v2` is backward-compatible; existing files validate unchanged. The design doc frames this under a notional "schema_version 3," but since the field is optional-with-default, a **version bump is not required** — you may keep `schema_version:2`. (If other v3 features from the design doc ship together, bump then; for `side` alone, additive is lowest-risk.)

**Alert schema (W1): two-phase deprecation, not big-bang.**
- **Phase 1 (this change):** `action` is the sole direction driver internally; delete the conflict gate and all side→direction derivation; **keep** `side` as an accepted deprecated alias at the edge with a dedup deprecation log + counter. Schema still `anyOf[side|action]`. No operator is broken.
- **Phase 2 (later, after the counter shows `side`-only alerts ≈ 0):** drop the `side` alias and the schema property; require `action`.

**Why this over the alternatives:**
- *Feature flag* — unnecessary; the strategy default makes W2 inert until an operator opts in, and W1's alias is itself the safety valve.
- *Big-bang cutover* — would break every operator's TradingView template the instant it ships (they cannot all be re-saved simultaneously). Rejected.
- *Schema version bump for the alert* — heavier than needed; the `anyOf` already tolerates both shapes during Phase 1.

This is the lowest-risk path: W2 ships behavior-neutral behind a default; W1 removes the legacy *logic* immediately while preserving the legacy *wire alias* just long enough to migrate operators.

---

## 6. Edge cases & concrete test cases

**W1 (action-driven direction):**
- `action=buy`, no `side` → `direction=long`, submits (new happy path).
- `action=sell`, no `side` → `direction=short`.
- `action=close`, no `side` → routes to `_build_close_record`, 200, `"side" not in normalized` (existing `test_action_close_matched_strategy_returns_200`).
- `side=buy`, no `action` (legacy template) → alias derives `action=buy`, routes identically; **assert deprecation log emitted** (replaces `test_action_buy_routes_identically_to_side_buy`).
- `action=buy` + `side=sell` (old conflict case) → **no longer 400**; `action` wins → `direction=long`. Update/delete `test_action_conflict_returns_400`.
- Garbage `action` (e.g. `"foo"`), no side → open gate rejects (`action_not_open`/`side_not_allowed`), 400.
- No `action`, no `side` → schema `anyOf` fails / gate rejects.
- `signal_id` determinism unchanged (already action-hashed) — regression test.

**W2 (strategy side gate):**
- `long-only` + `action=buy` → `OPEN_LONG` submits normally.
- `long-only` + `action=sell` while **flat** → `OPEN_SHORT` stripped; only `CLOSE_OPPOSITE_IF_ANY` remains → no position opened → journaled no-op, `mode="side_restricted"`, HTTP 200.
- `long-only` + `action=sell` while **long open** → close leg flattens the long, open leg stripped → net flat, ledgered as `side_restricted`. **(Critical: exit must still work.)**
- `short-only` symmetric mirror (both above with sides flipped).
- `long-short` (default / absent field) → both legs pass, identical to today (characterization test — byte-compare `execution_intent.actions`).
- `action=close` / `sl` / `tp` under `long-only` → **always** executes fully (never gated) — the never-block-a-close invariant.
- Invalid `side` value in a strategy file (e.g. `"long"`) → schema validation error at load (`load_strategy_files`).
- Round-trip: `side_restricted` open produces **no** `closed-trades.jsonl` open row but does not corrupt P&L attribution (`pnl_strategy_map`).

**Fixtures to add:** `strategies/`-style test strategy with `side:"long-only"`; alert fixtures pairing an `action=sell` against it.

---

## 7. Ordered list of files to touch

**W1 — legacy removal (do first; behavior-neutral for `action` alerts):**
1. `schemas/tradingview-alert.schema.json` — keep `anyOf` + deprecate `side` (Phase 1); add description marking `side` legacy.
2. `src/signals/normalize.py` — direction/action derived from `action` only; `side` alias reduced to edge back-compat with deprecation log; `side` field = pure action mirror.
3. `src/webhook_receiver.py` — delete `action_side_conflict` gate (`1427-1437`); re-base the open gate (`1447`) and `direction` (`1523`) on `action`; keep/rename `ALLOWED_SIDES`.
4. `src/strategy/readiness.py` — `direction` from `action` (`:95`).
5. `src/skills/hermes_execution.py` — `_SIDE_TO_DIRECTION`→`_ACTION_TO_DIRECTION`; read `action` only (`:118-119`).
6. `tests/test_action_close_intake.py` — update conflict + alias tests; add deprecation-log assertion.
7. `tests/fixtures/alerts/strategy/*.json` + `tests/test_phase6_alert_schema_m2.py`, `test_phase5_normalization_cleanup.py`, `test_characterization_strategy_matching.py` — add `action`-based fixtures; keep one legacy-`side` alias regression.

**W2 — new capability gate (additive, default-neutral):**
8. `schemas/strategy.schema.json` — add optional `side` enum (`strategy_v2`).
9. `src/strategy/readiness.py` — read `strategy["side"]` (default `long-short`); strip disallowed `OPEN_*` from `execution_intent.actions` (`:188-189`), keeping `CLOSE_OPPOSITE_IF_ANY`; set `mode/decision="side_restricted"`.
10. `src/execution/service.py` — ledger the `side_restricted` skip in the `gate` field (observe-not-drop).
11. `src/dashboard/*` (optional, follow-up) — surface allowed-side + `side_restricted` on the strategy card.
12. `docs/EXECUTION_STRATEGY_DESIGN.md` — mark §1/#1 and §4 side-gate shipped; add `short-only`.
13. **New tests:** `tests/test_strategy_side_gate.py` — the W2 matrix in §6; a `strategies/`-style `long-only` fixture.

**Dual-file / rules note:** if any pattern here becomes a durable lesson (e.g. "alert `side` is a deprecated alias, not a direction input"), record it in **both** `.claude/rules/code-quality.md` and `.windsurf/rules/code-quality.md` per the dual-file rule.

---

Per this project's dev-rules ("describe your approach first and wait for approval"; "changes touching >3 files must be broken into smaller tasks"), this is planning only — no files were edited when this doc was produced. W1 and W2 are independently shippable; recommendation is to land **W1 first** (removes the legacy seam, behavior-neutral for `action` traffic) then **W2** (additive gate).
