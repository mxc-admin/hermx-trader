"""Tests for scripts/security-audit.py — the static security auditor.

The audit module lives at ``scripts/security-audit.py`` (hyphenated, so it is not
importable by name); we load it by file path once and exercise the individual
check functions against synthetic known-good / known-bad fixtures written into
``tmp_path``. Each check is a pure ``(root, fast=…) -> (findings, status)`` function,
which makes fixture-driven testing straightforward and hermetic.

The checks that shell out to git (``gitleak``, and ``iter_files`` file discovery)
fall back to an ``os.walk`` of the tmp tree when the fixture dir is not a git repo,
so most tests need no git. The git-tracked-secret tests init a real repo and are
skipped when git is unavailable.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Load the hyphenated script as a module once for the whole test session.
# --------------------------------------------------------------------------- #

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "security-audit.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("hermx_security_audit", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load_audit()


def _write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _titles(findings):
    return [f["title"] for f in findings]


def _has(findings, substr, severity=None):
    return any(substr in f["title"] and (severity is None or f["severity"] == severity)
               for f in findings)


# --------------------------------------------------------------------------- #
# Severity model / helpers
# --------------------------------------------------------------------------- #

def test_sev_at_or_above_case_insensitive():
    assert audit._sev_at_or_above("HIGH", "high")
    assert audit._sev_at_or_above("CRITICAL", "high")
    assert audit._sev_at_or_above("MEDIUM", "medium")
    assert not audit._sev_at_or_above("MEDIUM", "high")
    assert not audit._sev_at_or_above("LOW", "HIGH")
    # unknown severity sorts to the bottom (never fails)
    assert not audit._sev_at_or_above("BOGUS", "high")


def test_finding_normalizes_bad_severity():
    f = audit.finding("x", "not-a-sev", "t")
    assert f["severity"] == "MEDIUM"
    assert f["check"] == "x"
    assert f["line"] == 0


def test_mask_hides_long_quoted_values():
    masked = audit._mask('api_key = "SUPERSECRETVALUE1234567"')
    assert "SUPERSECRETVALUE1234567" not in masked
    assert "*" in masked
    # short values are left alone
    assert audit._mask('x = "abc"') == 'x = "abc"'


# --------------------------------------------------------------------------- #
# Check 3 — secrets
# --------------------------------------------------------------------------- #

def test_secrets_flags_private_key_and_aws(tmp_path):
    _write(tmp_path, "src/leak.py",
           'KEY = """-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"""\n'
           'AWS = "AKIAIOSFODNN7EXAMPLE0"\n')
    findings, status = audit.check_secrets(tmp_path)
    assert status.startswith("ran")
    assert _has(findings, "private key", "CRITICAL")
    # AKIA value in the fixture is 16 uppercase after AKIA -> matches.
    assert any(f["severity"] == "CRITICAL" for f in findings)


def test_secrets_ignores_generic_assignments(tmp_path):
    # The generic `key = "value"` heuristic was removed; only high-signal patterns
    # and detect-secrets (optional) flag secrets now.
    _write(tmp_path, "src/ok.py",
           'password = "hunter2hunter2hunter2"\n'
           'api_key = os.environ["OKX_API_KEY"]\n'
           'secret = "changeme"\n'
           'token = "${TOKEN}"\n')
    findings, _ = audit.check_secrets(tmp_path)
    assert findings == []


def test_secrets_skips_env_files(tmp_path):
    # .env / .env.* are the legitimate home for secrets; not scanned by check_secrets.
    _write(tmp_path, ".env", 'AWS = "AKIAIOSFODNN7EXAMPLE0"\n')
    _write(tmp_path, ".env.production", 'AWS = "AKIAIOSFODNN7EXAMPLE0"\n')
    findings, _ = audit.check_secrets(tmp_path)
    assert findings == []


def test_secrets_ignores_command_substitution_and_spaces(tmp_path):
    _write(tmp_path, "install.sh",
           'SECRET="$(openssl rand -hex 32)"\n'
           'read -r -s -p "  Webhook secret: " val\n')
    findings, _ = audit.check_secrets(tmp_path)
    assert findings == []


def test_secrets_allowlist_pragma_suppresses(tmp_path):
    _write(tmp_path, "tests/fix.py",
           'TEST_KEY = "AKIAIOSFODNN7EXAMPLE0"  # pragma: allowlist secret\n')
    findings, _ = audit.check_secrets(tmp_path)
    assert findings == []


def test_secrets_skips_self_and_baseline(tmp_path):
    # A file literally named like the audit script must not scan its own patterns.
    _write(tmp_path, "scripts/security-audit.py", 'x = "AKIAIOSFODNN7EXAMPLE0"\n')
    _write(tmp_path, ".secrets.baseline", '{"AKIA": "AKIAIOSFODNN7EXAMPLE0"}\n')
    findings, _ = audit.check_secrets(tmp_path)
    assert findings == []


# --------------------------------------------------------------------------- #
# Check 4 — dangerous calls
# --------------------------------------------------------------------------- #

def test_dangerous_flags_eval_exec_pickle_ossystem(tmp_path):
    _write(tmp_path, "src/bad.py",
           "import os, pickle\n"
           "def f(x):\n"
           "    eval(x)\n"
           "    exec(x)\n"
           "    pickle.loads(x)\n"
           "    os.system(x)\n")
    findings, status = audit.check_dangerous(tmp_path)
    assert status == "ran"
    assert _has(findings, "eval()", "CRITICAL")
    assert _has(findings, "exec()", "CRITICAL")
    assert _has(findings, "pickle.loads", "CRITICAL")
    assert _has(findings, "os.system", "HIGH")


def test_dangerous_shell_true_and_yaml_load(tmp_path):
    _write(tmp_path, "src/sh.py",
           "import subprocess, yaml\n"
           "subprocess.run(cmd, shell=True)\n"
           "yaml.load(blob)\n"
           "yaml.load(blob, Loader=yaml.SafeLoader)\n")
    findings, _ = audit.check_dangerous(tmp_path)
    assert _has(findings, "shell=True", "HIGH")
    # yaml.load without loader -> flagged; with SafeLoader -> not.
    yaml_hits = [f for f in findings if "yaml.load" in f["title"]]
    assert len(yaml_hits) == 1


def test_dangerous_ignores_safe_code(tmp_path):
    _write(tmp_path, "src/good.py",
           "import subprocess, json\n"
           "subprocess.run(['ls', '-l'])\n"
           "json.loads('{}')\n")
    findings, _ = audit.check_dangerous(tmp_path)
    assert findings == []


def test_dangerous_survives_syntax_error(tmp_path):
    _write(tmp_path, "src/broken.py", "def f(:\n")
    findings, status = audit.check_dangerous(tmp_path)
    assert status == "ran"
    assert findings == []


# --------------------------------------------------------------------------- #
# Check 5 — HMAC / auth bypass
# --------------------------------------------------------------------------- #

def test_authbypass_flags_timing_unsafe_secret_compare(tmp_path):
    _write(tmp_path, "src/webhook.py",
           "def check(provided, secret):\n"
           "    if provided == secret:\n"
           "        return True\n")
    findings, status = audit.check_authbypass(tmp_path)
    assert status == "ran"
    assert _has(findings, "timing-unsafe equality", "HIGH")


def test_authbypass_ok_with_compare_digest(tmp_path):
    _write(tmp_path, "src/webhook.py",
           "import hmac\n"
           "def check(provided, secret):\n"
           "    return hmac.compare_digest(provided, secret)\n")
    findings, _ = audit.check_authbypass(tmp_path)
    assert not _has(findings, "timing-unsafe equality")


def test_authbypass_skips_test_files(tmp_path):
    _write(tmp_path, "tests/test_x.py", 'assert module.SECRET == "abc"\n')
    findings, _ = audit.check_authbypass(tmp_path)
    assert findings == []


def test_authbypass_asserts_webhook_auth_uses_compare_digest(tmp_path):
    # A webhook_auth.py that hand-rolls == comparison and has no replay handling.
    _write(tmp_path, "src/security/webhook_auth.py",
           "def verify(a, b):\n"
           "    return a == b\n")
    findings, _ = audit.check_authbypass(tmp_path)
    assert _has(findings, "does not use hmac.compare_digest", "HIGH")
    assert _has(findings, "no replay-window", "MEDIUM")


def test_authbypass_benign_none_compare_not_flagged(tmp_path):
    _write(tmp_path, "src/x.py",
           "def f(secret):\n"
           "    if secret == None:\n"
           "        return\n")
    findings, _ = audit.check_authbypass(tmp_path)
    assert not _has(findings, "timing-unsafe equality")


# --------------------------------------------------------------------------- #
# Check 6 — secret logging
# --------------------------------------------------------------------------- #

def test_logging_flags_secret_in_log_call(tmp_path):
    _write(tmp_path, "src/l.py",
           "import logging\n"
           "def f(api_key):\n"
           "    logging.info(api_key)\n")
    findings, status = audit.check_logging(tmp_path)
    assert status == "ran"
    assert _has(findings, "secret passed to info()", "HIGH")


def test_logging_ignores_redacted_and_boolean(tmp_path):
    _write(tmp_path, "src/l.py",
           "import logging\n"
           "def f(secret):\n"
           "    logging.info(redact_secrets(secret))\n"
           "    logging.info('secret_present=%s', has_secret)\n")
    findings, _ = audit.check_logging(tmp_path)
    # redact_secrets(...) call name 'redact_secrets' matches _SECRET_SAFE; has_secret too.
    assert findings == []


def test_logging_skips_tests(tmp_path):
    _write(tmp_path, "tests/test_l.py",
           "import logging\n"
           "logging.info(password)\n")
    findings, _ = audit.check_logging(tmp_path)
    assert findings == []


# --------------------------------------------------------------------------- #
# Check 7 — file permissions
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_perms_flags_group_readable_env(tmp_path):
    p = _write(tmp_path, ".env", "OKX_API_KEY=x\n")
    os.chmod(p, 0o644)
    findings, status = audit.check_perms(tmp_path)
    assert status == "ran"
    assert _has(findings, "group/other-accessible", "HIGH")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_perms_ok_when_600(tmp_path):
    p = _write(tmp_path, ".env", "OKX_API_KEY=x\n")
    os.chmod(p, 0o600)
    findings, _ = audit.check_perms(tmp_path)
    assert findings == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_perms_flags_pem_glob(tmp_path):
    p = _write(tmp_path, "server.pem", "-----BEGIN PRIVATE KEY-----\n")
    os.chmod(p, 0o640)
    findings, _ = audit.check_perms(tmp_path)
    assert any(f["check"] == "perms" for f in findings)


# --------------------------------------------------------------------------- #
# Check 8 — docker
# --------------------------------------------------------------------------- #

def test_docker_flags_root_and_copied_env(tmp_path):
    _write(tmp_path, "Dockerfile",
           "FROM python:3.11-slim\n"
           "COPY .env /app/.env\n"
           "CMD [\"python\", \"app.py\"]\n")
    findings, status = audit.check_docker(tmp_path)
    assert status == "ran"
    assert _has(findings, "no non-root USER", "HIGH")
    assert _has(findings, "secret file copied into image", "CRITICAL")


def test_docker_nonroot_user_ok(tmp_path):
    _write(tmp_path, "Dockerfile",
           "FROM python:3.11-slim\n"
           "RUN useradd app\n"
           "USER app\n"
           "CMD [\"python\", \"app.py\"]\n")
    findings, _ = audit.check_docker(tmp_path)
    assert not _has(findings, "no non-root USER")


def test_docker_compose_privileged_flagged(tmp_path):
    _write(tmp_path, "docker-compose.yml",
           "services:\n"
           "  bad:\n"
           "    image: x\n"
           "    privileged: true\n")
    findings, _ = audit.check_docker(tmp_path)
    assert _has(findings, "runs privileged", "CRITICAL")


def test_docker_skipped_when_absent(tmp_path):
    findings, status = audit.check_docker(tmp_path)
    assert status == "skipped"
    assert findings == []


# --------------------------------------------------------------------------- #
# Check 9 — gitleak (git-tracked secrets)
# --------------------------------------------------------------------------- #

def _git_init_commit(root, files):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    for rel, text in files.items():
        _write(root, rel, text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "x"], check=True)


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_gitleak_flags_tracked_env(tmp_path):
    _git_init_commit(tmp_path, {".env": "OKX_API_KEY=real\n", "a.py": "x = 1\n"})
    findings, status = audit.check_gitleak(tmp_path)
    assert status == "ran"
    assert _has(findings, "tracked in git: .env", "CRITICAL")


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_gitleak_clean_repo(tmp_path):
    _git_init_commit(tmp_path, {"a.py": "x = 1\n", ".gitignore": ".env\n"})
    findings, status = audit.check_gitleak(tmp_path)
    assert status == "ran"
    assert not _has(findings, "tracked in git")


def test_gitleak_non_git_dir_skips(tmp_path):
    findings, status = audit.check_gitleak(tmp_path)
    assert status == "skipped"


# --------------------------------------------------------------------------- #
# Orchestration + report
# --------------------------------------------------------------------------- #

def test_run_audit_only_filter(tmp_path):
    _write(tmp_path, "src/bad.py", "eval(x)\n")
    findings, statuses = audit.run_audit(tmp_path, only={"dangerous"})
    assert statuses["dangerous"] == "ran"
    assert statuses["secrets"] == "off"
    assert all(f["check"] == "dangerous" for f in findings)


def test_run_audit_sorted_by_severity(tmp_path):
    _write(tmp_path, "src/bad.py", "eval(x)\n")            # CRITICAL
    _write(tmp_path, "src/imp.py", "__import__(name)\n")   # MEDIUM (dynamic import)
    findings, _ = audit.run_audit(tmp_path)
    ranks = [audit._SEV_RANK[f["severity"]] for f in findings]
    assert ranks == sorted(ranks)


def test_broken_check_does_not_abort(tmp_path, monkeypatch):
    def boom(root, fast=False):
        del root, fast  # unused; signature must match production check functions (run_audit calls fn(root, fast=fast))
        raise RuntimeError("kaboom")
    monkeypatch.setattr(audit, "CHECKS", [("secrets", boom), ("dangerous", audit.check_dangerous)])
    findings, statuses = audit.run_audit(tmp_path)
    assert statuses["secrets"] == "error"
    assert statuses["dangerous"] == "ran"
    assert _has(findings, "check crashed")


def test_build_report_counts_and_result(tmp_path):
    findings = [
        audit.finding("dangerous", "CRITICAL", "eval()"),
        audit.finding("secrets", "MEDIUM", "maybe"),
    ]
    report = audit.build_report(tmp_path, findings, {"dangerous": "ran"}, "high")
    assert report["counts"]["CRITICAL"] == 1
    assert report["counts"]["MEDIUM"] == 1
    assert report["fail_count"] == 1
    assert report["result"] == "FAIL"


def test_build_report_pass_when_below_threshold(tmp_path):
    findings = [audit.finding("secrets", "MEDIUM", "maybe")]
    report = audit.build_report(tmp_path, findings, {}, "high")
    assert report["result"] == "PASS"
    assert report["fail_count"] == 0


def test_build_report_deterministic_no_timestamp(tmp_path):
    findings = [audit.finding("secrets", "LOW", "x")]
    r1 = audit.build_report(tmp_path, findings, {}, "high")
    r2 = audit.build_report(tmp_path, findings, {}, "high")
    assert r1 == r2
    assert "generated_at" not in r1


# --------------------------------------------------------------------------- #
# main() / CLI + exit codes
# --------------------------------------------------------------------------- #

def test_main_exit_zero_on_clean(tmp_path, capsys):
    _write(tmp_path, "src/ok.py", "x = 1\n")
    rc = audit.main(["--root", str(tmp_path)])
    assert rc == 0
    assert "RESULT: PASS" in capsys.readouterr().out


def test_main_exit_one_on_critical(tmp_path, capsys):
    _write(tmp_path, "src/bad.py", "eval(x)\n")
    rc = audit.main(["--root", str(tmp_path)])
    assert rc == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_main_json_output_is_valid(tmp_path, capsys):
    import json
    _write(tmp_path, "src/bad.py", "eval(x)\n")
    audit.main(["--root", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["tool"] == "hermx-security-audit"
    assert data["result"] == "FAIL"


def test_main_report_file_written(tmp_path):
    import json
    _write(tmp_path, "src/bad.py", "eval(x)\n")
    out = tmp_path / "report.json"
    audit.main(["--root", str(tmp_path), "--report", str(out), "--json"])
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["findings"]


def test_main_unknown_check_errs(tmp_path):
    rc = audit.main(["--root", str(tmp_path), "--only", "bogus"])
    assert rc == 2


def test_main_fail_on_medium(tmp_path, capsys):
    _write(tmp_path, "src/imp.py", "__import__(name)\n")  # MEDIUM (dynamic import)
    assert audit.main(["--root", str(tmp_path), "--fail-on", "high"]) == 0
    capsys.readouterr()
    assert audit.main(["--root", str(tmp_path), "--fail-on", "medium"]) == 1


def test_main_missing_root_errs(tmp_path):
    rc = audit.main(["--root", str(tmp_path / "nope")])
    assert rc == 2


def test_main_fast_skips_external(tmp_path, capsys):
    import json
    _write(tmp_path, "src/ok.py", "x = 1\n")
    audit.main(["--root", str(tmp_path), "--fast", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["checks"]["static"] == "skipped"
    assert data["checks"]["deps"] == "skipped"


def test_main_timestamp_flag_adds_generated_at(tmp_path, capsys):
    import json
    _write(tmp_path, "src/ok.py", "x = 1\n")
    audit.main(["--root", str(tmp_path), "--json", "--timestamp"])
    data = json.loads(capsys.readouterr().out)
    assert "generated_at" in data
