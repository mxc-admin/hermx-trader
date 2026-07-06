"""Tests for scripts/hermes-gateway.sh — upsert/backup/masking + mutation guards.

These tests exercise the PRODUCTION script, never a re-implementation:
- helper functions (write_env_var / backup_env / mask) are called directly by
  sourcing the script (its source guard skips main() when sourced);
- subcommand behavior runs the script end-to-end via subprocess with HOME
  pointed at a temp dir, so only a sandboxed ~/.hermes/.env is ever touched.

A stub `hermes` binary is prepended to PATH wherever a subcommand would invoke
the real CLI, keeping the tests hermetic.
"""

import os
import stat
import subprocess
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "hermes-gateway.sh"


def _env(home: Path, extra_path: Optional[Path] = None) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    return env


def _source_call(home: Path, snippet: str) -> subprocess.CompletedProcess:
    """Run a bash snippet with the production script sourced (main() not dispatched)."""
    return subprocess.run(
        ["bash", "-c", 'source "$1" && eval "$2"', "_", str(SCRIPT), snippet],
        capture_output=True,
        text=True,
        env=_env(home),
    )


def _run(home: Path, args, stdin: str = "", extra_path: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=_env(home, extra_path),
    )


def _stub_hermes(tmp_path: Path) -> Path:
    """A fake `hermes` on PATH so lifecycle/status calls never reach a real CLI."""
    d = tmp_path / "stub-bin"
    d.mkdir(exist_ok=True)
    stub = d / "hermes"
    stub.write_text('#!/usr/bin/env bash\necho "stub-hermes $*"\n')
    stub.chmod(0o755)
    return d


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".hermes").mkdir()
    return tmp_path


def _seed(home: Path, text: str) -> Path:
    env_file = home / ".hermes" / ".env"
    env_file.write_text(text)
    return env_file


def _env_lines(env_file: Path) -> dict:
    out = {}
    for line in env_file.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


# --- upsert (write_env_var) ---------------------------------------------------

def test_upsert_inserts_new_var_and_preserves_other_lines(home):
    env_file = _seed(home, "OTHER=1\n# comment\n")
    r = _source_call(home, "write_env_var TELEGRAM_BOT_TOKEN tok123")
    assert r.returncode == 0, r.stderr
    lines = _env_lines(env_file)
    assert lines["TELEGRAM_BOT_TOKEN"] == "tok123"
    assert lines["OTHER"] == "1"
    assert "# comment" in env_file.read_text()


def test_upsert_replaces_existing_var_in_place(home):
    env_file = _seed(home, "TELEGRAM_BOT_TOKEN=old\nOTHER=1\n")
    r = _source_call(home, "write_env_var TELEGRAM_BOT_TOKEN new")
    assert r.returncode == 0, r.stderr
    text = env_file.read_text()
    assert text.count("TELEGRAM_BOT_TOKEN=") == 1
    assert _env_lines(env_file)["TELEGRAM_BOT_TOKEN"] == "new"
    assert "old" not in text


def test_upsert_leaves_env_file_mode_600(home):
    env_file = _seed(home, "")
    r = _source_call(home, "write_env_var TELEGRAM_BOT_TOKEN t")
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


# --- backup (backup_env) --------------------------------------------------------

def test_backup_env_snapshots_prior_contents(home):
    env_file = _seed(home, "TELEGRAM_BOT_TOKEN=before\n")
    r = _source_call(home, "backup_env && write_env_var TELEGRAM_BOT_TOKEN after")
    assert r.returncode == 0, r.stderr
    bak = home / ".hermes" / ".env.bak"
    assert bak.read_text() == "TELEGRAM_BOT_TOKEN=before\n"
    assert _env_lines(env_file)["TELEGRAM_BOT_TOKEN"] == "after"


# --- masking (mask) -------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("", "(empty)"),
        ("abcd", "****"),
        ("123456789:VERYSECRET", "1234****"),
    ],
)
def test_mask_reveals_at_most_four_leading_chars(home, value, expected):
    r = _source_call(home, f'mask "{value}"')
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected


def test_status_masks_token_and_shows_allowlist(home, tmp_path):
    _seed(home, "TELEGRAM_BOT_TOKEN=123456789:VERYSECRETVALUE\nTELEGRAM_ALLOWED_USERS=987654321\n")
    r = _run(home, ["status"], extra_path=_stub_hermes(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "VERYSECRETVALUE" not in out  # never the token, anywhere
    assert "1234****" in out
    assert "987654321" in out  # user ids are not secrets


def test_status_warns_when_token_set_but_allowlist_empty(home, tmp_path):
    _seed(home, "TELEGRAM_BOT_TOKEN=123456789:VERYSECRETVALUE\n")
    r = _run(home, ["status"], extra_path=_stub_hermes(tmp_path))
    assert "denies ALL" in (r.stdout + r.stderr)


# --- mutation guards -------------------------------------------------------------

def test_allow_rejects_non_numeric_id_before_any_prompt(home):
    env_file = _seed(home, "TELEGRAM_ALLOWED_USERS=111\n")
    r = _run(home, ["allow", "@not-an-id"])
    assert r.returncode != 0
    assert _env_lines(env_file)["TELEGRAM_ALLOWED_USERS"] == "111"


def test_allow_appends_after_exact_typed_confirmation(home):
    env_file = _seed(home, "TELEGRAM_ALLOWED_USERS=111\n")
    r = _run(home, ["allow", "222"], stdin="yes, allow 222\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _env_lines(env_file)["TELEGRAM_ALLOWED_USERS"] == "111,222"


def test_allow_wrong_confirmation_writes_nothing(home):
    env_file = _seed(home, "TELEGRAM_ALLOWED_USERS=111\n")
    r = _run(home, ["allow", "222"], stdin="yes\n")
    assert r.returncode != 0
    assert _env_lines(env_file)["TELEGRAM_ALLOWED_USERS"] == "111"


def test_revoke_removes_only_the_named_id(home):
    env_file = _seed(home, "TELEGRAM_ALLOWED_USERS=111,222,333\n")
    r = _run(home, ["revoke", "222"], stdin="yes, revoke 222\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _env_lines(env_file)["TELEGRAM_ALLOWED_USERS"] == "111,333"


def test_revoke_last_id_warns_gateway_denies_all(home):
    env_file = _seed(home, "TELEGRAM_ALLOWED_USERS=111\n")
    r = _run(home, ["revoke", "111"], stdin="yes, revoke 111\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "EMPTY" in (r.stdout + r.stderr)
    assert _env_lines(env_file)["TELEGRAM_ALLOWED_USERS"] == ""


def test_setup_hard_errors_without_a_tty(home):
    # read -s needs a TTY; piped stdin must be refused before any prompt.
    r = _run(home, ["setup"], stdin="")
    assert r.returncode != 0
    assert "TTY" in (r.stdout + r.stderr)


def test_remove_blanks_both_vars_and_keeps_backup(home, tmp_path):
    env_file = _seed(home, "TELEGRAM_BOT_TOKEN=123456789:VERYSECRETVALUE\nTELEGRAM_ALLOWED_USERS=111\n")
    r = _run(home, ["remove"], stdin="yes, remove telegram\n", extra_path=_stub_hermes(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _env_lines(env_file)
    assert lines["TELEGRAM_BOT_TOKEN"] == ""
    assert lines["TELEGRAM_ALLOWED_USERS"] == ""
    bak = home / ".hermes" / ".env.bak"
    assert "VERYSECRETVALUE" in bak.read_text()  # rolling backup retains prior state
    assert "VERYSECRETVALUE" not in (r.stdout + r.stderr)  # but is never printed
