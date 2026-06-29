"""Pure JSONL I/O primitives (Option A leaf extraction).

This module holds the byte-for-byte JSONL append/read primitives that used to
live in webhook_receiver.py: the durable whole-line ``append_jsonl`` (short-write
loop + fsync), its ``append_jsonl_durable`` compatibility alias, and the
crash-tolerant ``read_jsonl_tolerant`` reader that quarantines a torn trailing
line but RAISES on mid-file corruption.

These are genuine leaf primitives: every function takes a ``Path`` argument and
constructs no paths of its own, binds no LOG_DIR/DATA_DIR config, and reads no
mutable global state. It imports only ``json``/``logging``/``os`` and ``Path``.
The path-bound recorders that wrap these (record_pipeline_event,
record_raw_webhook, startup_quarantine_partial_ledgers) deliberately STAY in
webhook_receiver.py until a webhook/config.py exists to own the path constants.

webhook_receiver re-exports every name here for backward compatibility.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path


def append_jsonl(path: Path, obj: dict) -> None:
    """Append one JSONL record atomically + durably (whole-line write + fsync).

    Phase 1 task 2 remainder (REFACTOR_PLAN.md:206): all append_jsonl callers inherit
    durable writes. The whole encoded line (incl. trailing newline) is written via a
    short-write loop on a single unbuffered fd, so the OS can never wedge a HALF-written
    record in front of later appends -- a money-path PLANNED record is all-or-nothing.
    A crash mid-write can only ever leave a clean TRAILING tear, which read_jsonl_tolerant
    quarantines without bricking the ledger."""
    line = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    # buffering=0 => binary, unbuffered: f.write is a direct os.write we can complete.
    with path.open("ab", buffering=0) as f:
        fd = f.fileno()
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - defensive against a stuck fd
                raise OSError(f"append_jsonl: zero-length write to {path}")
            view = view[written:]
        os.fsync(fd)


def append_jsonl_durable(path: Path, obj: dict) -> None:
    """Compatibility alias for durable JSONL appends."""
    append_jsonl(path, obj)


def read_jsonl_tolerant(path: Path) -> list[dict]:
    """Parse a JSONL file, tolerating a truncated/partial *trailing* line
    (REFACTOR_PLAN.md:206, :234). A crash mid-append can leave a half-written
    final line; that line is dropped (and copied to ``<path>.corrupt`` for
    forensics) and reading continues — never raises. An invalid line that is NOT
    the last non-empty line is genuine mid-file corruption: log loudly and raise,
    because silently skipping it would fabricate state."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw:
        return []
    lines = raw.split("\n")
    last_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip():
            last_idx = i
    out: list[dict] = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            if i == last_idx:
                try:
                    (path.parent / (path.name + ".corrupt")).write_text(ln, encoding="utf-8")
                except Exception:
                    pass
                logging.warning("read_jsonl_tolerant: quarantined truncated trailing line in %s", path)
                break
            logging.error("read_jsonl_tolerant: corrupt non-trailing line %d in %s (mid-file corruption)", i, path)
            raise
    return out
