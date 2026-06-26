#!/usr/bin/env python3
"""OKX demo/sandbox executor adapter.

Wraps the existing, battle-tested ``src/okx_demo_executor.py`` CLI (which speaks
the OKX v5 REST API) behind the venue-neutral :class:`BaseExecutor` interface.
We deliberately shell out to that script rather than re-implement the OKX signing
and order logic here — composition over rewrite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from .base import BaseExecutor, empty_fill_summary


class OkxDemoExecutor(BaseExecutor):
    key = "okx_demo"

    # The OKX REST client lives in this standalone script.
    SCRIPT_RELPATH = ("src", "okx_demo_executor.py")
    EXECUTE_TIMEOUT_SECONDS = 45
    HEALTH_TIMEOUT_SECONDS = 12

    def _script_path(self):
        return self.root.joinpath(*self.SCRIPT_RELPATH)

    def _child_env(self) -> dict:
        """Translate generic execution config into the OKX_* env the script reads."""
        env = os.environ.copy()
        env["OKX_SIMULATED_TRADING"] = "1" if bool(self.execution_cfg.get("simulated_trading", True)) else "0"
        env["OKX_FORCE_IPV4"] = "1" if bool(self.execution_cfg.get("force_ipv4", True)) else "0"
        env["OKX_SUBMIT_ORDERS"] = "true"
        return env

    def execute(self, readiness: dict) -> dict:
        script = self._script_path()
        client_order_id = (readiness.get("execution_intent") or {}).get("client_order_id")
        if not script.exists():
            return self.normalized_result(
                ok=False,
                mode="executor_missing",
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": f"missing {'/'.join(self.SCRIPT_RELPATH)}"},
            )
        started = time.time()
        try:
            completed = subprocess.run(
                [sys.executable, str(script), "execute"],
                input=json.dumps(readiness, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.EXECUTE_TIMEOUT_SECONDS,
                env=self._child_env(),
            )
            elapsed_ms = round((time.time() - started) * 1000)
            if completed.returncode != 0:
                return self.normalized_result(
                    ok=False,
                    mode="submit_failed",
                    elapsed_ms=elapsed_ms,
                    fill_summary=empty_fill_summary(client_order_id),
                    payload={
                        "returncode": completed.returncode,
                        "stderr": completed.stderr[-2000:],
                        "stdout": completed.stdout[-2000:],
                    },
                )
            payload = json.loads(completed.stdout)
            # The OKX script emits ``okx_fill_summary``; expose it as the generic
            # ``fill_summary`` while leaving the raw payload intact for the UI.
            fill = payload.get("okx_fill_summary") or empty_fill_summary(client_order_id)
            return self.normalized_result(
                ok=True,
                mode=payload.get("mode") or "submit_enabled",
                elapsed_ms=elapsed_ms,
                fill_summary=fill,
                payload=payload,
            )
        except Exception as exc:
            return self.normalized_result(
                ok=False,
                mode="submit_exception",
                elapsed_ms=round((time.time() - started) * 1000),
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": str(exc)},
            )

    def health(self) -> dict:
        script = self._script_path()
        if not script.exists():
            return {"ok": False, "exchange": self.key, "error": "executor_missing"}
        env = os.environ.copy()
        env["OKX_SUBMIT_ORDERS"] = "false"
        try:
            completed = subprocess.run(
                [sys.executable, str(script), "health"],
                cwd=str(self.root),
                text=True,
                capture_output=True,
                timeout=self.HEALTH_TIMEOUT_SECONDS,
                env=env,
            )
            if completed.returncode != 0:
                return {"ok": False, "exchange": self.key, "error": (completed.stderr or completed.stdout)[-500:]}
            return json.loads(completed.stdout)
        except Exception as exc:
            return {"ok": False, "exchange": self.key, "error": str(exc)}
