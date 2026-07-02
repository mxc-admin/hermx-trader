#!/usr/bin/env python3
"""HermX risk-watch pre-check gate (Hermes cron, every 15m).

Honors the local ``risk_index_gate_enabled`` flag in ``control-state.json`` (fail-open
when false/absent). When enabled, fetches the MXC Kinetic dashboard, parses ``risk_state``,
and wakes only on a transition INTO ``{elevated, high, risk_off}`` (per
``docs/MONITOR_DAEMON_SPEC.md`` §6.4; suppression window 3600s, escalation bypass).
MXC unreachable / unparseable → ``{"wakeAgent": false}`` (fail-open, never a false alarm).

READ-ONLY: never mutates HermX state. The only write is the risk sidecar.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402

MXC_BASE = os.environ.get("HERMX_MXC_BASE", "https://mxc-kinetic-crypto.replit.app")
HTTP_TIMEOUT = float(os.environ.get("HERMX_HTTP_TIMEOUT", "5"))


def gate_enabled(ops, repo) -> bool:
    """Read ``risk_index_gate_enabled`` from control-state (call-time, env-aware).
    Absent/false/unreadable → False (fail-open: no risk alerts)."""
    ctrl_path = os.path.join(repo, "control-state.json")
    ctrl, _err = ops.safe_json_load(ctrl_path)
    return bool(isinstance(ctrl, dict) and ctrl.get("risk_index_gate_enabled"))


def fetch_mxc():
    """GET the MXC dashboard → parsed dict, or ``None`` on any error (fail-open)."""
    url = MXC_BASE.rstrip("/") + "/"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def risk_conditions(ops, repo):
    """Fresh risk conditions, or ``[]`` for every fail-open case (gate disabled,
    MXC unreachable, or a benign/normal risk state)."""
    if not gate_enabled(ops, repo):
        return []
    data = fetch_mxc()
    if not isinstance(data, dict):
        return []  # unreachable / unparseable → fail-open
    risk_state = str(data.get("risk_state") or "").lower()
    if risk_state not in g.RISK_ALERT_STATES:
        return []
    symbol = data.get("symbol")
    fp = f"risk:{risk_state}" + (f":{symbol}" if symbol else "")
    return [{
        "fingerprint": fp,
        "severity": g.RISK_SEVERITY.get(risk_state, "warning"),
        "category": "risk",
        "title": f"risk {risk_state}",
        "detail": {"risk_state": risk_state, "symbol": symbol,
                   "pp_acc": data.get("pp_acc"), "pp_vel": data.get("pp_vel"),
                   "regime": data.get("regime")},
    }]


def main():
    ops = g.import_hermx_ops()
    now = time.time()
    conds = risk_conditions(ops, g.repo_root())
    g.run_gate("risk", conds, now)


if __name__ == "__main__":
    main()
