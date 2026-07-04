"""Operator alert transport (Phase 0 extraction).

Houses the three operator-alert emitters that used to live in
webhook_receiver.py: ``emit_operator_alert`` (durable ledger + log + optional
webhook POST), ``emit_auth_failure_alert`` and ``maybe_emit_queue_saturation_alert``.

These functions read a handful of root-bound / monkeypatchable constants that
still live in webhook_receiver.py (``ALERTS_LEDGER``,
``HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS``, ``ALERT_AUTH_FAILURE``,
``ALERT_QUEUE_SATURATION``, ``QUEUE_SATURATION_ALERT_DEPTH``). Rather than move
those constants (tests monkeypatch them on ``wr``), each function reads them
lazily through ``import webhook_receiver as _wr`` -- mirroring the
executors/ccxt_adapter.py lazy ``_wr`` pattern and avoiding a
receiver<->alerts import cycle at load time.

The leaf-pure ``now_iso`` / ``append_jsonl`` primitives are imported directly
from their extracted homes. webhook_receiver re-exports these three names for
backward compatibility.
"""
from __future__ import annotations

import json
import logging
import os
from urllib import request as urllib_request

from webhook.timeutil import now_iso
from webhook.ledger_io import append_jsonl


def emit_operator_alert(kind: str, detail: "dict | None" = None, *, severity: str = "warning") -> dict:
    """Concrete operator alert transport (Task 6): durable ledger + log + optional
    webhook POST configured by HERMX_ALERT_WEBHOOK_URL."""
    import webhook_receiver as _wr
    record = {
        "ts": now_iso(),
        "kind": "operator",
        "alert": kind,
        "severity": severity,
        "detail": detail or {},
    }
    try:
        append_jsonl(_wr.ALERTS_LEDGER, record)
    except OSError as exc:
        logging.error("failed to write operator alert %s: %s", kind, exc)

    webhook_url = (os.environ.get("HERMX_ALERT_WEBHOOK_URL") or "").strip()
    if webhook_url:
        timeout_seconds = _wr.HERMX_ALERT_WEBHOOK_TIMEOUT_SECONDS
        body = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds):
                pass
        except Exception as exc:
            logging.error("operator alert webhook failed kind=%s url=%s error=%s", kind, webhook_url, exc)

    log_fn = logging.error if severity.lower() in {"error", "critical"} else logging.warning
    log_fn("%s %s", kind, json.dumps(detail or {}, ensure_ascii=False))
    return record


def emit_auth_failure_alert(path: str, client_ip: "str | None") -> dict:
    import webhook_receiver as _wr
    return emit_operator_alert(
        _wr.ALERT_AUTH_FAILURE,
        {"path": path, "client_ip": client_ip},
        severity="error",
    )


def maybe_emit_queue_saturation_alert(queue_depth: int) -> bool:
    import webhook_receiver as _wr
    if _wr.QUEUE_SATURATION_ALERT_DEPTH <= 0 or queue_depth < _wr.QUEUE_SATURATION_ALERT_DEPTH:
        return False
    emit_operator_alert(
        _wr.ALERT_QUEUE_SATURATION,
        {"queue_depth": queue_depth, "threshold": _wr.QUEUE_SATURATION_ALERT_DEPTH},
        severity="error",
    )
    return True
