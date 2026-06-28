# Executor-output fixtures (historical — legacy `okx_demo_executor` module removed)

> **Historical note.** These four JSON files are artifacts from the now-removed legacy
> `okx_demo_executor` module. `src/okx_demo_executor.py` was **deleted** in the CCXT cutover
> (execution is now unconditionally CCXT via `src/execution/service.py` +
> `src/executors/ccxt_adapter.py`), and the `REFACTOR_PLAN.md` that originally scoped this work
> no longer exists in the repo. The references below are kept only to explain the fixtures'
> provenance; do not treat those paths as live.

These four JSON files captured the **shape** of the legacy `okx_demo_executor` stdout as it was
once consumed by `execute_okx_if_enabled` (parsing stdout JSON for `mode` + `okx_fill_summary`).

They are intentionally **minimal, synthetic placeholders**. They were never wired into a live
test (a faithful mock-OKX component test / partial-fill reconciliation would have required the
mock-OKX REST layer). They remain committed (and hash-stamped) as a stable historical oracle.

Outcomes covered: `fill.json`, `partial_fill.json`, `reject.json`, `timeout.json`.

NO secrets. All ids/prices are synthetic.
