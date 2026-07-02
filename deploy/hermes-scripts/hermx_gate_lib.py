"""Shared logic for the HermX Hermes-cron pre-check gate scripts.

Stdlib-only. Ports the deduplication/fingerprint/suppression logic from
``docs/MONITOR_DAEMON_SPEC.md`` §4.2–4.4 into a helper the ``~/.hermes/scripts/``
gate scripts import. It is READ-ONLY against HermX state — the only file it ever
writes is its own monitoring sidecar (``~/.hermes/scripts/.hermx-<concern>.state``),
which is monitoring bookkeeping and MUST NOT be confused with ``control-state.json``.

Design contract (see ``docs/HERMES_CRON_MONITOR_DESIGN.md`` §7):
  * the clock is passed in as ``now_epoch`` — never frozen, never read inline in the
    decision functions, so tests can pin it (repo convention);
  * fingerprints carry NO timestamp / counter / free-text — only the stable identity
    of a condition (the ``normalize()``/``signal_id`` non-determinism failure in
    ``.claude/rules/code-quality.md``);
  * a corrupt/missing sidecar degrades to "notify every active condition once",
    never a hard failure;
  * the sidecar is advanced ONLY when the gate decides to wake (at-least-once).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# info < warning < error < critical (matches emit_operator_alert severities).
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}

# Per-concern suppression windows (seconds). Ported verbatim from the daemon spec §4.3.
# "signal_late" is a slow absence condition (zero-intake) — 1h suppression to avoid spamming.
WINDOW = {"health": 900, "reconcile": 1800, "risk": 3600, "signal_late": 3600}

# Risk states that warrant an operator note (transition INTO one of these wakes).
RISK_ALERT_STATES = {"elevated", "high", "risk_off"}
RISK_SEVERITY = {"elevated": "warning", "high": "error", "risk_off": "critical"}


def severity_rank(sev) -> int:
    return SEVERITY_RANK.get(str(sev or "").lower(), 0)


# --------------------------------------------------------------------------- #
# hermx_ops bootstrap                                                          #
# --------------------------------------------------------------------------- #
def repo_root() -> str:
    """The HermX repo root. Cron jobs set ``--workdir`` to it and export
    ``HERMX_DATA_DIR`` to the same path (design §4.2); fall back to cwd."""
    return os.environ.get("HERMX_DATA_DIR") or os.getcwd()


def import_hermx_ops():
    """Import the shared ``hermx_ops`` lib from the repo (absolute, cwd-independent)."""
    lib = os.path.join(repo_root(), "skills", "hermx-ops", "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import hermx_ops  # noqa: E402

    return hermx_ops


# --------------------------------------------------------------------------- #
# Sidecar state (atomic, self-healing)                                         #
# --------------------------------------------------------------------------- #
def scripts_dir() -> Path:
    return Path(os.environ.get("HERMX_SCRIPTS_DIR") or (Path.home() / ".hermes" / "scripts"))


def state_path(concern: str) -> Path:
    return scripts_dir() / f".hermx-{concern}.state"


def load_state(path) -> dict:
    """Read the sidecar → dict. Missing/corrupt/non-dict → ``{}`` (notify-once,
    self-healing) — never raises."""
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def save_state(path, state: dict) -> None:
    """Atomically persist the sidecar (temp file → fsync → os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Freshness decision                                                           #
# --------------------------------------------------------------------------- #
def is_fresh(entry, severity, now_epoch: float, window: float) -> bool:
    """A condition is *fresh* (worth waking on) if it is unseen, its suppression
    window has elapsed, or its severity escalated since last notification."""
    if not isinstance(entry, dict):
        return True
    last = entry.get("last_notified_epoch")
    if last is None:
        return True
    if severity_rank(severity) > severity_rank(entry.get("last_severity")):
        return True  # escalation bypass (info<warning<error<critical)
    try:
        return (now_epoch - float(last)) >= window
    except (TypeError, ValueError):
        return True


def evaluate(conditions, state: dict, window: float, now_epoch: float):
    """Split ``conditions`` into the fresh set and compute the next sidecar state.

    Returns ``(fresh, new_state)``. ``new_state`` advances ``last_notified_epoch``
    ONLY for fresh fingerprints; existing entries are preserved. Callers persist
    ``new_state`` only when ``fresh`` is non-empty (wake == write)."""
    fresh = []
    new_state = dict(state)
    for c in conditions:
        fp = c["fingerprint"]
        if is_fresh(state.get(fp), c.get("severity"), now_epoch, window):
            fresh.append(c)
            new_state[fp] = {
                "last_notified_epoch": now_epoch,
                "last_severity": str(c.get("severity") or "info").lower(),
            }
    return fresh, new_state


# --------------------------------------------------------------------------- #
# wakeAgent JSON contract                                                      #
# --------------------------------------------------------------------------- #
_CONTEXT_KEYS = ("category", "severity", "title", "fingerprint", "detail")


def emit_sleep() -> None:
    """Emit the $0 skip. The last stdout line is ``{"wakeAgent": false}``."""
    print(json.dumps({"wakeAgent": False}))


def emit_wake(fresh) -> None:
    """Emit the wake with the fresh conditions as injected agent context."""
    alerts = [{k: c.get(k) for k in _CONTEXT_KEYS} for c in fresh]
    print(json.dumps({"wakeAgent": True, "context": {"alerts": alerts}}))


def run_gate(concern: str, conditions, now_epoch: float) -> dict:
    """Shared gate tail: evaluate ``conditions`` for ``concern``, persist on wake,
    emit the wakeAgent line. Returns the decision dict (for tests)."""
    sp = state_path(concern)
    state = load_state(sp)
    fresh, new_state = evaluate(conditions, state, WINDOW[concern], now_epoch)
    if fresh:
        save_state(sp, new_state)
        emit_wake(fresh)
        return {"wakeAgent": True, "fresh": fresh}
    emit_sleep()
    return {"wakeAgent": False, "fresh": []}


# --------------------------------------------------------------------------- #
# Misc                                                                         #
# --------------------------------------------------------------------------- #
def row_epoch(ts):
    """Parse a microsecond-ISO row ``ts`` → epoch seconds, or ``None`` if unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
