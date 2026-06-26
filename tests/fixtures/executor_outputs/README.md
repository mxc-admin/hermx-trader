# Executor-output fixtures (placeholders — deferred to Phase 1)

These four JSON files capture the **shape** of `okx_demo_executor.py` stdout as
consumed by `execute_okx_if_enabled` (it parses stdout JSON and reads
`mode` + `okx_fill_summary`). See `src/okx_demo_executor.py:617-633`.

They are intentionally **minimal, synthetic placeholders**. Phase 0 / Batch A
does NOT wire them into a live test, because doing so faithfully (mock-OKX
component test, partial-fill reconciliation) requires touching `src/` and the
mock-OKX REST layer — that is P1/P7 work per `REFACTOR_PLAN.md:166` ("captured
`okx_demo_executor` stdout for fill / partial-fill / reject / timeout") and the
P1 reconciliation REST payloads. They are committed now (and hash-stamped) so
P1 inherits a stable oracle rather than inventing its own inputs.

Outcomes covered: `fill.json`, `partial_fill.json`, `reject.json`, `timeout.json`.

NO secrets. All ids/prices are synthetic.
