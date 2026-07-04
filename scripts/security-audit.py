#!/usr/bin/env python3
"""HermX security audit — static, read-only threat scanner.

Runs a battery of security checks tailored to this project's threat model (a
crypto-trading webhook receiver that dispatches real orders):

  1. static      — bandit (optional) over src/ + deploy/ + scripts/
  2. deps        — pip-audit (optional) dependency CVE scan
  3. secrets     — hardcoded-secret heuristics + detect-secrets (optional)
  4. dangerous   — AST scan for eval/exec/pickle/yaml.load/shell=True/os.system
  5. authbypass  — HMAC/webhook comparison patterns (== instead of compare_digest)
  6. logging     — secret-bearing variables passed to logging calls
  7. perms       — world/group-readable .env / secret / state files
  8. docker      — Dockerfile + docker-compose security posture
  9. gitleak     — secret files tracked in git / missing from .gitignore

Design principles:
  * STDLIB ONLY for the core. Every external tool (bandit, pip-audit,
    detect-secrets) is OPTIONAL — if it is not installed the check is SKIPPED,
    never failed. The heuristic checks (secrets/dangerous/authbypass/logging/
    perms/docker/gitleak) always run on stdlib alone.
  * READ-ONLY. The audit never mutates the repo.
  * DETERMINISTIC. Findings are sorted; no timestamps in the comparable body
    (the JSON metadata header carries an optional generated_at only when asked).

Exit codes:
  0  no findings at/above the fail threshold (warnings allowed)
  1  at least one finding at/above the fail threshold (default: high)
  2  usage / internal error

Usage:
  ./.venv/bin/python scripts/security-audit.py                 # human report
  ./.venv/bin/python scripts/security-audit.py --json          # machine JSON
  ./.venv/bin/python scripts/security-audit.py --report out.json
  ./.venv/bin/python scripts/security-audit.py --fast          # skip external tools
  ./.venv/bin/python scripts/security-audit.py --fail-on medium # stricter gate
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import yaml  # third-party (PyYAML, see requirements-security.txt); used by the docker/compose check

# --------------------------------------------------------------------------- #
# Severity + finding model
# --------------------------------------------------------------------------- #

# Ordered most-severe first. Index is used for threshold comparisons.
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Which severities render as FAIL vs WARN in the human summary.
_FAIL_SEVS = {"CRITICAL", "HIGH"}
_WARN_SEVS = {"MEDIUM", "LOW"}


def _sev_at_or_above(sev, threshold):
    """True when `sev` is at least as severe as `threshold` (case-insensitive)."""
    return (_SEV_RANK.get(str(sev).upper(), len(SEVERITIES))
            <= _SEV_RANK.get(str(threshold).upper(), 1))


def finding(check, severity, title, path="", line=0, detail="", remediation=""):
    """Build a single finding record (plain dict for easy JSON serialization)."""
    severity = severity.upper()
    if severity not in _SEV_RANK:
        severity = "MEDIUM"
    return {
        "check": check,
        "severity": severity,
        "title": title,
        "path": str(path),
        "line": int(line),
        "detail": detail,
        "remediation": remediation,
    }


def _sort_key(f):
    return (
        _SEV_RANK.get(f["severity"], len(SEVERITIES)),
        f["check"],
        f["path"],
        f["line"],
        f["title"],
    )


# --------------------------------------------------------------------------- #
# Repo / file discovery
# --------------------------------------------------------------------------- #

# Directories never worth scanning (vendored, build output, caches, VCS).
_PRUNE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".deploy-backups",
    "out", ".next",
}

# Extensions we treat as "source we scan for secrets / patterns".
_CODE_EXT = {".py", ".sh", ".bash", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env", ".ps1"}


def repo_root(start=None):
    """Repo root via git; fall back to the given/enclosing directory."""
    base = Path(start) if start else Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        top = out.stdout.strip()
        if top:
            return Path(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return base


def _pruned(path, root):
    rel_parts = path.relative_to(root).parts
    return any(part in _PRUNE_DIRS for part in rel_parts)


def iter_files(root, exts=None):
    """Yield files under `root`, skipping pruned dirs. Prefer git-tracked set."""
    exts = exts or _CODE_EXT
    tracked = _git_tracked(root)
    if tracked is not None:
        for rel in tracked:
            p = root / rel
            if exts and p.suffix.lower() not in exts:
                continue
            if _pruned(p, root):
                continue
            if p.is_file():
                yield p
        return
    # No git — walk the tree.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if exts and p.suffix.lower() not in exts:
                continue
            yield p


def _git_tracked(root):
    """Sorted list of git-tracked relative paths, or None if not a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted(line for line in out.stdout.splitlines() if line)


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(path, root):
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


# Lines carrying this pragma (detect-secrets convention) suppress secret/pattern
# findings — used by tests/fixtures that legitimately embed fake secrets.
_ALLOWLIST_PRAGMA = "pragma: allowlist secret"


def _is_test_path(rel):
    """True for test files/dirs. Timing-safety and secret-logging rules target the
    production request/auth surface; test code legitimately compares/prints secrets."""
    parts = Path(rel).parts
    return "tests" in parts or Path(rel).name.startswith("test_")


# --------------------------------------------------------------------------- #
# Check 1 — bandit (optional external tool)
# --------------------------------------------------------------------------- #

def check_bandit(root, fast=False):
    """Run bandit if importable. HIGH severity+confidence -> HIGH finding."""
    findings = []
    if fast or not _module_available("bandit"):
        return findings, "skipped"
    targets = [d for d in ("src", "deploy", "scripts", "tests") if (root / d).is_dir()]
    if not targets:
        return findings, "skipped"
    cmd = [
        sys.executable, "-m", "bandit", "-r", *targets,
        "-f", "json", "-q",
        # B101 = assert removed by -O; noisy in tests. Keep it out globally.
        "--skip", "B101",
    ]
    # Bandit does NOT auto-discover pyproject.toml; pass -c only when a [tool.bandit]
    # section is actually present (else bandit raises), so operator excludes are honored.
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and "[tool.bandit]" in _read_text(pyproject):
        cmd += ["-c", "pyproject.toml"]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    except OSError:
        return findings, "skipped"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return [finding("static", "LOW", "bandit produced unparseable output",
                        detail=(proc.stderr or "")[:400],
                        remediation="Run bandit manually to inspect.")], "ran"
    for res in data.get("results", []):
        sev = str(res.get("issue_severity", "LOW")).upper()
        conf = str(res.get("issue_confidence", "LOW")).upper()
        # Map bandit's own severity onto ours, tempered by confidence.
        if sev == "HIGH" and conf in ("HIGH", "MEDIUM"):
            ours = "HIGH"
        elif sev == "HIGH" or sev == "MEDIUM":
            ours = "MEDIUM"
        else:
            ours = "LOW"
        findings.append(finding(
            "static", ours,
            "bandit %s: %s" % (res.get("test_id", "?"), res.get("issue_text", "")[:120]),
            path=_rel(res.get("filename", ""), root),
            line=int(res.get("line_number", 0) or 0),
            detail="severity=%s confidence=%s" % (sev, conf),
            remediation="Review the flagged construct; see https://bandit.readthedocs.io.",
        ))
    return findings, "ran"


# --------------------------------------------------------------------------- #
# Check 2 — dependency CVEs (pip-audit, optional)
# --------------------------------------------------------------------------- #

def check_deps(root, fast=False):
    findings = []
    if fast or not _module_available("pip_audit"):
        return findings, "skipped"
    cmd = [sys.executable, "-m", "pip_audit", "-f", "json", "--progress-spinner", "off"]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    except OSError:
        return findings, "skipped"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return findings, "skipped"
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    if isinstance(deps, dict):
        deps = deps.get("dependencies", [])
    for dep in deps or []:
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in dep.get("vulns", []) or []:
            fix = ", ".join(vuln.get("fix_versions", []) or []) or "no fix listed"
            findings.append(finding(
                "deps", "HIGH",
                "vulnerable dependency %s==%s (%s)" % (name, version, vuln.get("id", "?")),
                detail=(vuln.get("description", "") or "")[:200],
                remediation="Upgrade to: %s" % fix,
            ))
    return findings, "ran"


# --------------------------------------------------------------------------- #
# Check 3 — hardcoded secrets (stdlib heuristics + detect-secrets optional)
# --------------------------------------------------------------------------- #

# High-signal patterns -> CRITICAL. These almost never occur benignly.
_HIGH_SIGNAL = [
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bghp_[0-9A-Za-z]{36}\b")),
    ("Stripe live key", re.compile(r"\bsk_live_[0-9A-Za-z]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
]

def check_secrets(root, fast=False):
    """Stdlib high-signal secret regexes (CRITICAL); entropy detection is delegated
    to detect-secrets when installed. The old generic `key = "value"` heuristic was
    removed as low-SNR — it flagged benign assignments and `.env` values."""
    findings = []
    for path in iter_files(root):
        # Never scan the audit's own detection patterns or the secrets baseline.
        rel = _rel(path, root)
        if rel.endswith("scripts/security-audit.py") or _is_test_path(rel):
            continue
        if rel.endswith(".secrets.baseline"):
            continue
        # `.env` / `.env.*` are the legitimate home for secrets; perms + gitleak
        # cover them, so don't double-flag real values here.
        name = Path(rel).name
        if name == ".env" or name.startswith(".env."):
            continue
        text = _read_text(path)
        if not text:
            continue
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if _ALLOWLIST_PRAGMA in raw:
                continue
            line = raw.strip()
            for label, pat in _HIGH_SIGNAL:
                if pat.search(raw):
                    findings.append(finding(
                        "secrets", "CRITICAL",
                        "hardcoded %s" % label,
                        path=rel, line=lineno,
                        detail=_mask(line),
                        remediation="Remove the secret, rotate it, and load it from the "
                                    "environment / .env at runtime.",
                    ))
    status = "ran"
    ds_findings, ds_status = _detect_secrets(root, fast)
    findings.extend(ds_findings)
    return findings, status if ds_status != "ran" else "ran+detect-secrets"


def _mask(line):
    """Truncate + partially mask a source line so the report never leaks a full secret."""
    line = line[:160]
    def _mask_quoted(m):
        v = m.group(2)
        if len(v) <= 8:
            return m.group(0)
        return "%s%s%s%s" % (m.group(1), v[:2], "*" * 6, m.group(3))
    return re.sub(r"(['\"])([^'\"]{9,})(['\"])", _mask_quoted, line)


def _detect_secrets(root, fast):
    if fast or not _module_available("detect_secrets"):
        return [], "skipped"
    baseline = root / ".secrets.baseline"
    cmd = [sys.executable, "-m", "detect_secrets", "scan"]
    if baseline.exists():
        cmd += ["--baseline", str(baseline)]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    except OSError:
        return [], "skipped"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return [], "skipped"
    findings = []
    for fname, entries in (data.get("results") or {}).items():
        for e in entries:
            if e.get("is_secret") is False:
                continue
            findings.append(finding(
                "secrets", "HIGH",
                "detect-secrets: %s" % e.get("type", "potential secret"),
                path=fname, line=int(e.get("line_number", 0) or 0),
                remediation="Verify and, if real, remove + rotate. Otherwise audit into "
                            ".secrets.baseline.",
            ))
    return findings, "ran"


# --------------------------------------------------------------------------- #
# Check 4 — dangerous calls (AST)
# --------------------------------------------------------------------------- #

def check_dangerous(root, fast=False):
    """AST scan of .py files for code-execution / deserialization sinks."""
    findings = []
    for path in iter_files(root, exts={".py"}):
        rel = _rel(path, root)
        if rel.endswith("scripts/security-audit.py"):
            continue
        src = _read_text(path)
        if not src:
            continue
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            lineno = getattr(node, "lineno", 0)
            if lineno and lineno <= len(lines) and _ALLOWLIST_PRAGMA in lines[lineno - 1]:
                continue
            hit = _classify_call(name, node)
            if hit:
                sev, title, remediation = hit
                findings.append(finding(
                    "dangerous", sev, title, path=rel, line=lineno,
                    detail=name + "(...)", remediation=remediation,
                ))
    return findings, "ran"


def _call_name(func):
    """Dotted call name, e.g. 'os.system', 'pickle.loads', 'eval'."""
    parts = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _kwarg_is_true(node, key):
    for kw in node.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _classify_call(name, node):
    base = name.split(".")[-1]
    root_name = name.split(".")[0]
    if name in ("eval", "exec") or (root_name in ("builtins",) and base in ("eval", "exec")):
        return ("CRITICAL", "use of %s()" % base,
                "Avoid eval/exec on any external input; use ast.literal_eval or explicit parsing.")
    if base == "system" and root_name in ("os",):
        return ("HIGH", "os.system() shell call",
                "Use subprocess.run([...]) with a list argv and no shell.")
    if base in ("load", "loads") and root_name in ("pickle", "cPickle", "_pickle", "marshal"):
        return ("CRITICAL", "%s.%s() deserialization" % (root_name, base),
                "Never unpickle untrusted data; use JSON for interchange.")
    if base == "load" and root_name == "yaml":
        # yaml.load without an explicit safe loader is unsafe.
        has_loader = any(kw.arg == "Loader" for kw in node.keywords)
        loader_val = _yaml_loader_name(node)
        if not has_loader or (loader_val and "Safe" not in loader_val):
            return ("HIGH", "yaml.load() without SafeLoader",
                    "Use yaml.safe_load() or Loader=yaml.SafeLoader.")
        return None
    if base == "__import__":
        return ("MEDIUM", "dynamic __import__()",
                "Prefer importlib with a fixed allowlist; never import attacker-controlled names.")
    # subprocess.* with shell=True
    if root_name == "subprocess" and base in ("Popen", "call", "run", "check_call", "check_output"):
        if _kwarg_is_true(node, "shell"):
            return ("HIGH", "%s(..., shell=True)" % name,
                    "Drop shell=True and pass an argv list; if a shell is unavoidable, "
                    "shlex.quote() every interpolated value.")
    return None


def _yaml_loader_name(node):
    for kw in node.keywords:
        if kw.arg == "Loader":
            return _call_name(kw.value) if isinstance(kw.value, (ast.Attribute, ast.Name)) else ""
    return ""


# --------------------------------------------------------------------------- #
# Check 5 — HMAC / auth comparison bypasses (AST)
# --------------------------------------------------------------------------- #

_SECRETY_NAME = re.compile(
    r"(?i)(secret|signature|\bsig\b|hmac|\btoken\b|digest|password|passphrase|\bmac\b)"
)


def check_authbypass(root, fast=False):
    """Flag `==`/`!=` comparisons over secret-ish operands (timing-unsafe)."""
    findings = []
    sec_dir = root / "src" / "security"
    for path in iter_files(root, exts={".py"}):
        rel = _rel(path, root)
        if rel.endswith("scripts/security-audit.py") or _is_test_path(rel):
            continue
        src = _read_text(path)
        if not src:
            continue
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                continue
            operands = [node.left] + list(node.comparators)
            names = [_operand_text(o) for o in operands]
            if not any(n and _SECRETY_NAME.search(n) for n in names):
                continue
            lineno = getattr(node, "lineno", 0)
            if lineno and lineno <= len(lines):
                ctx = lines[lineno - 1]
                if _ALLOWLIST_PRAGMA in ctx:
                    continue
                # Don't flag comparisons that are clearly to empty/None/length.
                if _is_benign_compare(operands):
                    continue
            findings.append(finding(
                "authbypass", "HIGH",
                "timing-unsafe equality on secret-like value (%s)" % ", ".join(n for n in names if n)[:80],
                path=rel, line=lineno,
                detail=(lines[lineno - 1].strip() if lineno and lineno <= len(lines) else ""),
                remediation="Compare secrets/HMACs with hmac.compare_digest(), never == / !=.",
            ))
    # Positive assurance: the extracted auth module should use compare_digest.
    if (sec_dir / "webhook_auth.py").exists():
        wa = _read_text(sec_dir / "webhook_auth.py")
        if "compare_digest" not in wa:
            findings.append(finding(
                "authbypass", "HIGH",
                "webhook_auth.py does not use hmac.compare_digest",
                path=_rel(sec_dir / "webhook_auth.py", root),
                remediation="Restore constant-time comparison for the shared secret and HMAC.",
            ))
        if "replay" not in wa.lower():
            findings.append(finding(
                "authbypass", "MEDIUM",
                "no replay-window handling detected in webhook_auth.py",
                path=_rel(sec_dir / "webhook_auth.py", root),
                remediation="Bind HMAC to a timestamp and reject stale requests (replay window).",
            ))
    return findings, "ran"


def _operand_text(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_name(node)
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Subscript):
        return _operand_text(node.value)
    return ""


def _is_benign_compare(operands):
    for o in operands:
        if isinstance(o, ast.Constant) and (o.value is None or o.value == "" or o.value == 0):
            return True
        # len(x) == n
        if isinstance(o, ast.Call) and _call_name(o.func) == "len":
            return True
    return False


# --------------------------------------------------------------------------- #
# Check 6 — secret logging (AST)
# --------------------------------------------------------------------------- #

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_LOG_FUNCS = {"print"}
_SECRET_VARNAME = re.compile(
    r"(?i)(secret|passphrase|password|passwd|api[_-]?key|apikey|private[_-]?key|"
    r"access[_-]?token|auth[_-]?token|\btoken\b|hmac_key)"
)
# Names that carry secret-ish substrings but are safe (already redacted / booleans).
_SECRET_SAFE = re.compile(r"(?i)(redact|has_|_present|_set|_ok|healthy|missing|len|reason|token_created)")
# A call to one of these functions sanitizes its argument (redaction/masking), so
# `log.info(redact_secrets(secret))` is the CORRECT pattern and must not be flagged.
_SANITIZER = re.compile(r"(?i)(redact|mask|scrub|sanitiz|obfuscat)")


def check_logging(root, fast=False):
    """Flag logging/print calls that pass a secret-named variable as an argument."""
    findings = []
    for path in iter_files(root, exts={".py"}):
        rel = _rel(path, root)
        if rel.endswith("scripts/security-audit.py") or _is_test_path(rel):
            continue
        src = _read_text(path)
        if not src:
            continue
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = _call_name(node.func)
            base = fname.split(".")[-1]
            if base not in _LOG_METHODS and fname not in _LOG_FUNCS:
                continue
            lineno = getattr(node, "lineno", 0)
            if lineno and lineno <= len(lines) and _ALLOWLIST_PRAGMA in lines[lineno - 1]:
                continue
            for arg in _iter_arg_names(node):
                if _SECRET_VARNAME.search(arg) and not _SECRET_SAFE.search(arg):
                    findings.append(finding(
                        "logging", "HIGH",
                        "possible secret passed to %s(): %s" % (base, arg),
                        path=rel, line=lineno,
                        detail=(lines[lineno - 1].strip() if lineno and lineno <= len(lines) else ""),
                        remediation="Never log raw secrets. Log a boolean/redacted form, or route "
                                    "through redact_secrets().",
                    ))
                    break
    return findings, "ran"


def _is_sanitized(arg):
    """True if the argument is wrapped in a redaction/masking call, e.g.
    redact_secrets(secret) — the correct way to log secret-derived data."""
    if isinstance(arg, ast.Call) and _SANITIZER.search(_call_name(arg.func)):
        return True
    return False


def _iter_arg_names(call):
    """Yield identifier-ish text for names/attributes referenced in a call's args.

    Arguments already wrapped in a redaction/masking call are skipped (sanitized),
    so the canonical `log.info(redact_secrets(secret))` pattern is not flagged.
    """
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        if _is_sanitized(arg):
            continue
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name):
                yield sub.id
            elif isinstance(sub, ast.Attribute):
                yield sub.attr
            elif isinstance(sub, ast.FormattedValue):
                if _is_sanitized(sub.value):
                    continue
                inner = _operand_text(sub.value)
                if inner:
                    yield inner


# --------------------------------------------------------------------------- #
# Check 7 — file permissions on sensitive files
# --------------------------------------------------------------------------- #

_SENSITIVE_FILES = (
    ".env", ".env.local", ".env.production",
    "HERMX_SECRET.txt", "control-state.json",
)
_SENSITIVE_GLOBS = ("*.pem", "*.key", "*_secret*", "*credentials*")


def check_perms(root, fast=False):
    """World/group-readable secret/state files are a finding (POSIX only)."""
    findings = []
    if os.name != "posix":
        return [finding("perms", "INFO", "permission check skipped on non-POSIX platform")], "skipped"
    seen = set()
    candidates = []
    for name in _SENSITIVE_FILES:
        candidates.append(root / name)
    for pattern in _SENSITIVE_GLOBS:
        candidates.extend(sorted(root.glob(pattern)))
    for path in candidates:
        if path in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        rel = _rel(path, root)
        if mode & 0o077:
            findings.append(finding(
                "perms", "HIGH",
                "sensitive file is group/other-accessible (mode %04o)" % mode,
                path=rel,
                remediation="chmod 600 %s" % rel,
            ))
        elif mode & 0o022:
            findings.append(finding(
                "perms", "MEDIUM",
                "sensitive file is world/group-writable (mode %04o)" % mode,
                path=rel, remediation="chmod 600 %s" % rel,
            ))
    return findings, "ran"


# --------------------------------------------------------------------------- #
# Check 8 — Docker / compose security
# --------------------------------------------------------------------------- #

def check_docker(root, fast=False):
    findings = []
    ran = False
    dockerfile = root / "Dockerfile"
    if dockerfile.exists():
        ran = True
        findings.extend(_audit_dockerfile(dockerfile, root))
    for compose in sorted(root.glob("docker-compose*.yml")) + sorted(root.glob("docker-compose*.yaml")):
        ran = True
        findings.extend(_audit_compose(compose, root))
    return findings, ("ran" if ran else "skipped")


def _audit_dockerfile(path, root):
    findings = []
    rel = _rel(path, root)
    text = _read_text(path)
    lines = text.splitlines()
    has_user_nonroot = False
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        low = line.lower()
        if low.startswith("user "):
            user = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            if user and not user.startswith(("root", "0", "0:")):
                has_user_nonroot = True
        # Secrets baked into the image.
        if low.startswith(("copy ", "add ")) and re.search(r"(^|\s)\.env(\s|$)", line):
            findings.append(finding(
                "docker", "CRITICAL", "secret file copied into image (.env)",
                path=rel, line=i, detail=line,
                remediation="Never COPY .env; inject secrets at runtime via env_file / secrets.",
            ))
        if re.search(r"(?i)(ENV|ARG)\s+\w*(SECRET|PASSWORD|API_?KEY|TOKEN|PASSPHRASE)\w*\s*=", line):
            findings.append(finding(
                "docker", "HIGH", "secret embedded in ENV/ARG layer",
                path=rel, line=i, detail=_mask(line),
                remediation="Pass secrets at runtime, not as build-time ARG/ENV (they persist in layers).",
            ))
    if not has_user_nonroot:
        findings.append(finding(
            "docker", "HIGH", "Dockerfile has no non-root USER directive",
            path=rel,
            remediation="Add a non-root USER (e.g. a fixed uid/gid) before CMD.",
        ))
    return findings


def _audit_compose(path, root):
    findings = []
    rel = _rel(path, root)
    text = _read_text(path)
    services = _parse_compose(text, root)
    is_legacy = "host" in path.name  # docker-compose.host.yml is the documented legacy fallback

    for name, body in services.items():
        blob = body["text"]
        low = blob.lower()
        if re.search(r"privileged\s*:\s*true", low):
            findings.append(finding(
                "docker", "CRITICAL", "service '%s' runs privileged" % name,
                path=rel, remediation="Remove privileged: true; grant only specific caps if needed.",
            ))
        # Ports published to all interfaces (no 127.0.0.1 bind).
        for pm in re.findall(r'-\s*["\']?([0-9.]*:?[0-9]+:[0-9]+)["\']?', blob):
            if pm.count(":") >= 2:
                host_ip = pm.split(":", 1)[0]
                if host_ip not in ("127.0.0.1", "::1", "localhost"):
                    findings.append(finding(
                        "docker", "MEDIUM",
                        "service '%s' publishes port on non-loopback host (%s)" % (name, pm),
                        path=rel,
                        remediation="Bind to 127.0.0.1 and front with the reverse proxy/tailnet.",
                    ))
        if not is_legacy:
            for line in blob.splitlines():
                m = re.match(r'\s*-\s*["\']?([0-9.:]+)["\']?\s*$', line)
                if not m:
                    continue
                mapping = m.group(1)
                # A bare "8891:8891" (host:container) with no host-IP prefix -> 0.0.0.0.
                if mapping.count(":") != 1:
                    continue
                host, container = mapping.split(":")
                if not (host.isdigit() and container.isdigit()):
                    continue
                findings.append(finding(
                    "docker", "MEDIUM",
                    "service '%s' publishes a port without a host-IP bind (0.0.0.0): %s" % (name, mapping),
                    path=rel,
                    remediation="Prefix the mapping with 127.0.0.1: to avoid exposing it on all interfaces.",
                ))
        # Hardening posture (advisory for the primary compose; skipped for legacy fallback).
        if not is_legacy:
            if "cap_drop" not in low:
                findings.append(finding(
                    "docker", "LOW",
                    "service '%s' does not drop capabilities (no cap_drop)" % name,
                    path=rel, remediation="Add cap_drop: [ALL] and add back only required caps.",
                ))
    if not services:
        findings.append(finding(
            "docker", "LOW", "could not parse services from compose file",
            path=rel, remediation="Verify the compose file is well-formed.",
        ))
    return findings


def _parse_compose(text, root):
    """Parse compose services into {name: {"text": <yaml>}} via PyYAML."""
    services = {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return services
    if not isinstance(data, dict):
        return services
    for name, body in (data.get("services") or {}).items():
        services[name] = {"text": yaml.safe_dump(body, default_flow_style=False)}
    return services


# --------------------------------------------------------------------------- #
# Check 9 — secrets tracked in git / .gitignore hygiene
# --------------------------------------------------------------------------- #

def check_gitleak(root, fast=False):
    findings = []
    tracked = _git_tracked(root)
    if tracked is None:
        return [finding("gitleak", "INFO", "not a git repo; skipped tracked-secret check")], "skipped"
    tracked_set = set(tracked)
    for name in (".env", "HERMX_SECRET.txt", "control-state.json"):
        if name in tracked_set:
            findings.append(finding(
                "gitleak", "CRITICAL",
                "secret/state file is tracked in git: %s" % name,
                path=name,
                remediation="git rm --cached %s ; ensure it is in .gitignore; rotate any exposed secret." % name,
            ))
    for rel in tracked:
        low = rel.lower()
        if low.endswith((".pem", ".key")) or re.search(r"(?i)(id_rsa|id_ed25519|\.p12|\.pfx)$", low):
            findings.append(finding(
                "gitleak", "HIGH", "key material tracked in git: %s" % rel,
                path=rel,
                remediation="Remove from history if it is a real key; add to .gitignore and rotate.",
            ))
    # .gitignore should list .env (defense in depth).
    gi = root / ".gitignore"
    if gi.exists() and ".env" not in _read_text(gi):
        findings.append(finding(
            "gitleak", "MEDIUM", ".gitignore does not list .env",
            path=".gitignore", remediation="Add `.env` to .gitignore.",
        ))
    return findings, "ran"


# --------------------------------------------------------------------------- #
# Tool availability
# --------------------------------------------------------------------------- #

def _module_available(mod):
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Orchestration + reporting
# --------------------------------------------------------------------------- #

CHECKS = [
    ("static", check_bandit),
    ("deps", check_deps),
    ("secrets", check_secrets),
    ("dangerous", check_dangerous),
    ("authbypass", check_authbypass),
    ("logging", check_logging),
    ("perms", check_perms),
    ("docker", check_docker),
    ("gitleak", check_gitleak),
]


def run_audit(root, fast=False, only=None):
    """Run all (or selected) checks. Returns (findings, statuses)."""
    findings = []
    statuses = {}
    for name, fn in CHECKS:
        if only and name not in only:
            statuses[name] = "off"
            continue
        try:
            fs, status = fn(root, fast=fast)
        except Exception as exc:  # a broken check must not abort the whole audit
            fs = [finding(name, "LOW", "check crashed: %s" % type(exc).__name__,
                          detail=str(exc)[:200],
                          remediation="File a bug against security-audit.py.")]
            status = "error"
        findings.extend(fs)
        statuses[name] = status
    findings.sort(key=_sort_key)
    return findings, statuses


def build_report(root, findings, statuses, fail_on, generated_at=None):
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f["severity"]] += 1
    fail_count = sum(1 for f in findings if _sev_at_or_above(f["severity"], fail_on))
    report = {
        "tool": "hermx-security-audit",
        "version": 1,
        "root": str(root),
        "fail_on": fail_on,
        "checks": statuses,
        "counts": counts,
        "fail_count": fail_count,
        "result": "FAIL" if fail_count else "PASS",
        "findings": findings,
    }
    if generated_at:
        report["generated_at"] = generated_at
    return report


def print_human(report):
    out = sys.stdout
    def w(line=""):
        out.write(line + "\n")

    sev_glyph = {"CRITICAL": "FAIL", "HIGH": "FAIL", "MEDIUM": "WARN", "LOW": "WARN", "INFO": "    "}
    w("HermX Security Audit")
    w("=" * 60)
    w("root: %s" % report["root"])
    w()
    # Check status line.
    w("Checks:")
    for name, status in report["checks"].items():
        w("  %-11s %s" % (name, status))
    w()
    if not report["findings"]:
        w("No findings. Clean.")
    else:
        current = None
        for f in report["findings"]:
            if f["check"] != current:
                current = f["check"]
                w("=== %s ===" % current)
            loc = f["path"]
            if f["line"]:
                loc += ":%d" % f["line"]
            w("%-4s [%s] %s" % (sev_glyph[f["severity"]], f["severity"], f["title"]))
            if loc.strip():
                w("       %s" % loc)
            if f["detail"]:
                w("       %s" % f["detail"])
            if f["remediation"]:
                w("       fix: %s" % f["remediation"])
        w()
    c = report["counts"]
    w("-" * 60)
    w("Summary: CRITICAL=%d HIGH=%d MEDIUM=%d LOW=%d INFO=%d"
      % (c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"]))
    w("Fail threshold: %s (>= this severity fails the run)" % report["fail_on"].upper())
    if report["result"] == "FAIL":
        w("RESULT: FAIL (%d finding(s) at/above %s)" % (report["fail_count"], report["fail_on"].upper()))
    else:
        w("RESULT: PASS (warnings, if any, are advisory)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="HermX static security audit (read-only).")
    parser.add_argument("--json", action="store_true", help="emit JSON to stdout instead of human report")
    parser.add_argument("--report", metavar="PATH", help="write the JSON report to PATH")
    parser.add_argument("--fast", action="store_true", help="skip external tools (bandit/pip-audit/detect-secrets)")
    parser.add_argument("--fail-on", default="high", choices=[s.lower() for s in SEVERITIES],
                        help="minimum severity that fails the run (default: high)")
    parser.add_argument("--only", metavar="CHECK", action="append",
                        help="run only the named check(s); repeatable (e.g. --only secrets --only perms)")
    parser.add_argument("--root", metavar="DIR", help="repo root to audit (default: auto-detect)")
    parser.add_argument("--timestamp", action="store_true",
                        help="include generated_at in the JSON report (off by default for determinism)")
    args = parser.parse_args(argv)

    root = repo_root(args.root)
    if not root.exists():
        sys.stderr.write("error: root %s does not exist\n" % root)
        return 2

    only = set(args.only) if args.only else None
    if only:
        known = {name for name, _ in CHECKS}
        bad = only - known
        if bad:
            sys.stderr.write("error: unknown check(s): %s (known: %s)\n"
                             % (", ".join(sorted(bad)), ", ".join(sorted(known))))
            return 2

    findings, statuses = run_audit(root, fast=args.fast, only=only)

    generated_at = None
    if args.timestamp:
        import datetime
        generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    report = build_report(root, findings, statuses, args.fail_on, generated_at=generated_at)

    if args.report:
        try:
            Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("error: cannot write report: %s\n" % exc)
            return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        print_human(report)

    return 1 if report["result"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
