"""Shared helpers for the HermX slash-command skills.

Stdlib-only. Talks to the local HermX dashboard/receiver over loopback and reads
the on-disk strategy files, control-state, and JSONL logs. Every network/file read
degrades to an explicit UNKNOWN sentinel — never a fabricated "flat" view.

See ``skills/hermx-ops/references/api-contract.md`` for the canonical contract this
module encodes (endpoints, response shapes, freshness, UNKNOWN-never-flat, trace join).

Phase 1 (read-only): NEVER POSTs, NEVER submits an order, NEVER sets a size.
Phase 2 (mutating): adds a *small*, audited set of POST/file-write wrappers used by
the mutating skills (`/strategy-mode`, `/close`, `/emergency-stop`). These never set
an order size (sizing is owned by the execution layer), never route via `/webhook`,
and never overwrite a corrupt control-state file. See the "Mutating helpers (Phase 2)"
section at the bottom of this module.
"""

from __future__ import annotations

import difflib
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DASHBOARD_BASE = os.environ.get("HERMX_DASHBOARD_BASE", "http://127.0.0.1:8098")
RECEIVER_BASE = os.environ.get("HERMX_RECEIVER_BASE", "http://127.0.0.1:8891")
HTTP_TIMEOUT_SECONDS = float(os.environ.get("HERMX_HTTP_TIMEOUT", "5"))

# Resolve on-disk state paths against HERMX_DATA_DIR so the skills read/write the
# SAME control-state.json and strategies/ that the server does. Defaults to "." for
# a repo-root invocation when HERMX_DATA_DIR is unset.
CONTROL_STATE_PATH = Path(os.environ.get("HERMX_DATA_DIR", ".")) / "control-state.json"
STRATEGIES_DIR = Path(os.environ.get("HERMX_DATA_DIR", ".")) / "strategies"

# Sentinel for any value we could not determine. Distinct from FLAT/empty on purpose.
UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Low-level fetch                                                              #
# --------------------------------------------------------------------------- #
def _get_json(base, path, secret=None, timeout=HTTP_TIMEOUT_SECONDS):
    """GET {base}{path} → (payload_dict, error_str|None).

    Adds the dashboard token only when a secret is supplied (HERMX_DASH_AUTH on).
    Any transport/status/parse problem yields (None, "<reason>"): the caller must
    treat that as a read failure (UNKNOWN), not as an empty result.
    """
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    if secret:
        req.add_header("X-Dashboard-Token", secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw), None
    except urllib.error.HTTPError as exc:
        # Try to surface the JSON error body (e.g. {"error":"latest_unreadable"}).
        body = None
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return body, f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable:{exc}"
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"bad_json:{exc}"


def safe_json_load(path):
    """Load a JSON file → (obj, error_str|None). Missing/corrupt never raises."""
    p = Path(path)
    if not p.exists():
        return None, "missing"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"unreadable:{exc}"


def _iter_jsonl(path, limit=None):
    """Yield parsed rows from a .jsonl file, skipping corrupt lines silently.

    Bounded-tail read when ``limit`` is set: returns only the last ``limit`` rows,
    matching the dashboard's bounded-reader posture on large ledgers.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit * 4:]  # over-read; corrupt lines get dropped below
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (ValueError, json.JSONDecodeError):
            continue
    if limit is not None:
        rows = rows[-limit:]
    return rows


# --------------------------------------------------------------------------- #
# State                                                                        #
# --------------------------------------------------------------------------- #
def read_state(dashboard_base=DASHBOARD_BASE, secret=None):
    """Fetch /api and /health; normalize into a single state dict.

    Encodes the UNKNOWN-never-flat rule: positions are UNKNOWN unless the executor
    read is present, ``ok``, and not degraded. Returns a dict shaped like::

        {
          "reachable": {"dashboard": bool, "receiver": bool},
          "armed": bool | "UNKNOWN",
          "mode": str | "UNKNOWN",
          "kill_switch_engaged": bool | "UNKNOWN",
          "strategy_count": int | "UNKNOWN",
          "positions": {sym: {...}} | "UNKNOWN",
          "positions_status": "OK" | "UNKNOWN",
          "freshness": "OK" | "STALE" | "UNKNOWN",
          "executor": {...} | None,
          "errors": {"api": ..., "health": ..., "receiver": ...},
        }
    """
    receiver_secret = secret  # receiver /health is unauthenticated; kept for symmetry
    api, api_err = _get_json(dashboard_base, "/api", secret=secret)
    health, health_err = _get_json(dashboard_base, "/health", secret=secret)
    rhealth, rhealth_err = _get_json(RECEIVER_BASE, "/health")

    state = {
        "reachable": {
            "dashboard": bool(health) or bool(api),
            "receiver": bool(rhealth and rhealth.get("ok")),
        },
        "armed": UNKNOWN,
        "mode": UNKNOWN,
        "kill_switch_engaged": UNKNOWN,
        "strategy_count": UNKNOWN,
        "positions": UNKNOWN,
        "positions_status": UNKNOWN,
        "freshness": UNKNOWN,
        "executor": None,
        "errors": {"api": api_err, "health": health_err, "receiver": rhealth_err},
    }

    if isinstance(health, dict) and health.get("ok"):
        arm = health.get("arm") or {}
        state["armed"] = bool(arm.get("armed"))
        state["kill_switch_engaged"] = bool(arm.get("kill_switch_engaged"))
        state["mode"] = health.get("mode") or UNKNOWN
        files = health.get("strategy_files")
        if isinstance(files, list):
            state["strategy_count"] = len(files)

    if isinstance(api, dict):
        state["executor"] = api.get("executor") or {}
        state["freshness"] = freshness_flag(api)
        okx = api.get("okx_live") or {}
        executor = api.get("executor") or {}
        # UNKNOWN-never-flat: only trust positions on a healthy, non-degraded read.
        if okx.get("ok") and not executor.get("degraded"):
            state["positions"] = okx.get("positions") or {}
            state["positions_status"] = "OK"
        # else: leave positions == UNKNOWN
    return state


def freshness_flag(state):
    """Derive OK/STALE/UNKNOWN from an /api payload's executor + freshness blocks.

    ``state`` is the parsed /api dict (or ``None``). STALE if the executor is
    degraded/stale or the model freshness is stale/no_data. UNKNOWN if we have no
    executor/freshness info at all.
    """
    if not isinstance(state, dict):
        return UNKNOWN
    executor = state.get("executor")
    freshness = state.get("freshness")
    if not isinstance(executor, dict) and not isinstance(freshness, dict):
        return UNKNOWN
    executor = executor if isinstance(executor, dict) else {}
    freshness = freshness if isinstance(freshness, dict) else {}
    if executor.get("degraded") or executor.get("stale"):
        return "STALE"
    if freshness.get("no_data") or freshness.get("stale"):
        return "STALE"
    if executor.get("ok"):
        return "OK"
    return UNKNOWN


# --------------------------------------------------------------------------- #
# Positions formatting                                                         #
# --------------------------------------------------------------------------- #
def format_positions(positions):
    """Render positions as an aligned text table.

    ``positions == UNKNOWN`` (or the string) → an explicit UNKNOWN line, never a
    "flat"/empty table. A healthy empty map → 'FLAT (no open positions)'.
    """
    if positions == UNKNOWN or not isinstance(positions, dict):
        return "positions: UNKNOWN (executor read failed or stale — not flat)"
    open_rows = {s: p for s, p in positions.items()
                 if str((p or {}).get("side", "")).upper() not in ("", "FLAT")}
    if not open_rows:
        return "FLAT (no open positions)"
    headers = ["SYMBOL", "SIDE", "POS", "AVG_PX", "MARK", "UPL", "LEV", "MGN"]
    rows = [headers]
    for sym, p in open_rows.items():
        p = p or {}
        rows.append([
            str(sym),
            str(p.get("side", "?")),
            _fmt_num(p.get("pos")),
            _fmt_num(p.get("avg_px")),
            _fmt_num(p.get("mark_px") or p.get("last")),
            _fmt_num(p.get("upl")),
            str(p.get("leverage", "-")),
            str(p.get("margin_mode", "-")),
        ])
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows)


def _fmt_num(v):
    if v is None:
        return "-"
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------------------- #
# Strategies                                                                   #
# --------------------------------------------------------------------------- #
def _symbol_from_inst_id(inst_id):
    """BTC-USDT-SWAP → BTCUSDT. Best-effort; falls back to the raw string."""
    if not inst_id:
        return ""
    parts = str(inst_id).split("-")
    if len(parts) >= 2:
        return (parts[0] + parts[1]).upper()
    return str(inst_id).upper()


def _effective_mode(strategy, override):
    """Mirror of dashboard `_effective_strategy_mode` (override > pause > file)."""
    if isinstance(override, dict) and override.get("mode"):
        return str(override["mode"]).lower()
    if strategy.get("submit_orders") is False:
        return "pause"
    return str(strategy.get("execution_mode") or "demo").lower()


def list_strategies(strategies_dir, control_state_path=None):
    """List strategy dicts merged with control-state overrides / symbol pauses.

    Each item::

        {"id", "name", "symbol", "timeframe", "file_mode", "effective_mode",
         "paused", "budget_usd", "leverage", "inst_id", "file"}

    ``file_mode`` is the strategy file's own execution_mode; ``effective_mode`` folds
    in the control-state override. ``paused`` is true when effective_mode == "pause"
    or the symbol is paused in control-state.

    ``control_state_path`` defaults to the env-aware ``CONTROL_STATE_PATH`` (resolved
    against ``HERMX_DATA_DIR``) so callers pick up the same file the server writes;
    pass an explicit path to override.
    """
    if control_state_path is None:
        control_state_path = str(CONTROL_STATE_PATH)
    overrides, symbol_pauses = {}, {}
    if control_state_path:
        ctrl, _err = safe_json_load(control_state_path)
        if isinstance(ctrl, dict):
            overrides = ctrl.get("strategy_overrides") or {}
            symbol_pauses = ctrl.get("symbol_pauses") or {}

    out = []
    for path in sorted(Path(strategies_dir).glob("*.json")):
        data, err = safe_json_load(path)
        if not isinstance(data, dict):
            out.append({"id": path.stem, "name": UNKNOWN, "symbol": UNKNOWN,
                        "timeframe": UNKNOWN, "file_mode": UNKNOWN,
                        "effective_mode": UNKNOWN, "paused": UNKNOWN,
                        "budget_usd": None, "leverage": None, "inst_id": None,
                        "file": path.name, "error": err})
            continue
        sid = data.get("strategy_id") or path.stem
        inst_id = ((data.get("instrument") or {}).get("inst_id"))
        symbol = _symbol_from_inst_id(inst_id)
        ov = overrides.get(sid) if isinstance(overrides, dict) else None
        eff = _effective_mode(data, ov)
        # symbol_pauses entry may be a bool or a {"paused": ...} object.
        sp = symbol_pauses.get(symbol) if isinstance(symbol_pauses, dict) else None
        sym_paused = bool(sp.get("paused")) if isinstance(sp, dict) else bool(sp)
        out.append({
            "id": sid,
            "name": data.get("name") or sid,
            "symbol": symbol or UNKNOWN,
            "timeframe": data.get("timeframe") or UNKNOWN,
            "file_mode": str(data.get("execution_mode") or "demo").lower(),
            "effective_mode": eff,
            "paused": (eff == "pause") or sym_paused,
            "budget_usd": (data.get("capital") or {}).get("budget_usd"),
            "leverage": data.get("leverage"),
            "inst_id": inst_id,
            "file": path.name,
        })
    return out


def resolve_strategy(arg, strategies_dir):
    """Resolve a friendly name/id/symbol/basename → resolution dict.

    Precedence (first match wins, higher = more authoritative):
        1. exact strategy_id
        2. exact file basename (with or without .json)
        3. exact symbol — accepted only if UNIQUE across strategies
        4. fuzzy — NEVER auto-applied; returns candidates for the caller to confirm

    Returns::

        {"resolved": strategy_id|None, "match": "id|basename|symbol|None",
         "ambiguous": bool, "candidates": [ids], "reason": str}
    """
    arg_s = str(arg or "").strip()
    strategies = list_strategies(strategies_dir)
    by_id = {s["id"]: s for s in strategies}

    if arg_s in by_id:
        return {"resolved": arg_s, "match": "id", "ambiguous": False,
                "candidates": [arg_s], "reason": "exact strategy_id"}

    base = arg_s[:-5] if arg_s.endswith(".json") else arg_s
    for s in strategies:
        if s["file"] == arg_s or Path(s["file"]).stem == base:
            return {"resolved": s["id"], "match": "basename", "ambiguous": False,
                    "candidates": [s["id"]], "reason": "exact file basename"}

    sym = arg_s.upper()
    sym_matches = [s["id"] for s in strategies if str(s.get("symbol", "")).upper() == sym]
    if len(sym_matches) == 1:
        return {"resolved": sym_matches[0], "match": "symbol", "ambiguous": False,
                "candidates": sym_matches, "reason": "exact symbol (unique)"}
    if len(sym_matches) > 1:
        return {"resolved": None, "match": "symbol", "ambiguous": True,
                "candidates": sym_matches,
                "reason": f"symbol {sym} maps to {len(sym_matches)} strategies — specify strategy_id"}

    # Fuzzy — never auto-apply.
    pool = list(by_id.keys()) + [s["file"] for s in strategies] + \
        [str(s.get("symbol", "")) for s in strategies]
    close = difflib.get_close_matches(arg_s, pool, n=5, cutoff=0.6)
    cand_ids = []
    for c in close:
        for s in strategies:
            if c in (s["id"], s["file"], str(s.get("symbol", ""))) and s["id"] not in cand_ids:
                cand_ids.append(s["id"])
    return {"resolved": None, "match": None, "ambiguous": bool(cand_ids),
            "candidates": cand_ids,
            "reason": "no exact match — fuzzy candidates (confirm before applying)"
            if cand_ids else "no match"}


# --------------------------------------------------------------------------- #
# Trace / correlation                                                          #
# --------------------------------------------------------------------------- #
def _received_at(row):
    """Pull the join key from any log row (signals use first_seen_at/ts)."""
    if not isinstance(row, dict):
        return None
    return row.get("received_at") or row.get("first_seen_at") or row.get("ts")


def correlate_trace(key, logs_dir):
    """Join raw-webhooks → signals → pipeline → executions on ``received_at``.

    ``key`` is a ``received_at`` value (microsecond ISO). Returns::

        {"received_at", "found": bool, "time_less": bool,
         "raw": row|None, "signal": row|None,
         "pipeline": [rows], "executions": [rows]}

    Time-less handling: if the matched intake payload has no ``tv_time`` its
    ``signal_id`` was wall-clock-derived and non-deterministic — we DO NOT re-derive
    or match on it; correlation stays keyed on the stable ``received_at`` and we set
    ``time_less: True`` so the caller can flag it.
    """
    logs = Path(logs_dir)
    raw_rows = _iter_jsonl(logs / "raw-webhooks.jsonl")
    sig_rows = _iter_jsonl(logs / "signals.jsonl")
    pipe_rows = _iter_jsonl(logs / "pipeline.jsonl")
    exec_rows = _iter_jsonl(logs / "executions.jsonl")

    raw = next((r for r in raw_rows if _received_at(r) == key), None)
    signal = next((r for r in sig_rows if _received_at(r) == key), None)
    pipeline = [r for r in pipe_rows if _received_at(r) == key]
    executions = [r for r in exec_rows if _received_at(r) == key]

    payload = (raw or {}).get("payload") or {}
    time_less = raw is not None and not payload.get("tv_time")

    return {
        "received_at": key,
        "found": any([raw, signal, pipeline, executions]),
        "time_less": time_less,
        "raw": raw,
        "signal": signal,
        "pipeline": pipeline,
        "executions": executions,
    }


def find_traces_by_symbol(symbol, logs_dir, limit=10):
    """Return recent (received_at, symbol, side) intake rows matching ``symbol``.

    Newest first. Used by /trace when given a symbol instead of a received_at:
    surface recent matches and let the caller default to the latest.
    """
    sym = str(symbol or "").upper()
    raw_rows = _iter_jsonl(Path(logs_dir) / "raw-webhooks.jsonl", limit=limit * 20)
    hits = []
    for r in raw_rows:
        payload = (r or {}).get("payload") or {}
        if str(payload.get("symbol", "")).upper() == sym:
            hits.append({
                "received_at": _received_at(r),
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "tv_time": payload.get("tv_time"),
                "time_less": not payload.get("tv_time"),
            })
    hits.reverse()  # newest first
    return hits[:limit]


# --------------------------------------------------------------------------- #
# Mutating helpers (Phase 2)                                                   #
# --------------------------------------------------------------------------- #
# These are the ONLY functions in this module that write. Every one is a thin,
# audited wrapper. Invariants they preserve:
#   * never set/suggest an order size (close is reduce-only; server derives size)
#   * never route via /webhook (the only order-creation path)
#   * a transport/parse failure → UNKNOWN outcome, never a fabricated success
#   * a corrupt control-state file is refused, never overwritten
def _now_iso():
    """UTC ISO-8601 with offset, matching the receiver's now_iso() shape."""
    return datetime.now(timezone.utc).isoformat()


def _post_json(base, path, payload, secret=None, timeout=HTTP_TIMEOUT_SECONDS):
    """POST JSON to {base}{path} → (payload_dict|None, error_str|None).

    Mirrors ``_get_json`` semantics: a non-2xx status returns the parsed error body
    plus ``http_<code>``; a transport/parse failure returns ``(None, "<reason>")``.
    The ``X-Dashboard-Token`` header is added whenever a secret is supplied — the
    close/control endpoints fail closed on a blank/absent token, so callers should
    always pass ``secret`` (a blank one surfaces as a 401 the caller can report).
    """
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("X-Dashboard-Token", secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as exc:
        body = None
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return body, f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable:{exc}"
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"bad_json:{exc}"


# resume is a UI verb; the wire protocol clears the override to restore the file mode.
_MODE_WIRE = {"resume": "clear", "clear": "clear",
              "pause": "pause", "demo": "demo", "live": "live"}


def post_strategy_mode(dashboard_base, secret, strategy_id, mode):
    """POST /api/control/strategy/{id} on the dashboard → normalized result dict.

    Maps the friendly ``mode`` to the wire value (``resume`` → ``clear``) and reports:
        {"outcome": "applied"|"rejected"|"UNKNOWN", "wire_mode", "reason",
         "http", "raw"}
    - transport/parse failure or 5xx → UNKNOWN (we cannot confirm the override).
    - 401 → rejected (unauthorized: HERMX_SECRET missing/wrong when auth is on).
    - 404 → rejected (unknown strategy_id). 400 → rejected (invalid mode).
    - 200 ok → applied.
    """
    wire = _MODE_WIRE.get(str(mode or "").strip().lower(), str(mode or "").strip().lower())
    path = "/api/control/strategy/" + quote(str(strategy_id or ""), safe="")
    body, err = _post_json(dashboard_base, path, {"mode": wire}, secret=secret)
    out = {"outcome": UNKNOWN, "wire_mode": wire, "reason": None,
           "http": err, "raw": body}
    berr = body.get("error") if isinstance(body, dict) else None
    # Transport/parse failure (never reached the server) → indeterminate.
    if err is None:
        if isinstance(body, dict) and body.get("ok"):
            out["outcome"] = "applied"
        else:
            out["outcome"] = "rejected"
            out["reason"] = berr or "rejected"
    elif err == "http_401":
        out["outcome"] = "rejected"
        out["reason"] = berr or "unauthorized (HERMX_SECRET missing/wrong)"
    elif err in ("http_404", "http_400"):
        out["outcome"] = "rejected"
        out["reason"] = berr or err
    elif err.startswith("http_5"):  # server error → cannot confirm
        out["reason"] = berr or err
    else:  # unreachable / bad_json → indeterminate
        out["reason"] = berr or err or "no_response"
    return out


def post_close(receiver_base, secret, symbol, strategy_id, operator, reason):
    """POST /api/close on the receiver → normalized result dict. Reduce-only.

    Body is exactly ``{"symbol", "strategy_id", "operator", "reason"}`` — NO size is
    ever sent; the receiver derives a reduce-only close from the live position. Result:
        {"outcome": "submitted"|"not_submitted"|"UNKNOWN", "reason", "cl_ord_id",
         "symbol", "http", "raw"}
    - transport/parse failure or 5xx → UNKNOWN (order may or may not have been sent).
    - 200 ok:true → submitted (carries cl_ord_id).
    - 200 mode:not_submitted, or 400/401/404 → not_submitted (nothing was sent).
    """
    payload = {"symbol": symbol, "strategy_id": strategy_id,
               "operator": operator, "reason": reason}
    body, err = _post_json(receiver_base, "/api/close", payload, secret=secret)
    out = {"outcome": UNKNOWN, "reason": None, "cl_ord_id": None,
           "symbol": symbol, "http": err, "raw": body}
    breason = None
    if isinstance(body, dict):
        breason = body.get("reason") or body.get("error")
    # Transport/parse failure or 5xx → the order may or may not have been sent.
    if (err is not None and not err.startswith("http_")) or (err or "").startswith("http_5"):
        out["reason"] = breason or err or "no_response"
        return out
    if err is None and isinstance(body, dict) and body.get("ok") and body.get("mode") == "submitted":
        out["outcome"] = "submitted"
        out["cl_ord_id"] = body.get("cl_ord_id")
        out["reason"] = "submitted"
        return out
    # Any other 200 (mode:not_submitted / non-ok) or 400/401/404 → definitively not sent.
    out["outcome"] = "not_submitted"
    out["reason"] = breason or err or "not_submitted"
    return out


def read_position_for_symbol(state, symbol):
    """Resolve one symbol's position from a ``read_state()`` result → status dict.

    Encodes UNKNOWN-never-flat: an unreadable/stale executor read is UNKNOWN, never
    "nothing to close". Returns::

        {"status": "UNKNOWN"|"FLAT"|"OPEN", "symbol", "side", "size",
         "position": {...}|None}
    """
    sym = str(symbol or "").strip().upper()
    positions = (state or {}).get("positions")
    if positions == UNKNOWN or not isinstance(positions, dict):
        return {"status": UNKNOWN, "symbol": sym, "side": UNKNOWN,
                "size": UNKNOWN, "position": None}
    pos = None
    for key, val in positions.items():
        if str(key).upper() == sym:
            pos = val or {}
            break
    side = str((pos or {}).get("side", "")).upper()
    if not pos or side in ("", "FLAT"):
        return {"status": "FLAT", "symbol": sym, "side": "FLAT",
                "size": 0, "position": pos}
    return {"status": "OPEN", "symbol": sym, "side": side,
            "size": pos.get("pos"), "position": pos}


def safe_update_control_state(path, mutator):
    """Atomic read-modify-write of a control-state JSON file. Refuses on parse failure.

    Reads ``path`` → applies ``mutator(state)`` (which mutates in place and/or returns
    the new dict) → bumps ``updated_at`` → writes a temp file → fsync → ``os.replace``.
    A missing file starts from ``{}`` (fresh create); a file that exists but does NOT
    parse as a JSON object is **refused** (never overwritten, to preserve operator
    state). Returns::

        {"ok": bool, "changed": bool, "error": str|None,
         "before": <obj>, "after": <obj>, "diff": <unified-diff str>}
    """
    p = Path(path)
    obj, err = safe_json_load(path)
    if err == "missing":
        obj = {}
    elif err is not None:
        return {"ok": False, "changed": False, "error": f"refused: {err}",
                "before": None, "after": None, "diff": ""}
    if not isinstance(obj, dict):
        return {"ok": False, "changed": False, "error": "refused: not_a_json_object",
                "before": obj, "after": None, "diff": ""}

    before_txt = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    result = mutator(obj)
    new_obj = result if isinstance(result, dict) else obj
    new_obj["updated_at"] = _now_iso()
    after_txt = json.dumps(new_obj, indent=2, sort_keys=True, ensure_ascii=False)

    diff = "".join(difflib.unified_diff(
        before_txt.splitlines(keepends=True),
        after_txt.splitlines(keepends=True),
        fromfile=f"{p.name} (before)", tofile=f"{p.name} (after)"))

    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent or "."))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(new_obj, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"ok": True, "changed": before_txt != after_txt, "error": None,
            "before": obj, "after": new_obj, "diff": diff}


def preview_control_state_update(path, mutator):
    """Dry-run companion to ``safe_update_control_state``: compute the diff WITHOUT
    writing. Same return shape (``ok``/``changed``/``diff``) but never touches disk."""
    obj, err = safe_json_load(path)
    if err == "missing":
        obj = {}
    elif err is not None:
        return {"ok": False, "changed": False, "error": f"refused: {err}",
                "before": None, "after": None, "diff": ""}
    if not isinstance(obj, dict):
        return {"ok": False, "changed": False, "error": "refused: not_a_json_object",
                "before": obj, "after": None, "diff": ""}
    import copy
    working = copy.deepcopy(obj)
    before_txt = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    result = mutator(working)
    new_obj = result if isinstance(result, dict) else working
    new_obj["updated_at"] = "<bumped on write>"
    after_txt = json.dumps(new_obj, indent=2, sort_keys=True, ensure_ascii=False)
    diff = "".join(difflib.unified_diff(
        before_txt.splitlines(keepends=True),
        after_txt.splitlines(keepends=True),
        fromfile=f"{Path(path).name} (before)", tofile=f"{Path(path).name} (after)"))
    return {"ok": True, "changed": before_txt != after_txt, "error": None,
            "before": obj, "after": new_obj, "diff": diff}
