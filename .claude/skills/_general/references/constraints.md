# Constraints & Limitations
<!-- Populated by /learn. Framework limits, library behaviors, platform quirks. -->

### No close-only execution intent exists in HermX today
`build_strategy_execution_readiness` only produces open intents (`CLOSE_OPPOSITE_IF_ANY` → `OPEN_<dir>`). A standalone close does not exist. `hermx-control` SKILL.md explicitly says "Close is not supported yet." Any Telegram-instructed close requires new Python gate code first.

### Hermes Curator auto-creates and auto-patches skill files
After complex tasks (5+ tool calls), Hermes autonomously creates/patches SKILL.md files. Git-version `skills/` and review Curator diffs before staging to prevent silent advisory quality rot (degraded veto quality without money-safety failure).

### Advisory quality rot: gate chain doesn't validate skill prose
`ExecutionService` only reads advisor output (proceed/skip/unknown). It never reads the skill prose that produced it. A Curator rewrite can silently degrade veto logic (e.g. "elevated → skip" becomes "high-only → skip") with no alert from the gate chain.

### symbol_pauses has no operator API endpoint — set only by reconcile resolver
There is no API endpoint to manually set `symbol_pauses`. The only writer is the unknown/reconcile resolver (`UNKNOWN_RESOLVER_TIMEOUT` path). Operator clears it manually by editing `control-state.json`.

### Two MXC consumers will drift
The HermX Python health gate already reads MXC (`pp_acc`/`pp_vel`, `tab-health.jsonl`). The `dashboard-risk` skill will read the same source independently. Two consumers with different parse, cache, and timing will diverge. Accepted as a known trade-off; optionally serve the skill from HermX's `/api` view later.

### Receiver caches STRATEGIES at module import — never re-reads strategies/
`webhook_receiver.py` (~line 525) loads `STRATEGIES` once at import. The directory is never re-read at runtime, so strategy file edits require an explicit reload (or process restart) to take effect. This is a constraint, not a pattern.

### PROCESS_QUEUE is in-memory only — systemd empties it on restart
The process queue holds no durable state. systemd `Restart=always` / `RestartSec=5` drains it within ~5s of any crash. Durability comes only from replaying `raw-webhooks.jsonl` at startup.

### HERMX_LIVE_TRADING is the hard floor — no UI toggle overrides it
The `HERMX_LIVE_TRADING` env var gates live entries at the lowest level. Per-strategy mode pills and dashboard toggles cannot promote to live when the env floor is false.

### TradingView treats non-2xx as delivery failure
Rejecting at `do_POST` (non-2xx) causes TradingView to retry — producing duplicate-delivery noise, not added safety. Accept (2xx) and gate downstream instead.

### Strategy schema uses additionalProperties: false
The strategy JSON schema forbids unknown keys. Adding provenance/metadata fields requires a schema change first — you cannot smuggle extra fields into a strategy file.

### Two strategies on the same inst_id fight over one netted position
The exchange nets per `inst_id`; the dashboard keys positions by symbol. Two strategies on the same instrument contend over a single netted position with no per-strategy separation.

### "ccxt" is a backend name, not a venue name
`CcxtExecutor._exchange_id()` resolves `ccxt_exchange or exchange or "okx"`. Setting `exchange="ccxt"` (the backend) without `ccxt_exchange="okx"` (the venue) causes `getattr(ccxt, "ccxt")` → `None` → `ValueError("unsupported_ccxt_exchange:ccxt")`. Always distinguish backend from venue in execution config.

### Empty shadow_config() breaks dashboard executor (historical — dead code)
(Historical — dead code.) After reducing `dashboard_core.shadow_config()` to `return {}`, `_dashboard_executor` lost its source for `ccxt_exchange` default. The fallback logic (`exchange="ccxt"`) masked the built-in `"okx"` default because `"ccxt"` was truthy. Removing a config source requires auditing all consumers for masked defaults.

### engine-config.json is gitignored — cannot be COPY'd in Dockerfile
`engine-config.json` is listed in `.gitignore:7`. It is generated at runtime, not tracked. Any Dockerfile step that `COPY engine-config.json` will fail on a fresh clone or CI checkout. Use the tracked `config/runtime.demo.json` (identical defaults) instead.

### Docker named volume initialization copies ownership from image mount-point
When Docker creates a named volume for the first time, it copies the ownership and permissions of the image's mount-point directory into the volume. The Dockerfile must `mkdir -p /app/data /app/logs && chown -R hermx:hermx /app` before `USER hermx` so fresh volumes are writable by the non-root process without manual host chown.
