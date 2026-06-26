"""Fixture-corpus drift verifier (REFACTOR_PLAN.md:163, :178 -- Phase 0).

Verifies that every fixture listed in ``tests/fixtures/MANIFEST.sha256`` exists
and matches its recorded SHA-256. Fails on drift (changed/missing fixture) so the
corpus is a stable, shared oracle rather than ad-hoc per-test input.

The corpus itself is built in later Phase 0 work; while the manifest has no
entries this test skips gracefully -- but the verifier structure exists now.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MANIFEST_PATH = FIXTURES_DIR / "MANIFEST.sha256"


def _parse_manifest(text: str) -> list[tuple[str, str]]:
    """Return (sha256_hex, relative_path) entries, skipping comments/blank lines."""
    entries: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)  # hash and path separated by whitespace
        if len(parts) != 2:
            raise ValueError(f"Malformed manifest line: {raw!r}")
        digest, rel_path = parts[0].strip(), parts[1].strip()
        entries.append((digest, rel_path))
    return entries


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def test_fixture_manifest_matches():
    assert MANIFEST_PATH.exists(), "tests/fixtures/MANIFEST.sha256 must exist"
    entries = _parse_manifest(MANIFEST_PATH.read_text(encoding="utf-8"))

    if not entries:
        pytest.skip(
            "Fixture corpus not built yet (MANIFEST.sha256 has no entries); "
            "verifier is in place and will enforce on drift once populated."
        )

    mismatches: list[str] = []
    for expected_digest, rel_path in entries:
        target = FIXTURES_DIR / rel_path
        if not target.exists():
            mismatches.append(f"missing: {rel_path}")
            continue
        actual = _sha256_of(target)
        if actual != expected_digest:
            mismatches.append(
                f"drift: {rel_path} expected {expected_digest[:12]}.. got {actual[:12]}.."
            )

    assert not mismatches, "Fixture corpus drift detected:\n" + "\n".join(mismatches)
