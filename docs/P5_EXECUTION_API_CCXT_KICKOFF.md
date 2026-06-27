# Phase 5 Kickoff Checklist: Execution API + CCXT

This is the implementation checklist for Phase 5 from `REFACTOR_PLAN.md`.

Goal: ship a controlled execution architecture where Hermes uses a skill, the skill calls a HermX-controlled API, and that API uses CCXT for exchange execution and polling.

## Non-Negotiable Safety Invariants

- Kill switch precedence remains first (`HERMX_SUBMIT_ENABLED`).
- Submit is blocked unless all gates are affirmative.
- `PLANNED` and `SUBMITTED` are journaled before exchange submission.
- Stable client-order-id dedupe is enforced before submit.
- `UNKNOWN` is first-class and always reconciled.
- Per-exchange credentials are isolated and fail closed.
- CCXT is transport only; risk policy stays in HermX code.

## Flags (Default-Safe)

- `HERMX_SUBMIT_ENABLED` = `false|true`
- `HERMX_EXEC_API` = `legacy|service`
- `HERMX_EXEC_BACKEND` = `okx_demo|ccxt|...`
- `HERMX_EXEC_WRITE_BACKEND` = `legacy|ccxt`
- `HERMX_EXEC_SHADOW` = `false|true`

Any unset or ambiguous state must resolve to no-submit.

## Slice Plan

### P5-00: Prerequisites (No behavior change)

- [ ] Pin `ccxt` in project dependencies.
- [ ] Add `skills/hermes-execution.md` and align with gate precedence.
- [ ] Ensure namespaced exchange credentials are documented in `setup/env.example`.

Acceptance:

- [ ] `import ccxt` works in project runtime.
- [ ] Hermes execution skill spec exists and is review-approved.

### P5-01: Extract `ExecutionService` (Parity refactor)

- [ ] Add `src/execution/service.py` with `ExecutionService.submit(record)`.
- [ ] Move gate checks, idempotency pre-checks, order state journaling, and outcome mapping from `execute_okx_if_enabled` into service.
- [ ] Keep legacy inline path behind `HERMX_EXEC_API=legacy`.

Touchpoints:

- `src/webhook_receiver.py`
- `src/execution/service.py`

Acceptance:

- [ ] Legacy mode behavior unchanged.
- [ ] Service mode parity passes targeted characterization tests.

### P5-02: Route submit through adapter seam

- [ ] In service mode, call `ExecutorFactory.create(...).execute(...)` instead of hardcoded submit subprocess path.
- [ ] Keep risk gates and journaling above adapter boundary.

Touchpoints:

- `src/execution/service.py`
- `src/webhook_receiver.py`
- `src/executors/factory.py`

Acceptance:

- [ ] Active submit path no longer directly shells `okx_demo_executor.py` from `webhook_receiver.py`.
- [ ] Existing order-state and idempotency tests remain green.

### P5-03: CCXT read adapter (Observe-only first)

- [ ] Add `src/executors/ccxt_adapter.py` implementing read/query methods.
- [ ] Register `ccxt` backend in factory.
- [ ] Normalize read outputs to canonical executor shapes.
- [ ] Add method fallbacks for venue gaps and document them.

Touchpoints:

- `src/executors/ccxt_adapter.py`
- `src/executors/factory.py`
- `src/webhook_receiver.py` (reconcile adapter selection)

Acceptance:

- [ ] Reconciliation runs via CCXT read path in observe-only mode.
- [ ] Canonicalized read outputs are parity-equivalent to legacy adapter.

### P5-04: CCXT write path + shadow parity

- [ ] Implement `execute()` in CCXT adapter for submit/cancel/amend.
- [ ] Keep client-order-id and journal dedupe checks above adapter.
- [ ] Add shadow parity ledger for legacy vs CCXT outcome comparison.
- [ ] Cut over write path only after parity soak passes.

Acceptance:

- [ ] Shadow soak shows normalized-equivalent outcomes.
- [ ] Kill switch blocks submit regardless of backend.

### P5-05: Hermes execution skill as only agent-facing surface

- [ ] Finalize Hermes execution skill contract and examples.
- [ ] Ensure agent execution calls only controlled API (`ExecutionService`).

Acceptance:

- [ ] No agent path bypasses API/risk layer.

### P5-06: Dashboard read path through adapter seam

- [ ] Replace direct `okx_demo_executor.py` dashboard subprocess reads with factory/adapters.
- [ ] Preserve explicit operator-visible error banners on adapter failure.

Touchpoints:

- `src/dashboard.py`
- `src/executors/factory.py`

Acceptance:

- [ ] Dashboard has no direct active-path `okx_demo_executor.py` subprocess reads.

### P5-07: Contract cleanup and dead code removal (last)

- [ ] Enforce normalized execution payload names across API/adapter boundaries.
- [ ] Remove dead `src/executors/base_executor.py` and orphaned imports only after rollback drill passes.

Acceptance:

- [ ] Full suite green on CCXT-capable path.
- [ ] Rollback ladder drill validated.

## Rollback Ladder

1. Set `HERMX_SUBMIT_ENABLED=false`.
2. Set `HERMX_EXEC_WRITE_BACKEND=legacy`.
3. Set `HERMX_EXEC_API=legacy`.
4. Keep shadow comparison running until stable.

## Phase 5 Done Definition

- [ ] Receiver, reconcile, and dashboard exchange I/O run through controlled API + adapter seam.
- [ ] Hermes execution skill is documented and active.
- [ ] CCXT read and write paths pass parity soak and conformance tests.
- [ ] Credential isolation and fail-closed behavior are proven in tests.
- [ ] Legacy/dead execution surfaces removed only after rollback proof.
