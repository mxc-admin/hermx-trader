"""Control-state CRUD + atomic-write primitives (Phase 2 extraction, REFACTOR_PLAN.md).

Houses the control-state.json cluster that used to live in webhook_receiver.py:
``default_control_state``, ``save_control_state``, ``load_control_state``,
``symbol_pause_info``, ``pause_symbol``, ``set_strategy_override``,
``clear_strategy_override``, ``set_accounting_start``, ``clear_accounting_start``,
``accounting_start_for``, ``set_trading_state``, ``get_trading_state``,
``clear_trading_state`` -- plus the four generic atomic-write helpers
``_canonical_state_json``, ``_fsync_dir``, ``_atomic_json_dump`` and
``_fail_closed_state_write`` (also used by the order journal and record
building, which stay in webhook_receiver.py and call back through the shim).

The root-bound / reload-reset module state these functions read STAYS defined
in webhook_receiver.py (``CONTROL_STATE_FILE``, ``_STATE_WRITE_LOCK``,
``ALERTS_LEDGER``): tests bind CONTROL_STATE_FILE into a tmp HERMX_ROOT via the
per-test ``importlib.reload(webhook_receiver)`` in conftest's ``wr`` fixture --
copies living here would survive the reload and leak across tests. Each
function therefore reads that state lazily via ``import webhook_receiver as
_wr`` -- the same pattern as src/alerts.py (Phase 0) and src/signals/ (Phase 1).
webhook_receiver re-exports every moved name so ``wr.<fn>`` call sites and
monkeypatch seams keep working.

The leaf-pure primitives are imported directly from their extracted homes.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from webhook.timeutil import now_iso
from webhook.ledger_io import append_jsonl


def default_control_state() -> dict:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "mode": "shadow_only",
        "live_trading": "paused",
        "manual_pause": False,
        "pause_reason": "",
        "symbol_pauses": {},
        "strategy_overrides": {},
        # Phase 3 accounting windows: {strategy_id: {accounting_start_at: ms, set_at}}.
        # Locks P&L before the timestamp without deleting ledger history.
        "accounting_windows": {},
        # Phase A (A2) global trading_state: "active" (normal) | "reducing" (risk-off,
        # close-only). MUST live in the default dict or the load_control_state merge
        # ({k in default}) silently drops it -- the same class of bug that once dropped
        # accounting_windows. A simple string, so the merge preserves it with no
        # special re-attach (unlike the dict-valued keys below).
        "trading_state": "active",
        "notes": "Shadow control file. Dashboard/Hermes may read this. Live execution remains disabled here.",
    }


def save_control_state(state: dict) -> None:
    import webhook_receiver as _wr
    with _wr._STATE_WRITE_LOCK:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{_wr.CONTROL_STATE_FILE.name}.", suffix=".tmp", dir=str(_wr.CONTROL_STATE_FILE.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(_wr.CONTROL_STATE_FILE)
            _fsync_dir(_wr.CONTROL_STATE_FILE.parent)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def load_control_state() -> dict:
    import webhook_receiver as _wr
    if not _wr.CONTROL_STATE_FILE.exists():
        state = default_control_state()
        save_control_state(state)
        return state
    try:
        state = json.loads(_wr.CONTROL_STATE_FILE.read_text(encoding="utf-8"))
        default = default_control_state()
        merged = {k: v for k, v in (default | state).items() if k in default}
        merged["symbol_pauses"] = state.get("symbol_pauses") if isinstance(state.get("symbol_pauses"), dict) else {}
        merged["strategy_overrides"] = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
        # Phase 3 accounting windows. Preserved explicitly because the ``if k in
        # default`` merge above would otherwise drop it (same reason symbol_pauses/
        # strategy_overrides are re-attached from the raw state, not the merge).
        merged["accounting_windows"] = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
        # Backward compat: remap legacy override mode labels. "shadow" was the old
        # pause concept (validate+ledger, no orders) -> "pause"; "paper" was the
        # sandbox-submit concept -> "demo"; a stored "pause" stays "pause". Only the
        # display label is rewritten; execution_mode/submit_orders are untouched.
        _legacy = {"shadow": "pause", "paper": "demo", "pause": "pause"}
        for _ov in merged["strategy_overrides"].values():
            if isinstance(_ov, dict) and _ov.get("mode") in _legacy:
                _ov["mode"] = _legacy[_ov["mode"]]
        return merged
    except Exception:
        return default_control_state()


def symbol_pause_info(symbol: "str | None", state: "dict | None" = None) -> "dict | None":
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    cur_state = state if state is not None else load_control_state()
    pauses = cur_state.get("symbol_pauses") if isinstance(cur_state.get("symbol_pauses"), dict) else {}
    pause = pauses.get(sym)
    if isinstance(pause, dict) and pause.get("paused"):
        return pause
    return None


def pause_symbol(symbol: "str | None", reason: str) -> bool:
    """Persist a per-symbol pause artifact (Task 6 operator control)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    state = load_control_state()
    pauses = state.get("symbol_pauses") if isinstance(state.get("symbol_pauses"), dict) else {}
    current = pauses.get(sym) if isinstance(pauses.get(sym), dict) else {}
    next_reason = str(reason or "")[:500]
    if current.get("paused") and current.get("reason") == next_reason:
        return False
    pauses[sym] = {
        "paused": True,
        "paused_at": now_iso(),
        "reason": next_reason,
    }
    state["symbol_pauses"] = pauses
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


_VALID_STRATEGY_MODES = frozenset({"pause", "demo", "live"})
# Legacy UI-mode label remap: old control-state.json may carry shadow/paper.
# "shadow" was the old pause concept (validate+ledger, no orders) -> "pause";
# "paper" was the sandbox-submit concept -> "demo".
_LEGACY_STRATEGY_MODE_ALIASES = {"shadow": "pause", "paper": "demo"}
# Per-mode flag mapping. ``submit_orders`` gates actual submission (pause = off);
# ``execution_mode`` selects the account (demo sandbox vs live real). Must stay in
# sync with dashboard._STRATEGY_MODE_FLAGS.
_STRATEGY_MODE_FLAGS = {
    "pause": {"execution_mode": "demo", "submit_orders": False},
    "demo":  {"execution_mode": "demo", "submit_orders": True},
    "live":  {"execution_mode": "live", "submit_orders": True},
}


def set_strategy_override(strategy_id: str, mode: str) -> bool:
    """Set a per-strategy execution mode override in control-state.json.
    mode must be one of: 'pause' (no orders), 'demo' (sandbox) or 'live' (real venue)."""
    sid = str(strategy_id or "").strip()
    mode = str(mode or "").strip().lower()
    mode = _LEGACY_STRATEGY_MODE_ALIASES.get(mode, mode)
    if not sid or mode not in _VALID_STRATEGY_MODES:
        return False
    flags = _STRATEGY_MODE_FLAGS[mode]
    state = load_control_state()
    overrides = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
    overrides[sid] = {"mode": mode, **flags, "set_at": now_iso()}
    state["strategy_overrides"] = overrides
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def clear_strategy_override(strategy_id: str) -> bool:
    """Remove a strategy override, reverting to the strategy file's execution_mode/submit_orders."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    state = load_control_state()
    overrides = state.get("strategy_overrides") if isinstance(state.get("strategy_overrides"), dict) else {}
    if sid not in overrides:
        return False
    overrides.pop(sid)
    state["strategy_overrides"] = overrides
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def set_accounting_start(strategy_id: str, start_ms: "int | None") -> bool:
    """Set (or clear) a per-strategy accounting-window start in control-state.json.

    ``start_ms`` is a millisecond epoch: P&L from closes strictly before it is locked
    out of the strategy's current window (the ledger keeps the rows; the aggregation
    simply ignores them — see pnl_ledger.read_closed_trades). ``None``/absent clears
    the window. Additive: mirrors set_strategy_override; leaves strategy_overrides
    untouched. Returns True on a successful write."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    if start_ms is None:
        return clear_accounting_start(sid)
    try:
        ts = int(start_ms)
    except (TypeError, ValueError):
        return False
    if ts < 0:
        return False
    state = load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    windows[sid] = {"accounting_start_at": ts, "set_at": now_iso()}
    state["accounting_windows"] = windows
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def clear_accounting_start(strategy_id: str) -> bool:
    """Remove a strategy's accounting window (revert to the whole-ledger total)."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    state = load_control_state()
    windows = state.get("accounting_windows") if isinstance(state.get("accounting_windows"), dict) else {}
    if sid not in windows:
        return False
    windows.pop(sid)
    state["accounting_windows"] = windows
    state["updated_at"] = now_iso()
    save_control_state(state)
    return True


def accounting_start_for(strategy_id: str) -> "int | None":
    """The strategy's accounting-window start (ms epoch), or None if unset."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    windows = load_control_state().get("accounting_windows") or {}
    entry = windows.get(sid)
    if isinstance(entry, dict):
        try:
            v = entry.get("accounting_start_at")
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


# Phase A (A2) -- global trading_state. Collapsed to ONE extra state ("reducing" /
# risk-off): a Nautilus-style HALTED that also blocks closes would contradict HermX's
# deliberate never-block-a-close invariant (a close only REDUCES exposure). "active" is
# normal; "reducing" blocks every non-close order at the ExecutionService gate.
_VALID_TRADING_STATES = frozenset({"active", "reducing"})


def set_trading_state(state: str) -> bool:
    """Set the global trading_state in control-state.json. Validates the input:
    only 'active' or 'reducing' are accepted (anything else is a no-op returning
    False, so a typo can never disarm the gate)."""
    st = str(state or "").strip().lower()
    if st not in _VALID_TRADING_STATES:
        return False
    cs = load_control_state()
    cs["trading_state"] = st
    cs["updated_at"] = now_iso()
    save_control_state(cs)
    return True


def get_trading_state() -> str:
    """Read the global trading_state, defaulting to 'active'. An unknown/legacy value
    fails open to 'active' (normal trading) -- 'reducing' is the safe EXTRA state, and
    the live kill switch still guards real-venue submits independently."""
    st = str(load_control_state().get("trading_state") or "active").strip().lower()
    return st if st in _VALID_TRADING_STATES else "active"


def clear_trading_state() -> bool:
    """Reset trading_state to 'active' (the gate no-op)."""
    return set_trading_state("active")


def _canonical_state_json(state: dict) -> str:
    """Canonical JSON for hashing: sorted keys, compact separators. Independent of
    the pretty-printed on-disk form, so checkpoint formatting cannot affect the
    integrity hash."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a rename/replace inside it is durable."""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError):
        pass


def _atomic_json_dump(path: Path, obj: dict) -> None:
    """Write JSON atomically + durably (tmp -> fsync -> replace -> dir fsync).
    Propagates OSError so the caller can fail closed on a full disk
    (REFACTOR_PLAN.md:221)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)


def _fail_closed_state_write(operation: str, exc: Exception, context: dict | None = None) -> None:
    """A journal/checkpoint write failed (e.g. ENOSPC). Emit a loud, operator-visible
    ERROR + a record on the alert ledger, then let the caller re-raise so the money
    path is BLOCKED rather than proceeding on unpersisted/lost state
    (REFACTOR_PLAN.md:221, fail closed to no-submit). The alert append is
    best-effort: the alert ledger may sit on the same full disk, so the re-raise —
    not the alert — is the real fail-closed guarantee."""
    import webhook_receiver as _wr
    logging.error("STATE WRITE FAILED (%s) -- FAILING CLOSED, submission blocked: %s", operation, exc)
    try:
        append_jsonl(_wr.ALERTS_LEDGER, {"ts": now_iso(), "kind": "state", "alert": "STATE_WRITE_FAILED", "operation": operation, "error": str(exc), "context": context or {}})
    except Exception:
        pass
