---
trigger: always_on
---
# Code Quality Rules
# Dual-file sync: .windsurf/rules/code-quality.md (no frontmatter) + .claude/rules/code-quality.md (has YAML frontmatter)

## Known Patterns (populated by /learn)
<!-- Entries added here as bugs and patterns are discovered -->

### `.gitignore` is inert for already-tracked files
A file listed in `.gitignore` that is already tracked in git remains tracked — `gitignore` only prevents *new* untracked files from being staged. `git check-ignore <file>` exits 1 for tracked files regardless of `.gitignore` contents. This means operator-edited tracked config files (`engine-config.json`, `strategies/*.json`) will still conflict on `git pull` even if `.gitignore` lists them. The fix is `git rm --cached` (one-time repo change), not adding them to `.gitignore`.

### normalize() is non-deterministic for time-less payloads
When a payload has no `tv_time`, `normalize()` falls back to `now_iso()`, yielding a different `signal_id` each call. On replay this breaks dedupe. Fix: drop time-less payloads on replay (never re-derive their id from wall-clock).

### Dashboard regression when deleting a config source
Deleting a config file or reducing a config function to `return {}` can break downstream consumers that relied on it for defaults. The `_dashboard_executor` set `exchange="ccxt"` when config was empty, but `"ccxt"` is a backend name — not a venue — so `CcxtExecutor._exchange_id()` returned `"ccxt"` instead of falling back to `"okx"`, causing `getattr(ccxt, "ccxt")` → `None`. When removing a config source, audit every consumer for masked defaults, especially where `or` chains mix backend and venue names.

### shadow-config.json is dead code
`src/dashboard_core.py:shadow_config()` returns `{}` as a no-op. The receiver (`webhook_receiver.py:280-284`) sources from `engine-config.json` via `load_engine_config()`. Any installer or build step referencing `shadow-config.json` is stale. Verify with grep before assuming config file relevance.

### Dashboard UI silently broken when dashboard-ui/out not in image
`dashboard.py:2335` resolves `STATIC_DIR = REPO_ROOT / "dashboard-ui" / "out"`. If the Dockerfile omits `COPY dashboard-ui/out`, the `.is_dir()` gate fails and the dashboard falls back to legacy server-rendered HTML with no error. Any Docker image serving the dashboard must include the built `out/` directory.

### Empty bind-mounted directory shadows baked image files
When a host directory is bind-mounted over an image directory (e.g., `./strategies:/app/strategies:ro`), an empty host directory completely replaces the image contents — the baked files are inaccessible. If the operator needs those files, the installer must seed the host directory from the image before the first `docker compose up`.

### Dashboard writes control-state.json but needs writable mount
`dashboard.py:2497-2518` writes `control-state.json` for per-strategy mode overrides. Running the dashboard `read_only: true` without a writable volume mount for `HERMX_DATA_DIR` causes silent write failures — mode toggles appear to work in the UI but do not persist. The compose file must mount `hermx-state:/app/data` (rw) even when `read_only: true` is set on the root filesystem.

## Anti-Patterns (populated by /learn)
<!-- Entries added here as anti-patterns are identified -->

### Tests that re-implement the handler inline instead of calling production code
`test_intake_hardening.py::test_latest_corrupt_returns_503_not_500` re-implements the handler body in the test, so it passes even if production regresses. Tests must exercise the production code path, not a copy of it.

### Tests armed via a legacy config-flag chain
`test_unknown_resolver_controls.py::_armed_config` arms via the legacy config-flag path. If production moves to a different arming mechanism the test still passes against the dead path and masks a regression. Arm tests through the current production path.
