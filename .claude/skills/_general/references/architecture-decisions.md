# Architecture Decisions
<!-- Populated by /learn. Format: ### [Title] / Decision / Alternatives / Rationale -->

### Safety lives in Python gate code, not skill prose
- **Decision:** Hermes Curator is allowed to evolve/rewrite skill files. No prose locks.
- **Alternatives:** Lock skill files (Curator lockout + SHA pin).
- **Rationale:** `ExecutionService.execute()` is the money-safety gate. Skill prose is non-authoritative advisory guidance. A rewritten skill cannot widen an agent's authority because authority lives in code.

### HermesExecutionSkill → rename to HermesRelayAdapter
- **Decision:** Rename the Python class in `src/skills/hermes_execution.py` and the doc `docs/hermes-execution.md` to "relay adapter".
- **Alternatives:** Delete it (rejected), keep current name.
- **Rationale:** "Skill" collides with Hermes SKILL.md terminology. The Python component is an internal HermX relay adapter, not a Hermes Agent skill. The naming collision causes architectural confusion.

### Close-only path requires new Python gate code
- **Decision:** Operator-instructed close (via Telegram) requires a new reduce-only execution intent + `ExecutionService` close gate before any skill can expose it.
- **Alternatives:** Expose close as a skill capability without new Python gate (rejected — no safety boundary).
- **Rationale:** "Safety in code, not prose" means a close capability must have a Python boundary: reduce-only, must-have-existing-position, fully journaled. A prose-only unlock is unsafe by the project's own principle.

### Kill-switch semantics: closes bypass HERMX_LIVE_TRADING entry block
- **Decision:** When `HERMX_LIVE_TRADING=false`, new entry opens are blocked, but close/flatten operations must still be permitted.
- **Alternatives:** Block all submissions including closes when the kill switch is off.
- **Rationale:** Emergency flatten must work exactly when you've disabled new entries. Blocking closes during the kill-switch defeats its purpose as a "stop new entries" control.

### risk_index_gate_enabled flag on local HermX dashboard
- **Decision:** The toggle to enable/disable MXC risk-index veto lives on the local HermX dashboard (`127.0.0.1:8098`), stored in `control-state.json`, exposed in `GET /api`.
- **Alternatives:** Toggle on the MXC global dashboard (`https://mxc-kinetic-crypto.replit.app/`), or via the `HERMX_ADVISOR_SKILLS` env var.
- **Rationale:** Operator should control their own veto gate locally without depending on an external service. Consistent with the existing `control-state.json` pattern (`symbol_pauses`, `manual_pause`, etc).

### dashboard-risk skill reads risk_index_gate_enabled first
- **Decision:** The `dashboard-risk` skill checks `GET /api` → `risk_index_gate_enabled` before calling MXC. If false → return unknown (fail-open). If true → fetch MXC → evaluate.
- **Alternatives:** Always call MXC regardless of the gate flag.
- **Rationale:** Prevents unnecessary external calls when the gate is disabled. Keeps the skill self-contained and respects operator intent cleanly.

### symbol_pauses is an auto-safety net, not an operator control
- **Decision:** `symbol_pauses` stays as-is; do not remove or refactor.
- **Alternatives:** Remove as an apparently unused operator control.
- **Rationale:** `symbol_pauses` is automatically set by the unknown/reconcile resolver on `UNKNOWN_RESOLVER_TIMEOUT`. It's a self-protection gate, not an operator toggle. There's no API endpoint to set it manually — only the resolver writes to it. Removing it would lose automatic safety on order-reconciliation failures.
