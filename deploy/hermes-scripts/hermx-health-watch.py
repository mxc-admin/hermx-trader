#!/usr/bin/env python3
"""HermX health watchdog (Hermes cron, every 5m, ``--no-agent``).

Pure liveness check — zero LLM. Verifies the dashboard and receiver are reachable
over loopback and inspects the dashboard arm block (kill switch). Healthy → empty
stdout (silent tick). A problem → one line per issue on stdout, which Hermes delivers
verbatim. Any internal error exits non-zero so a broken watchdog auto-delivers rather
than failing silently.

Suppression: a steady-state problem (e.g. a kill switch that stays engaged on a
shadow-only host) would otherwise print its line on every 5m tick and flood Telegram.
Each problem is turned into a condition dict and run through the shared
``hermx_gate_lib`` suppression window (``WINDOW["health"]`` = 900s, escalation-bypass).
A line is printed only when its condition is *fresh* — unseen, its window has elapsed,
or its severity escalated. The "health" sidecar (``.hermx-health.state``) is advanced
only on the ticks that print (wake == write). This is a ``--no-agent`` script, so it
prints plain text; it never emits the ``wakeAgent`` JSON contract.

READ-ONLY against HermX state: the only file written is the monitoring sidecar.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402


def _truthy(name) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes"}


def _cond(fingerprint, severity, title, detail=None):
    return {
        "fingerprint": fingerprint,
        "severity": severity,
        "category": "health",
        "title": title,      # the plain-text line Hermes delivers verbatim
        "detail": detail or {},
    }


def check(ops):
    """Return the active health problems as condition dicts (empty when healthy).

    Fingerprints are stable (no timestamp/counter) so the suppression window can key
    on them across ticks. ``title`` carries the plain-text line printed for Hermes."""
    conds = []
    secret = os.environ.get("HERMX_SECRET")

    health, _herr = ops._get_json(ops.DASHBOARD_BASE, "/health", secret=secret)
    dashboard_ok = isinstance(health, dict) and bool(health.get("ok"))
    if not dashboard_ok:
        conds.append(_cond("health:dashboard_unreachable", "critical", "dashboard: unreachable"))

    rhealth, _rerr = ops._get_json(ops.RECEIVER_BASE, "/health")
    if not (isinstance(rhealth, dict) and rhealth.get("ok")):
        conds.append(_cond("health:receiver_down", "critical", "receiver: down"))

    # Arm state (kill switch / optional disarmed) — only when the dashboard answered.
    if dashboard_ok:
        arm = health.get("arm") if isinstance(health.get("arm"), dict) else {}
        # A kill switch is only a problem on hosts that expect live trading. On
        # demo/shadow hosts ``kill_switch_engaged = not live_enabled`` is the normal,
        # fail-closed state, so only alert when HERMX_LIVE_TRADING is truthy.
        if arm.get("kill_switch_engaged") and _truthy("HERMX_LIVE_TRADING"):
            conds.append(_cond("health:kill_switch_engaged", "warning", "arm: kill-switch engaged"))
        if _truthy("HERMX_HEALTH_REQUIRE_ARMED") and not arm.get("armed"):
            conds.append(_cond("health:disarmed", "warning", "arm: disarmed"))
    return conds


def run(ops, now_epoch):
    """Evaluate active problems through the health suppression window, print the
    plain-text line for each *fresh* condition, and persist the sidecar on wake.

    Returns the list of fresh conditions (for tests). The clock is injected — never
    read inline — per the repo convention."""
    conds = check(ops)
    sp = g.state_path("health")
    state = g.load_state(sp)
    fresh, new_state = g.evaluate(conds, state, g.WINDOW["health"], now_epoch)
    for c in fresh:
        print(c["title"])
    if fresh:
        g.save_state(sp, new_state)  # wake == write (at-least-once)
    return fresh


def main():
    ops = g.import_hermx_ops()
    run(ops, time.time())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — broken watchdog must surface, not hide
        print(f"health-watch internal error: {exc}", file=sys.stderr)
        sys.exit(1)
