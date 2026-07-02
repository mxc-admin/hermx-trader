---
description: Production-grade, read-only security audit for the HermX trading system — scans the webhook/HMAC auth path, secret handling, credential permissions, Docker/deployment posture, and dangerous code sinks. Runs on stdlib alone and layers in bandit/pip-audit/detect-secrets when installed. Run before a release, after touching src/security, the webhook receiver, Docker/compose, or credential handling, and on a schedule.
---

# /security-audit — HermX Security Audit

Static, **read-only** security audit tailored to this project's threat model: a
crypto-trading webhook receiver that authenticates TradingView alerts (shared
secret + HMAC) and dispatches **real orders** through CCXT adapters. A single
missed check here can mean an unauthorized trade, a leaked exchange key, or a
replayed alert. The audit never mutates the repo — it only reports.

The engine is a self-contained Python script, `scripts/security-audit.py`. It
runs entirely on the **standard library**, so it works in the stdlib-heavy
runtime venv with nothing extra installed. Optional external scanners (bandit,
pip-audit, detect-secrets) are *used when present* and *skipped when absent* —
they never turn a missing tool into a failure.

## Quick start

```bash
# From the repo root, using the project venv:
./.venv/bin/python scripts/security-audit.py            # human report + exit code
./.venv/bin/python scripts/security-audit.py --json     # machine-readable JSON
./.venv/bin/python scripts/security-audit.py --fast     # stdlib-only (skip external tools)

# Enable the full scanner suite (optional, one-time):
./.venv/bin/pip install -r requirements-security.txt
./.venv/bin/python scripts/security-audit.py            # now also runs bandit/pip-audit/detect-secrets
```

Exit code is `0` when nothing meets the fail threshold (default: **HIGH**),
`1` when it does, `2` on a usage/internal error — so it drops straight into CI
or a pre-release gate.

## When to run

- Before cutting a release or publishing a new Docker image.
- After changing `src/security/*`, `src/webhook_receiver.py`, or anything in the
  auth / HMAC / rate-limit path.
- After editing `Dockerfile`, `docker-compose*.yml`, or credential handling.
- After adding a dependency (dependency-CVE surface changes).
- On a schedule (cron / CI) as a standing regression gate — see **Running regularly**.

## What it checks (threat categories)

Each category is a named check; run a subset with `--only <check>` (repeatable).

| # | Check | Threat | Severity of a hit |
|---|-------|--------|-------------------|
| 1 | `static` | Code-level vulns via **bandit** (optional): weak crypto, injection, insecure temp, TLS-off. Scans `src deploy scripts tests`, skips `B101` (asserts). | HIGH sev+conf → HIGH, else MEDIUM/LOW |
| 2 | `deps` | Vulnerable dependencies via **pip-audit** (optional): known CVEs in `ccxt`, `cryptography`, `aiohttp`, … | HIGH |
| 3 | `secrets` | Hardcoded secrets: private keys, AWS/GitHub/Stripe/Slack/Google tokens (CRITICAL), generic `key = "…"` assignments (MEDIUM). Plus **detect-secrets** entropy/keyword scan when installed. | CRITICAL / HIGH / MEDIUM |
| 4 | `dangerous` | Code-execution & deserialization sinks (AST): `eval`/`exec`/`pickle`/`marshal` (CRITICAL), `os.system`/`subprocess(shell=True)`/`yaml.load` (HIGH), `__import__` (MEDIUM). | CRITICAL / HIGH / MEDIUM |
| 5 | `authbypass` | **Webhook/HMAC auth bypass** (AST): timing-unsafe `==`/`!=` on secret/signature/HMAC values (must use `hmac.compare_digest`); asserts `src/security/webhook_auth.py` uses `compare_digest` and has replay-window handling. *Highest-value custom rule — off-the-shelf tools miss a missing replay window.* | HIGH / MEDIUM |
| 6 | `logging` | Secrets written to logs (AST): a secret-named variable passed to `logging.*`/`print` and not wrapped in `redact_secrets(...)`. | HIGH |
| 7 | `perms` | Credential file permissions: `.env`, `HERMX_SECRET.txt`, `control-state.json`, `*.pem`/`*.key` must be `0600` (not group/other-readable). | HIGH / MEDIUM |
| 8 | `docker` | Deployment posture: `Dockerfile` non-root `USER`, no `.env` baked into a layer, no secret `ENV`/`ARG`; compose `privileged`, non-loopback port publishing, missing `cap_drop`. | CRITICAL / HIGH / MEDIUM / LOW |
| 9 | `gitleak` | Secret/state files tracked in git (`.env`, `HERMX_SECRET.txt`, `control-state.json`), committed key material (`*.pem`/`*.key`), `.env` missing from `.gitignore`. | CRITICAL / HIGH / MEDIUM |

Checks 3–9 are **stdlib-only** and always run. Checks 1–2 (and the detect-secrets
pass in 3) require the optional tools and otherwise report `skipped`.

### Threat-model notes specific to HermX

- **HMAC + replay (`authbypass`).** `verify_webhook_hmac` binds the signature to
  `X-Webhook-Timestamp` and rejects requests outside the replay window; the shared
  secret and the HMAC are both compared with `hmac.compare_digest`. This check
  fails closed if that constant-time comparison or the replay window regresses.
  (Freshness for *trading* decisions is separately bounded on `tv_time`, not
  server time — see `CLAUDE.md`.)
- **Least-privilege subprocess env.** `resolve_executor_env` passes only the
  selected exchange's credentials to an executor subprocess. Hardcoded exchange
  keys in source (`secrets` check) would defeat that isolation.
- **Docker legacy fallback.** `docker-compose.host.yml` is the documented legacy
  `network_mode: host` deployment; the `docker` check relaxes the loopback-bind
  and `cap_drop` rules for it (they are advisory there), while holding the primary
  `docker-compose.yml` to the hardened bar.

## The command

```bash
# Full audit (uses optional tools if installed), human report:
./.venv/bin/python scripts/security-audit.py

# Common variants:
./.venv/bin/python scripts/security-audit.py --fast                 # skip external tools
./.venv/bin/python scripts/security-audit.py --json                 # JSON to stdout
./.venv/bin/python scripts/security-audit.py --report audit.json    # write JSON report
./.venv/bin/python scripts/security-audit.py --only secrets --only perms   # subset
./.venv/bin/python scripts/security-audit.py --fail-on medium       # stricter gate
./.venv/bin/python scripts/security-audit.py --fail-on critical     # laxer gate (CRITICAL only)
```

### Flags

| Flag | Effect |
|------|--------|
| `--json` | Emit the JSON report to stdout instead of the human report. |
| `--report PATH` | Also write the JSON report to `PATH`. |
| `--fast` | Skip external tools (`static`, `deps`, and the detect-secrets pass). Stdlib checks still run. |
| `--fail-on {critical,high,medium,low,info}` | Minimum severity that fails the run. Default `high`. |
| `--only CHECK` | Run only the named check(s); repeatable. Valid: `static deps secrets dangerous authbypass logging perms docker gitleak`. |
| `--root DIR` | Audit a different repo root (default: auto-detected via git). |
| `--timestamp` | Include `generated_at` in the JSON. Off by default so output is byte-deterministic. |

## How to read the output

The human report prints a `Checks:` block (each check → `ran` / `skipped` /
`off` / `error`), then findings grouped by check, then a summary:

- **`FAIL`** lines — a **CRITICAL** or **HIGH** finding. These fail the run
  (at the default threshold). Fix before shipping.
- **`WARN`** lines — a **MEDIUM** or **LOW** finding. Advisory; does not fail the
  run at the default threshold. Review and either fix or annotate.
- Each finding shows `path:line`, a masked detail line, and a `fix:` remediation.

The final block prints per-severity counts, the fail threshold, and a `RESULT:`
line. **Exit `0` only when zero findings meet the threshold.**

```
Summary: CRITICAL=0 HIGH=0 MEDIUM=1 LOW=2 INFO=0
Fail threshold: HIGH (>= this severity fails the run)
RESULT: PASS (warnings, if any, are advisory)
```

### JSON shape (for CI / dashboards)

```json
{
  "tool": "hermx-security-audit",
  "version": 1,
  "fail_on": "high",
  "checks": { "secrets": "ran", "static": "skipped", ... },
  "counts": { "CRITICAL": 0, "HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 0 },
  "fail_count": 0,
  "result": "PASS",
  "findings": [ { "check", "severity", "title", "path", "line", "detail", "remediation" } ]
}
```

Output is deterministic: findings are sorted by `(severity, check, path, line, title)`
and no timestamp is emitted unless `--timestamp` is passed — so two runs on the
same tree diff clean, and CI can compare against a committed baseline.

## Fixing findings

| Check | Where / how to fix |
|-------|--------------------|
| `secrets` | Remove the literal, **rotate the exposed credential**, and load it from `.env`/`os.environ` at runtime. If it is a test fixture, append `# pragma: allowlist secret` to the line. |
| `dangerous` | Replace `eval`/`exec` with explicit parsing or `ast.literal_eval`; never `pickle`/`marshal` untrusted data (use JSON); drop `shell=True` for an argv list; use `yaml.safe_load`. |
| `authbypass` | Compare secrets/HMACs with `hmac.compare_digest`, never `==`/`!=`. Keep the timestamp-bound replay window in `verify_webhook_hmac`. |
| `logging` | Never log a raw secret. Log a boolean/redacted form or route through `redact_secrets()`. |
| `perms` | `chmod 600 <file>` on the flagged credential/state file. |
| `docker` | Add a non-root `USER`; never `COPY .env`; inject secrets at runtime via `env_file`/secrets; add `cap_drop: [ALL]`; bind published ports to `127.0.0.1`. |
| `gitleak` | `git rm --cached <file>`, ensure it is in `.gitignore`, and rotate any secret that was committed. Purge history if it was a real key. |
| `static` (bandit) | Address the specific `Bxxx` finding; scope an intentional exception with `# nosec Bxxx`. |
| `deps` (pip-audit) | Upgrade the flagged package to the listed fix version; if unfixable, justify and pin. |

After fixing, re-run until `RESULT: PASS`. If a finding is a genuine, intentional
pattern, record it via `/learn` so future audits and reviewers understand the
exception (and add the allowlist pragma / `# nosec` / `.secrets.baseline` entry).

## Running regularly

### Pre-commit (fast, stdlib-only)

`.pre-commit-config.yaml` includes a local hook that runs the fast audit on every
commit and blocks on any HIGH/CRITICAL finding:

```yaml
- repo: local
  hooks:
    - id: hermx-security-audit
      name: hermx security audit (fast)
      entry: python scripts/security-audit.py --fast --fail-on high
      language: system
      pass_filenames: false
      always_run: true
```

Enable it once with `./.venv/bin/pre-commit install`.

### CI (full suite, blocking)

```yaml
# .github/workflows — audit step
- name: Security audit
  run: |
    python -m pip install -r requirements-security.txt
    python scripts/security-audit.py --report security-audit.json
# Non-zero exit fails the job. Archive security-audit.json as an artifact.
```

For git-**history** secret scanning (secrets already committed and later removed —
which the working-tree scan cannot see), add gitleaks or trufflehog as a separate
CI step (installed out-of-band, not via pip):

```bash
gitleaks git . --report-format json --report-path gitleaks.json
trufflehog git file://. --only-verified --fail --json
```

### Cron (scheduled regression gate)

Run the full audit daily and alert on a non-zero exit — mirrors the existing
Hermes cron-monitor pattern in `deploy/hermes-scripts/`:

```bash
# daily
cd /path/to/hermx && ./.venv/bin/python scripts/security-audit.py --report /var/log/hermx/security-audit.json \
  || notify "HermX security audit FAILED — see security-audit.json"
```

## Extending the audit

- Add a check: write a `check_<name>(root, fast=False) -> (findings, status)`
  function in `scripts/security-audit.py` and register it in the `CHECKS` list.
  Return findings via the `finding(...)` helper (`CRITICAL|HIGH|MEDIUM|LOW|INFO`).
- Keep the core **stdlib-only** and any new external tool **optional** (report
  `skipped` when it is absent) — that is what lets the audit run everywhere.
- Add a good/bad fixture test to `tests/test_security_audit.py`; validate any new
  secret/pattern regex against the repo's own `tests/` fixtures before making it
  a fail-level rule (secret-pattern rules are the top false-positive source).

## Related

- `scripts/security-audit.py` — the engine.
- `tests/test_security_audit.py` — fixture-driven tests for every check.
- `pyproject.toml` `[tool.bandit]` / `[tool.ruff.lint]` — optional-tool config.
- `requirements-security.txt` — optional scanner extras.
- `.pre-commit-config.yaml` — detect-secrets + the fast local audit hook.
- `docs/hermx-slash-commands.md`, `.windsurf/workflows/doc-audit.md` — sibling audits.
