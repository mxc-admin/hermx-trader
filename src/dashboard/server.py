"""Dashboard HTTP server: request Handler (read routes + strategy-mode /
trading-state control routes) and the background model-cache refresh loop
(REFACTOR_PLAN.md Phase 7 sub-step 4; monolith ``class Handler`` +
``_refresh_dashboard_cache_loop``, moved not rewritten).

PACKAGE LAYOUT NOTE: this directory deliberately has NO __init__.py. A regular
package named ``dashboard`` would shadow ``src/dashboard.py`` for every
``import dashboard`` (packages win over same-named modules on sys.path), which
would break the whole test suite and the shim design. Instead, dashboard.py
extends its own ``__path__`` to this directory, which makes
``dashboard.server`` importable as a submodule while ``import dashboard``
keeps resolving to the monolith. Do not add an __init__.py here.

Auth/static module state (``DASH_AUTH_ENABLED``, ``DASH_AUTH_TOKEN``,
``STATIC_DIR``, ``DEV_CORS_*``, ``STATIC_MIME_TYPES``) stays defined in
dashboard.py -- tests monkeypatch it there (``monkeypatch.setattr(
dashboard_mod, "DASH_AUTH_ENABLED", ...)``) and fixtures reload dashboard.py
against per-test env -- so every Handler method reads it lazily via
``import dashboard as _dash`` at request time. The same goes for the routed
callables (``render``, ``api_payload``, ``health_payload``,
``signals_payload``, ``active_strategies``, the ``_set_*`` control-state
writers) and the model cache internals (``_MODEL_BUILD_LOCK``,
``_build_dashboard_model``): patches applied on the dashboard module must be
observed here, so nothing reload-sensitive is imported at module top.
"""
from __future__ import annotations

import base64
import hmac
import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse


class Handler(BaseHTTPRequestHandler):
    def _dashboard_auth_ok(self) -> bool:
        import dashboard as _dash
        if not _dash.DASH_AUTH_ENABLED:
            return True
        if not _dash.DASH_AUTH_TOKEN:
            return False
        provided = (self.headers.get("X-Dashboard-Token") or "").strip()
        if provided and hmac.compare_digest(provided, _dash.DASH_AUTH_TOKEN):
            return True
        auth_header = (self.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            return bool(token) and hmac.compare_digest(token, _dash.DASH_AUTH_TOKEN)
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                _user, pwd = decoded.split(":", 1)
            except Exception:
                return False
            return bool(pwd) and hmac.compare_digest(pwd, _dash.DASH_AUTH_TOKEN)
        return False

    def _maybe_cors(self):
        import dashboard as _dash
        # Dev-only: allow the Next dev server (localhost:3001) to read /api,/health.
        if not _dash.DEV_CORS_ENABLED:
            return
        self.send_header("Access-Control-Allow-Origin", _dash.DEV_CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _auth_challenge(self):
        body = {"ok": False, "error": "unauthorized"}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self._maybe_cors()
        # Offer both Basic (browser native prompt) and Bearer (tools/APIs).
        self.send_header("WWW-Authenticate", 'Basic realm="hermx-dashboard"')
        self.send_header("WWW-Authenticate", 'Bearer realm="hermx-dashboard"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self._maybe_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight (dev only). 204 No Content with the allow-* headers.
        self.send_response(204)
        self._maybe_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, path):
        import dashboard as _dash
        # Serve the built Next.js export from dashboard-ui/out, never escaping it.
        # Strip the /dashboard basePath prefix (Next builds with basePath=/dashboard).
        if path.startswith("/dashboard"):
            path = path[len("/dashboard"):] or "/"
        out_root = _dash.STATIC_DIR.resolve()
        rel = path.lstrip("/") or "index.html"
        candidate = (out_root / rel).resolve()
        # trailingSlash export emits dir/index.html (e.g. /health/ -> health/index.html).
        if candidate.is_dir():
            candidate = (candidate / "index.html").resolve()
        if candidate != out_root and out_root not in candidate.parents:
            self.send_bytes(404, b"not found", "text/plain")
            return
        if not candidate.is_file():
            self.send_bytes(404, b"not found", "text/plain")
            return
        ctype = _dash.STATIC_MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        body = candidate.read_bytes()
        # Inject the auth token into index.html so the SPA can read it from a meta
        # tag instead of baking the secret into the JS bundle at build time.
        if candidate.name == "index.html" and _dash.DASH_AUTH_TOKEN:
            meta = f'<meta name="hermx-token" content="{_dash.DASH_AUTH_TOKEN}">'
            body = body.replace(b"</head>", meta.encode("utf-8") + b"</head>", 1)
        self.send_bytes(200, body, ctype)

    def do_GET(self):
        import dashboard as _dash
        path = urlparse(self.path).path
        # Prefix check, not an exact allowlist: _serve_static maps directory requests
        # to index.html, so /dashboard/index.html (which injects the auth token) must
        # be challenged too. An exact set left it unauthenticated and leaked the token.
        if (path == "/" or path == "/api" or path.startswith("/dashboard")
                or path.startswith("/shadow/dashboard")) and not self._dashboard_auth_ok():
            self._auth_challenge()
            return
        if path in {"/api/signals", "/shadow/api/signals", "/dashboard/api/signals"}:
            if not self._dashboard_auth_ok():
                self._auth_challenge()
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                n = int((qs.get("n") or [_dash.SIGNALS_DEFAULT_N])[0])
            except (TypeError, ValueError):
                n = _dash.SIGNALS_DEFAULT_N
            symbol = (qs.get("symbol") or [None])[0]
            try:
                payload = _dash.signals_payload(n, symbol)
            except Exception as exc:  # unexpected read/projection failure only
                self.send_bytes(500, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            self.send_bytes(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path in {"/api/positions", "/shadow/api/positions", "/dashboard/api/positions"}:
            if not self._dashboard_auth_ok():
                self._auth_challenge()
                return
            qs = parse_qs(urlparse(self.path).query)

            def _q(name):
                value = (qs.get(name) or [None])[0]
                return str(value).strip() or None if value is not None else None

            try:
                from pnl_positions import list_positions

                rows = list_positions(
                    strategy_id=_q("strategy_id"),
                    status=_q("status"),
                    mode=_q("mode"),
                    venue=_q("venue"),
                )
            except Exception as exc:  # unexpected ledger/fold failure only
                self.send_bytes(500, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            payload = {"ok": True, "positions": rows, "count": len(rows)}
            self.send_bytes(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        static_ready = _dash.STATIC_DIR.is_dir()
        if path in {"/dashboard/api", "/dashboard/api/", "/api", "/shadow/dashboard/api"}:
            # Presence stamp (auth already passed above): the refresh loop keys its
            # active/idle rebuild cadence off this. Serving stays cache-only.
            _dash._LAST_API_HIT = time.time()
            payload = _dash.api_payload()
            self.send_bytes(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path in {"/health", "/shadow/health"}:
            self.send_bytes(200, json.dumps(_dash.health_payload(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif static_ready and (path.startswith("/dashboard") or path == "/"):
            # React SPA (built with basePath=/dashboard) handles /dashboard/* and /.
            self._serve_static(path)
        elif path in {"/dashboard", "/dashboard/", "/shadow/dashboard"}:
            # Fallback: legacy Python HTML when React build not present.
            self.send_bytes(200, _dash.render().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/":
            self.send_bytes(200, _dash.render().encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_bytes(404, b"not found", "text/plain")

    # ---- Strategy-mode control (write) -------------------------------------
    # POST   /api/control/strategy/{id}  body {"mode": "demo"|"live"|"clear"}
    # DELETE /api/control/strategy/{id}  -> clear override (restore file default)
    _CONTROL_PREFIXES = (
        "/dashboard/api/control/strategy/",
        "/shadow/dashboard/api/control/strategy/",
        "/api/control/strategy/",
    )

    def _strategy_control_id(self, path):
        """Return the {id} from a strategy-control path, or None if not such a route."""
        for prefix in self._CONTROL_PREFIXES:
            if path.startswith(prefix):
                return unquote(path[len(prefix):]).strip("/").strip()
        return None

    # ---- Global trading-state control (write) ------------------------------
    # POST   /api/control/trading-state  body {"state": "active"|"reducing"}
    # DELETE /api/control/trading-state  -> reset to "active"
    _TRADING_STATE_PATHS = frozenset({
        "/dashboard/api/control/trading-state",
        "/shadow/dashboard/api/control/trading-state",
        "/api/control/trading-state",
    })

    def _is_trading_state_path(self, path):
        return path.rstrip("/") in {p.rstrip("/") for p in self._TRADING_STATE_PATHS}

    def _apply_trading_state(self, state):
        import dashboard as _dash
        st = str(state or "").strip().lower()
        if st not in _dash._VALID_TRADING_STATES:
            self._send_control_error(400, "state must be one of: active, reducing")
            return
        if not _dash._set_trading_state(st):
            self._send_control_error(400, "failed to set trading_state")
            return
        body = json.dumps({"ok": True, "trading_state": st}, ensure_ascii=False).encode("utf-8")
        self.send_bytes(200, body, "application/json; charset=utf-8")

    def _send_control_error(self, status, message):
        body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def _apply_strategy_control(self, sid, mode, accounting_field=None,
                                risk_state=None, execution_mode=None):
        """Apply strategy-control changes: legacy 3-mode override, split-model
        ``risk_state``/``execution_mode`` fields, and/or an accounting-window change.

        ``accounting_field`` is a 3-state marker for the Phase-3 accounting window:
        - None  -> body carried no ``accounting_start_at`` key; leave the window alone.
        - the sentinel ("__clear__", None) or an int -> set/clear the window.
        At least one field must be actionable."""
        import dashboard as _dash
        sid = (sid or "").strip()
        if not sid:
            self._send_control_error(400, "missing strategy_id")
            return
        known = {s.get("strategy_id") for s in _dash.active_strategies()}
        if sid not in known:
            self._send_control_error(404, f"unknown strategy_id: {sid}")
            return
        # Validate BEFORE any write so a partially-valid body mutates nothing.
        if risk_state and risk_state not in _dash._VALID_RISK_STATES:
            self._send_control_error(400, "risk_state must be one of: active, reduce")
            return
        if execution_mode and execution_mode not in _dash._VALID_EXECUTION_ACCOUNTS:
            self._send_control_error(400, "execution_mode must be one of: demo, live")
            return
        resp = {"ok": True, "strategy_id": sid}
        # Mode override (optional when only the accounting window is being set).
        if mode:
            mode = _dash._LEGACY_STRATEGY_MODE_ALIASES.get(mode, mode)  # accept legacy shadow->pause, paper->demo
            if mode not in {"pause", "demo", "live", "clear"}:
                self._send_control_error(400, "mode must be one of: pause, demo, live, clear")
                return
            if mode == "clear":
                _dash._clear_strategy_override(sid)  # idempotent: no-op if no override existed
                resp["mode"] = "clear"
            else:
                # Server-side live lock: the client pill is advisory only. Never
                # accept a live override while the global kill switch is engaged.
                if mode == "live" and not _dash.live_trading_enabled()[0]:
                    self._send_control_error(403, "live mode locked: HERMX_LIVE_TRADING is not enabled")
                    return
                if not _dash._set_strategy_override(sid, mode):
                    self._send_control_error(400, "failed to set override")
                    return
                resp["mode"] = mode
        # Split-model fields (account and risk are independent axes).
        if execution_mode:
            # Same server-side live lock as mode=live above.
            if execution_mode == "live" and not _dash.live_trading_enabled()[0]:
                self._send_control_error(403, "live mode locked: HERMX_LIVE_TRADING is not enabled")
                return
            if not _dash._set_strategy_account(sid, execution_mode):
                self._send_control_error(400, "failed to set execution_mode")
                return
            resp["execution_mode"] = execution_mode
        if risk_state:
            if not _dash._set_strategy_risk(sid, risk_state):
                self._send_control_error(400, "failed to set risk_state")
                return
            resp["risk_state"] = risk_state
        # Accounting window (Phase 3). accounting_field is (kind, value): "set"/"clear".
        if accounting_field is not None:
            kind, value = accounting_field
            if kind == "invalid":
                self._send_control_error(400, "accounting_start_at must be an integer ms epoch or null")
                return
            start_ms = None if kind == "clear" else value
            if not _dash._set_accounting_start(sid, start_ms) and kind != "clear":
                self._send_control_error(400, "failed to set accounting_start_at")
                return
            resp["accounting_start_at"] = start_ms
        if not (set(resp) & {"mode", "execution_mode", "risk_state", "accounting_start_at"}):
            self._send_control_error(400, "no actionable field (mode, execution_mode, risk_state or accounting_start_at)")
            return
        self.send_bytes(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if self._is_trading_state_path(path):
            if not self._dashboard_auth_ok():
                self._auth_challenge()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8")) or {}
                except Exception:
                    self._send_control_error(400, "invalid JSON body")
                    return
            self._apply_trading_state(body.get("state"))
            return
        sid = self._strategy_control_id(path)
        if sid is None:
            self.send_bytes(404, b"not found", "text/plain")
            return
        if not self._dashboard_auth_ok():
            self._auth_challenge()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        mode = ""
        risk_state = ""
        execution_mode = ""
        accounting_field = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_control_error(400, "invalid JSON body")
                return
            body = body or {}
            mode = str(body.get("mode") or "").strip().lower()
            risk_state = str(body.get("risk_state") or "").strip().lower()
            execution_mode = str(body.get("execution_mode") or "").strip().lower()
            # Phase 3: accounting_start_at is optional. Present-and-null clears the
            # window; an int (ms epoch) sets it; a non-int/non-null is rejected.
            if "accounting_start_at" in body:
                raw_val = body.get("accounting_start_at")
                if raw_val is None:
                    accounting_field = ("clear", None)
                elif isinstance(raw_val, bool):  # bool is an int subclass; reject it
                    accounting_field = ("invalid", None)
                elif isinstance(raw_val, int):
                    accounting_field = ("set", raw_val)
                else:
                    accounting_field = ("invalid", None)
        self._apply_strategy_control(sid, mode, accounting_field,
                                     risk_state=risk_state, execution_mode=execution_mode)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if self._is_trading_state_path(path):
            if not self._dashboard_auth_ok():
                self._auth_challenge()
                return
            self._apply_trading_state("active")  # DELETE resets to normal trading
            return
        sid = self._strategy_control_id(path)
        if sid is None:
            self.send_bytes(404, b"not found", "text/plain")
            return
        if not self._dashboard_auth_ok():
            self._auth_challenge()
            return
        self._apply_strategy_control(sid, "clear")

    def log_message(self, _fmt, *_args):
        return


DASHBOARD_REFRESH_INTERVAL_SECONDS = 15


def _should_rebuild(now: float, last_hit: float, last_build: float,
                    active_window: float = None, idle_rebuild: float = None) -> bool:
    """True if the background loop should rebuild the model now.

    Active (an authenticated /api GET stamped _LAST_API_HIT within
    active_window): rebuild every tick. Idle: rebuild only when the last build
    is older than idle_rebuild — or has never happened (last_build <= 0)."""
    import dashboard as _dash
    if active_window is None:
        active_window = _dash.ACTIVE_WINDOW_SECONDS
    if idle_rebuild is None:
        idle_rebuild = _dash.IDLE_REBUILD_SECONDS
    if last_hit > 0 and (now - last_hit) < active_window:
        return True
    if last_build <= 0:
        return True
    return (now - last_build) >= idle_rebuild


def _refresh_dashboard_cache_loop(interval=DASHBOARD_REFRESH_INTERVAL_SECONDS):
    """Periodic background rebuild of _MODEL_CACHE so /api always has a recent
    snapshot without ever blocking on the ~15s build in steady state.

    Each tick consults _should_rebuild: while a viewer is present (fresh
    _LAST_API_HIT presence stamp) every tick rebuilds; otherwise builds back
    off to one per IDLE_REBUILD_SECONDS. Rebuilds run under _MODEL_BUILD_LOCK
    (single-flighted with a cold synchronous /api build) and swallow failures:
    if a build throws, the last good model stays served and the exception is
    logged to stderr. Runs in a daemon thread from __main__ only — never at
    import time — so unit tests importing this module don't trigger network
    calls.
    """
    import dashboard as _dash
    last_build = 0.0
    while True:
        try:
            if _should_rebuild(time.time(), _dash._LAST_API_HIT, last_build):
                with _dash._MODEL_BUILD_LOCK:
                    _dash._build_dashboard_model()
                last_build = time.time()
        except Exception as exc:  # noqa: BLE001 — refresh is best-effort; keep last good model
            print(f"[dashboard] cache refresh failed (non-fatal): {exc}", file=sys.stderr)
        time.sleep(interval)
