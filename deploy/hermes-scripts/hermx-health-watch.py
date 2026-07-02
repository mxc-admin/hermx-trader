#!/usr/bin/env python3
"""HermX health watchdog (Hermes cron, every 5m, ``--no-agent``).

Pure liveness check — zero LLM. Verifies the dashboard and receiver are reachable
over loopback and inspects the dashboard arm block (kill switch). Healthy → empty
stdout (silent tick). A problem → one line per issue on stdout, which Hermes delivers
verbatim. Any internal error exits non-zero so a broken watchdog auto-delivers rather
than failing silently.

READ-ONLY: never mutates HermX state; writes nothing.

Note: a *disarmed* dashboard is NOT reported by default (this host runs shadow-only /
paused by design, so it would page every 5m). Set ``HERMX_HEALTH_REQUIRE_ARMED=true``
to treat disarmed as a problem.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermx_gate_lib as g  # noqa: E402


def _truthy(name) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes"}


def check(ops):
    """Return a list of problem lines (empty when everything is healthy)."""
    problems = []
    secret = os.environ.get("HERMX_SECRET")

    health, _herr = ops._get_json(ops.DASHBOARD_BASE, "/health", secret=secret)
    dashboard_ok = isinstance(health, dict) and bool(health.get("ok"))
    if not dashboard_ok:
        problems.append("dashboard: unreachable")

    rhealth, _rerr = ops._get_json(ops.RECEIVER_BASE, "/health")
    if not (isinstance(rhealth, dict) and rhealth.get("ok")):
        problems.append("receiver: down")

    # Arm state (kill switch / optional disarmed) — only when the dashboard answered.
    if dashboard_ok:
        arm = health.get("arm") if isinstance(health.get("arm"), dict) else {}
        if arm.get("kill_switch_engaged"):
            problems.append("arm: kill-switch engaged")
        if _truthy("HERMX_HEALTH_REQUIRE_ARMED") and not arm.get("armed"):
            problems.append("arm: disarmed")
    return problems


def main():
    ops = g.import_hermx_ops()
    for line in check(ops):
        print(line)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — broken watchdog must surface, not hide
        print(f"health-watch internal error: {exc}", file=sys.stderr)
        sys.exit(1)
