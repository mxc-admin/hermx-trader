#!/usr/bin/env python3
"""CCXT-backed executor adapter.

CCXT is the exchange transport layer; HermX keeps risk/idempotency/journaling
above this adapter in the controlled execution API path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import logging
import os
import time

logger = logging.getLogger(__name__)

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    ccxt = None

from .base import BaseExecutor, empty_fill_summary, empty_normalized_order
from security.credentials import redact_secrets, resolve_exchange_credentials
from hermx_shared import live_trading_enabled

# Submit timeout (seconds) for the ccxt client, mirroring the service-level submit
# timeout. Hard-coded per the flag fluff audit (no deployment tunes it); kept as a
# module constant so a test can monkeypatch it. The receiver holds its own copy of
# the same value -- a shared import here would create a cycle.
HERMX_SUBMIT_TIMEOUT_SECONDS = 45.0


def _to_float(value, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalized_realized_pnl(order: dict, info: dict, exchange_id: str) -> float | None:
    """Extract a venue-normalized realized P&L for an order-history row.

    The field name differs per venue; None when the venue does not expose realized
    P&L in order history (Phase 1 accepts None — Phase 2 backfills via positions
    history for those venues).
    """
    venue = str(exchange_id or "").lower()
    if venue == "okx":
        return _to_float(info.get("pnl"), None)
    if venue == "hyperliquid":
        return _to_float(info.get("closedPnl"), None)
    if venue == "binance":
        # Realized P&L lives on trades, not orders; may be absent on the order row.
        return _to_float(info.get("realizedPnl"), None)
    # bybit + others: not available in order history.
    logger.debug(
        "realized_pnl unavailable for venue %s ordId %s — recording None",
        venue, order.get("id"),
    )
    return None


def _normalize_reduce_only(order: dict, info: dict) -> bool | None:
    """Prefer the unified CCXT top-level ``reduceOnly``; fall back to the venue info
    blob (OKX returns the string "true"/"false"). Returns None when unknown."""
    raw = order.get("reduceOnly")
    if raw is None:
        raw = info.get("reduceOnly")
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    if raw is None:
        return None
    return bool(raw)


def _decimal_floor(value: float | Decimal, step: float | Decimal) -> float:
    d_value = Decimal(str(value or 0.0))
    d_step = Decimal(str(step or 0.0))
    if d_step <= 0:
        return float(d_value)
    units = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(units * d_step)


def _to_hyperliquid_cloid(client_order_id: str) -> str:
    """Map an arbitrary client order id to Hyperliquid's required cloid format:
    a 128-bit (16-byte / 32 hex char) hex string prefixed with ``0x``. A raw UUID is
    the wrong shape, so hash and truncate to guarantee exactly 16 bytes regardless of
    input length. (Hyperliquid's cloid is 128-bit; a 32-byte value is rejected.)"""
    stripped = str(client_order_id).replace("-", "")
    return "0x" + hashlib.sha256(stripped.encode()).hexdigest()[:32]


def _step_from_precision(precision_value) -> float | None:
    if precision_value in (None, ""):
        return None
    try:
        if isinstance(precision_value, int):
            if precision_value < 0:
                return None
            return float(Decimal("1") / (Decimal("10") ** precision_value))
        prec = float(precision_value)
        if prec < 0:
            return None
        if prec >= 1 and float(int(prec)) == prec:
            return float(Decimal("1") / (Decimal("10") ** int(prec)))
        return float(prec)
    except Exception:
        return None


def _submit_timeout_ms() -> int:
    """ccxt client `timeout` (ms) derived from HERMX_SUBMIT_TIMEOUT_SECONDS so a
    hung submit fails fast and maps to UNKNOWN (invariant 5) rather than blocking
    forever. Mirrors the service-level submit timeout."""
    return int(max(1.0, HERMX_SUBMIT_TIMEOUT_SECONDS) * 1000)


def _is_timeout_error(exc: Exception) -> bool:
    """A ccxt network/timeout error means the order may have reached the venue but
    its outcome is unknown -> caller must surface mode 'submit_timeout' (UNKNOWN),
    never 'submit_failed' (REJECTED)."""
    if ccxt is not None:
        timeout_types = tuple(
            t
            for t in (getattr(ccxt, "RequestTimeout", None), getattr(ccxt, "NetworkError", None))
            if isinstance(t, type)
        )
        if timeout_types and isinstance(exc, timeout_types):
            return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text


def _inst_id_to_ccxt_symbol(inst_id: str | None) -> str | None:
    text = str(inst_id or "").strip().upper()
    if not text:
        return None
    # BTC-USDT-SWAP -> BTC/USDT:USDT
    if text.endswith("-SWAP"):
        parts = text.split("-")
        if len(parts) >= 2:
            base, quote = parts[0], parts[1]
            settle = base if quote == "USD" else quote
            return f"{base}/{quote}:{settle}"
    if "-" in text and "/" not in text:
        parts = text.split("-")
        if len(parts) == 2:
            return f"{parts[0]}/{parts[1]}"
    # BTCUSDT -> BTC/USDT
    if text.endswith("USDT") and "-" not in text and "/" not in text:
        base = text[:-4]
        return f"{base}/USDT"
    # SOLUSDC -> SOL/USDC (Hyperliquid quotes in USDC)
    if text.endswith("USDC") and "-" not in text and "/" not in text:
        base = text[:-4]
        return f"{base}/USDC"
    return text


# Public name for the unified-symbol join (dashboard snapshots + drift detection key
# both dialects — venue "SOL-USDC-SWAP" and strategy "SOL/USDC:USDC" — through this).
inst_id_to_ccxt_symbol = _inst_id_to_ccxt_symbol


def _ccxt_symbol_to_inst_id(symbol: str | None) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if ":" in text:
        pair, _settle = text.split(":", 1)
        if "/" in pair:
            base, quote = pair.split("/", 1)
            return f"{base}-{quote}-SWAP"
    if "/" in text:
        base, quote = text.split("/", 1)
        return f"{base}-{quote}"
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _order_fully_filled(order: dict, requested_amount: float | None) -> bool:
    """True only when the create_order response proves the FULL submitted size filled:
    the response's ``filled`` reaches the size WE submitted for this leg. Comparing
    against the requested size (not the response's own ``remaining``/``amount``) means a
    partial IOC fill -- which can self-report ``remaining == 0`` on an executed-derived
    amount -- is never terminalized as complete. Conservative: any ambiguity (non-dict
    order, no fill, or no requested size) returns False, leaving the order an ACK to be
    reconciled rather than a falsely-claimed fill."""
    if not isinstance(order, dict):
        return False
    filled = _to_float(order.get("filled"), 0.0)
    req = _to_float(requested_amount, None)
    if filled <= 0 or req is None or req <= 0:
        return False
    # Tiny tolerance for float/rounding; a genuine partial fill is far below this.
    return filled >= req - 1e-9


# B2 -- venue equity/balance drift threshold (percent). Read at CALL time (not module
# load) so a test / operator can retune it without a reload. Default 5.0%.
HERMX_BALANCE_DRIFT_THRESHOLD_PCT_DEFAULT = 5.0

# B1 -- float epsilon for the venue-vs-journal position comparison. A drift smaller
# than this is treated as no drift (float add/subtract residue, not a real divergence).
POSITION_DRIFT_EPS = 1e-8


def detect_position_drift(executor, journal_positions: dict, venue: str, mode: str) -> list:
    """OBSERVE-ONLY (B1): compare HermX's journal view of open positions against what
    the venue actually reports. NEVER auto-corrects, never submits.

    Args:
        executor: an adapter exposing ``get_positions()`` (the read-only query verb).
        journal_positions: ``{inst_id: signed_qty}`` -- HermX's believed net position.
        venue / mode: tags echoed into each drift row (e.g. "okx", "demo").

    Returns a list of ``{inst_id, journal_qty, venue_qty, drift, venue, mode}`` for
    every instrument (in either set) where ``abs(venue_qty - journal_qty) >
    POSITION_DRIFT_EPS``. Journal keys (strategy-format, e.g. ``SOL/USDC:USDC``) and
    venue keys (venue-format, e.g. ``SOL-USDC-SWAP``) are joined through
    :func:`inst_id_to_ccxt_symbol` so the same position never double-reports as
    journal-open-venue-flat + venue-open-journal-unknown. A venue read that raises
    degrades to ``[]`` (logs a warning) so a drift check can NEVER crash startup or a
    render cycle.
    """
    try:
        venue_rows = executor.get_positions() or []
    except Exception as exc:  # never crash the caller on an observe-only read
        logger.warning(
            "detect_position_drift: venue read failed venue=%s mode=%s: %s",
            venue, mode, redact_secrets(str(exc)),
        )
        return []
    venue_view: dict = {}
    venue_names: dict = {}
    for row in venue_rows:
        inst = (row or {}).get("inst_id")
        if inst is None:
            continue
        key = _inst_id_to_ccxt_symbol(inst) or inst
        venue_view[key] = _to_float(row.get("pos"), 0.0) or 0.0
        venue_names[key] = inst
    journal_view: dict = {}
    journal_names: dict = {}
    for k, v in (journal_positions or {}).items():
        key = _inst_id_to_ccxt_symbol(k) or k
        journal_view[key] = _to_float(v, 0.0) or 0.0
        journal_names[key] = k
    drifts: list = []
    for key in set(venue_view) | set(journal_view):
        jq = journal_view.get(key, 0.0)
        vq = venue_view.get(key, 0.0)
        drift = vq - jq
        if abs(drift) > POSITION_DRIFT_EPS:
            drifts.append({
                "inst_id": journal_names.get(key) or venue_names.get(key) or key,
                "journal_qty": jq,
                "venue_qty": vq,
                "drift": drift,
                "venue": venue,
                "mode": mode,
            })
    return drifts


def check_balance_drift(executor, hermx_equity_usd: float, venue: str, mode: str,
                        currency: str = "USDT") -> dict | None:
    """OBSERVE-ONLY (B2): reconcile the venue's real total balance against HermX's
    computed equity. Live-mode only. NEVER auto-corrects, never blocks.

    Returns ``None`` for a demo/pause account (sandbox balance is fake) or when the
    venue balance read fails. Otherwise returns
    ``{venue_balance, hermx_equity, drift_usd, drift_pct, alerted}``. When
    ``drift_pct`` exceeds ``HERMX_BALANCE_DRIFT_THRESHOLD_PCT`` (env, default 5.0) it
    logs a WARNING and emits a RECONCILE_MISMATCH (best-effort).
    """
    if str(mode or "").lower() != "live":
        return None  # demo/pause sandbox balance is arbitrary -> meaningless to compare
    bal = executor.get_balance_summary(currency)
    if bal is None:
        return None  # venue call failed -> degrade silently, no false alert
    venue_balance = float(bal.get("total") or 0.0)
    equity = float(hermx_equity_usd or 0.0)
    drift = abs(venue_balance - equity)
    drift_pct = drift / max(equity, 1.0) * 100.0
    try:
        threshold = float(os.environ.get(
            "HERMX_BALANCE_DRIFT_THRESHOLD_PCT",
            HERMX_BALANCE_DRIFT_THRESHOLD_PCT_DEFAULT,
        ))
    except (TypeError, ValueError):
        threshold = HERMX_BALANCE_DRIFT_THRESHOLD_PCT_DEFAULT
    alerted = drift_pct > threshold
    if alerted:
        logger.warning(
            "balance_drift venue=%s mode=%s venue_balance=%.2f hermx_equity=%.2f "
            "drift_usd=%.2f drift_pct=%.2f threshold=%.2f",
            venue, mode, venue_balance, equity, drift, drift_pct, threshold,
        )
        try:  # lazy import avoids a receiver<->adapter import cycle at load time
            import webhook_receiver as _wr
            _wr.emit_reconcile_alert(_wr.RECONCILE_ALERT_MISMATCH, {
                "stage": "balance_drift",
                "type": "balance_drift",
                "venue": venue,
                "mode": mode,
                "venue_balance": venue_balance,
                "hermx_equity": equity,
                "drift_usd": drift,
                "drift_pct": drift_pct,
            })
        except Exception as e:  # an alert-transport failure must never break the check
            logger.debug("drift-check alert transport failed: %s", e, exc_info=False)
    return {
        "venue_balance": venue_balance,
        "hermx_equity": equity,
        "drift_usd": drift,
        "drift_pct": drift_pct,
        "alerted": alerted,
    }


class CcxtExecutor(BaseExecutor):
    key = "ccxt"

    def __init__(self, config: dict, root):
        super().__init__(config, root)
        self._cached_client = None

    def _exchange_id(self) -> str:
        # "ccxt" is the ADAPTER backend name, not a venue. When it (or nothing) is
        # supplied, fall back to a real venue so getattr(ccxt, "ccxt") -> None can't
        # silently disable reconciliation.
        raw = str(self.execution_cfg.get("ccxt_exchange")
                  or self.execution_cfg.get("exchange") or "").strip().lower()
        if raw in ("", "ccxt"):
            raw = str(os.environ.get("HERMX_CCXT_EXCHANGE") or "okx").strip().lower()
        return raw

    def _client(self, *, close_only: bool = False, read_only: bool = False):
        if self._cached_client is not None:
            return self._cached_client
        if ccxt is None:
            raise RuntimeError("ccxt_not_installed")

        exchange_id = self._exchange_id()
        exchange_cls = getattr(ccxt, exchange_id, None)
        if exchange_cls is None:
            raise ValueError(f"unsupported_ccxt_exchange:{exchange_id}")

        mode = "live" if not bool(self.execution_cfg.get("simulated_trading", True)) else "demo"
        creds = resolve_exchange_credentials(exchange_id, os.environ, mode=mode)
        # `timeout` (ms) bounds every ccxt HTTP call (including create_order) so a
        # hung submit raises RequestTimeout and is mapped to mode "submit_timeout"
        # -> UNKNOWN (invariant 5) instead of hanging.
        kwargs = {"enableRateLimit": True, "timeout": _submit_timeout_ms()}

        if exchange_id.startswith("okx"):
            kwargs.update(
                {
                    "apiKey": creds.get("OKX_API_KEY", ""),
                    "secret": creds.get("OKX_SECRET_KEY", ""),
                    "password": creds.get("OKX_PASSPHRASE", ""),
                    "options": {"defaultType": str(self.execution_cfg.get("ccxt_default_type") or "swap")},
                }
            )
        elif exchange_id.startswith("kucoin"):
            kwargs.update(
                {
                    "apiKey": creds.get("KUCOIN_API_KEY", ""),
                    "secret": creds.get("KUCOIN_SECRET", ""),
                    "password": creds.get("KUCOIN_PASSPHRASE", ""),
                }
            )
        elif exchange_id.startswith("bybit"):
            kwargs.update(
                {
                    "apiKey": creds.get("BYBIT_API_KEY", ""),
                    "secret": creds.get("BYBIT_SECRET_KEY", ""),
                    "options": {"defaultType": str(self.execution_cfg.get("ccxt_default_type") or "swap")},
                }
            )
        elif exchange_id.startswith("hyperliquid"):
            # Hyperliquid uses wallet-based auth (ccxt requiredCredentials:
            # walletAddress + privateKey), NOT the apiKey/secret/password shape.
            # Keys come ONLY from the hyperliquid credential resolver, which fails
            # closed on a partial set, so a missing key leaves these blank -> disarmed.
            kwargs.update(
                {
                    "walletAddress": creds.get("HYPERLIQUID_WALLET_ADDRESS", ""),
                    "privateKey": creds.get("HYPERLIQUID_PRIVATE_KEY", ""),
                }
            )
        elif exchange_id == "binance":
            default_type = self.execution_cfg.get("ccxt_default_type", "future")
            kwargs["apiKey"] = creds.get("BINANCE_API_KEY", "")
            kwargs["secret"] = creds.get("BINANCE_SECRET_KEY", "")
            kwargs.setdefault("options", {})["defaultType"] = default_type
        elif exchange_id == "bitget":
            kwargs["apiKey"] = creds.get("BITGET_API_KEY", "")
            kwargs["secret"] = creds.get("BITGET_SECRET_KEY", "")
            kwargs["password"] = creds.get("BITGET_PASSPHRASE", "")
        elif exchange_id in ("gate", "gateio"):
            kwargs["apiKey"] = creds.get("GATE_API_KEY", "")
            kwargs["secret"] = creds.get("GATE_SECRET_KEY", "")
        elif exchange_id in ("coinbase", "coinbaseadvanced"):
            # Credentials applied; a demo request still fails closed below via the
            # set_sandbox_mode() guard if ccxt's coinbase adapter has no sandbox URL.
            kwargs["apiKey"] = creds.get("COINBASE_API_KEY", "")
            kwargs["secret"] = creds.get("COINBASE_SECRET_KEY", "")

        client = exchange_cls(kwargs)

        if bool(self.execution_cfg.get("simulated_trading", True)):
            if not hasattr(client, "set_sandbox_mode"):
                raise RuntimeError(
                    f"execution_mode=demo requested but {exchange_id} has no sandbox support in CCXT"
                )
            try:
                client.set_sandbox_mode(True)
            except Exception as exc:
                raise RuntimeError(
                    f"execution_mode=demo: failed to enable sandbox for {exchange_id}: {exc}"
                ) from exc
        else:
            # Fail-closed defense in depth: even if the service-level live gate is
            # somehow bypassed, the adapter refuses to connect to a real venue unless
            # HERMX_LIVE_TRADING is explicitly armed. A close-only flatten bypasses
            # this gate (it only REDUCES exposure) to mirror the service-level bypass
            # in execution/service.py -- otherwise an emergency close can never reach
            # the venue while the kill switch is off.
            if not close_only and not read_only and not live_trading_enabled()[0]:
                raise RuntimeError(
                    "live_trading_disabled: CcxtExecutor refuses to connect to live venue "
                    "without HERMX_LIVE_TRADING=true"
                )

        self._cached_client = client
        return client

    def _market_spec(self, client, symbol: str) -> dict:
        market = {}
        try:
            if hasattr(client, "load_markets"):
                client.load_markets()
            if hasattr(client, "market"):
                market = client.market(symbol) or {}
            elif hasattr(client, "markets"):
                market = (getattr(client, "markets", {}) or {}).get(symbol) or {}
        except Exception:
            # Do NOT fall back to market={} -> contract_size=1.0: on a contract where
            # contractSize != 1 that fabricated default silently mis-sizes every order.
            # Re-raise so execute() records UNKNOWN (submit_timeout/submit_exception)
            # instead of sizing and submitting on a guessed spec.
            raise

        contract_size = _to_float((market or {}).get("contractSize"), 1.0) or 1.0
        precision_step = _step_from_precision(((market or {}).get("precision") or {}).get("amount"))
        min_amount = _to_float((((market or {}).get("limits") or {}).get("amount") or {}).get("min"), 0.0) or 0.0
        min_cost = _to_float((((market or {}).get("limits") or {}).get("cost") or {}).get("min"), 0.0) or 0.0
        step = precision_step or min_amount or 1.0
        return {
            "market": market,
            "contract_size": max(contract_size, 1e-12),
            "step": max(float(step), 1e-12),
            "min_amount": max(float(min_amount), 0.0),
            "min_cost": max(float(min_cost), 0.0),
            "is_contract": bool((market or {}).get("contract")),
        }

    def _position_snapshot(self, client, symbol: str) -> dict:
        rows = []
        if hasattr(client, "fetch_positions"):
            try:
                rows = client.fetch_positions([symbol]) or []
            except Exception:
                rows = []

        for row in rows:
            row_symbol = str(row.get("symbol") or "")
            if row_symbol and row_symbol != symbol:
                continue
            contracts = _to_float(row.get("contracts"), None)
            if contracts is None:
                contracts = _to_float(row.get("contractsSize"), 0.0)
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            side = str(row.get("side") or "").lower()
            if side not in {"long", "short"}:
                # ccxt left the side blank -> recover it from venue-native fields before
                # giving up. Defaulting a blank side to "long" (the old behavior)
                # mislabels a real short, so a CLOSE_SHORT is skipped and the short is
                # never flattened. Fall back to "unknown" (not "long") when truly
                # undeterminable so the close path can still act on the action's side.
                pos_side = str(info.get("posSide") or "").lower()
                signed = _to_float(info.get("pos") or info.get("positionAmt"), None)
                if pos_side in {"long", "short"}:
                    side = pos_side
                elif signed is not None and signed < 0:
                    side = "short"
                elif signed is not None and signed > 0:
                    side = "long"
                else:
                    side = "unknown"
            contracts = abs(float(contracts or 0.0))
            if contracts > 0:
                return {"side": side, "contracts": contracts, "raw": row}
        return {"side": "flat", "contracts": 0.0, "raw": None}

    def _target_direction(self, readiness: dict) -> str:
        intent = (readiness or {}).get("execution_intent") or {}
        target = str(intent.get("target_direction") or "").lower()
        if target in {"long", "short"}:
            return target
        signal_side = str((readiness or {}).get("signal_side") or "").lower()
        if signal_side == "buy":
            return "long"
        if signal_side == "sell":
            return "short"
        return ""

    def _expanded_actions(self, readiness: dict, current_side: str) -> list[str]:
        intent = (readiness or {}).get("execution_intent") or {}
        actions = [str(a or "").upper() for a in (intent.get("actions") or []) if str(a or "").strip()]
        target = self._target_direction(readiness)
        # side_policy suppressed the OPEN leg upstream: never synthesize it in the fallback.
        open_suppressed = bool(intent.get("open_suppressed"))
        out: list[str] = []

        for action in actions:
            if action == "CLOSE_OPPOSITE_IF_ANY":
                if target == "long" and current_side == "short":
                    out.append("CLOSE_SHORT")
                elif target == "short" and current_side == "long":
                    out.append("CLOSE_LONG")
                continue
            if action in {"CLOSE_LONG", "CLOSE_SHORT", "OPEN_LONG", "OPEN_SHORT"}:
                out.append(action)

        if not out and target and not open_suppressed:
            if target == "long" and current_side == "short":
                out.append("CLOSE_SHORT")
            elif target == "short" and current_side == "long":
                out.append("CLOSE_LONG")
            out.append(f"OPEN_{target.upper()}")
        return out

    def _record_hl_cloid(self, order, client_order_id, exchange_id: str) -> None:
        """Persist the submit-time mxc->cloid mapping for Hyperliquid (Phase 7b).

        Hyperliquid echoes a numeric/hex cloid (not the submitted ``mxc`` id) in
        order history, so ``is_hermx_cl_ord_id`` can't attribute it by prefix.
        Recording the returned cloid here lets reconciliation resolve it back.
        Best-effort: a map-write failure must never fail a live submit.
        """
        if exchange_id != "hyperliquid" or not client_order_id or not isinstance(order, dict):
            return
        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        returned_cloid = order.get("cloid") or order.get("clientOrderId") or info.get("cloid")
        if returned_cloid and str(returned_cloid) != str(client_order_id):
            try:
                from pnl_cloid_map import record_cloid_mapping
                record_cloid_mapping(str(client_order_id), str(returned_cloid), "hyperliquid")
            except Exception as e:
                logger.debug("cloid map write failed: %s", e, exc_info=False)

    def _order_params(self, *, readiness: dict, reduce_only: bool, client_order_id: str | None, exchange_id: str) -> dict:
        params = {}
        is_hyperliquid = exchange_id == "hyperliquid"
        if client_order_id:
            if is_hyperliquid:
                # ccxt reads the client id from ``clientOrderId`` (not ``cloid``) and maps
                # it to Hyperliquid's on-wire cloid; a raw id / OKX ``clOrdId`` is rejected.
                # Passing ``cloid`` here is silently dropped, so the order would carry no
                # client id and reconciliation-by-cloid could never match it.
                params["clientOrderId"] = _to_hyperliquid_cloid(str(client_order_id))
            else:
                params["clOrdId"] = str(client_order_id)
                params["clientOrderId"] = str(client_order_id)

        if not is_hyperliquid:
            td_mode = str((readiness or {}).get("td_mode") or self.execution_cfg.get("td_mode") or "").lower()
            if td_mode:
                params["tdMode"] = td_mode

        if reduce_only:
            params["reduceOnly"] = True

        return params

    @staticmethod
    def _leverage_params(exchange_id: str, td_mode: str, target_side: str) -> dict:
        """Per-venue params for ``client.set_leverage(lev, symbol, params)``.

        okx         -> {"mgnMode": td_mode}
        bitget      -> {"holdSide": target_side} when isolated (per-side leverage), else {}
        gate/gateio -> {"marginMode": td_mode}
        hyperliquid -> {"marginMode": td_mode} — MUST be explicit: ccxt defaults
                       hyperliquid's set_leverage to cross, the opposite of HermX's
                       isolated default, so omitting it would be actively wrong.
        bybit       -> {} — ccxt sets buyLeverage=sellLeverage internally; a stray
                       mgnMode would leak into the raw request.
        binance     -> {}
        kucoin      -> {} — contract set_leverage is cross-only; isolated raises
                       NotSupported, absorbed by the caller's fail-open except.
        coinbase    -> never reached (spot-only; filtered by the ``setLeverage``
                       capability gate at the call site).
        anything else -> {} (safe fallback: ccxt's own default takes over, and
                       fail-open protects against a wrong default).
        """
        if exchange_id == "okx":
            return {"mgnMode": td_mode}
        if exchange_id == "bitget":
            return {"holdSide": target_side} if td_mode == "isolated" else {}
        if exchange_id in ("gate", "gateio", "hyperliquid"):
            return {"marginMode": td_mode}
        return {}

    def _reference_price(self, client, readiness: dict) -> float | None:
        intent = (readiness or {}).get("execution_intent") or {}
        price = (
            _to_float((readiness or {}).get("signal_price"), None)
            or _to_float((readiness or {}).get("okx_mark_price"), None)
            or _to_float((readiness or {}).get("okx_last_price"), None)
            or _to_float(intent.get("paper_execution_price"), None)
        )
        if price and price > 0:
            return price
        symbol = (
            (readiness or {}).get("ccxt_symbol")
            or _inst_id_to_ccxt_symbol((readiness or {}).get("inst_id") or ((readiness or {}).get("instrument") or {}).get("inst_id"))
            or _inst_id_to_ccxt_symbol((readiness or {}).get("symbol"))
        )
        if not symbol:
            return None
        try:
            ticker = client.fetch_ticker(symbol) or {}
            return _to_float(ticker.get("last"), None)
        except Exception:
            return None

    def _close_fallback_price(self, position: dict, reference_price: float | None) -> float | None:
        """Last-resort price for a Hyperliquid reduce-only close when the ticker feed is
        down (``_reference_price`` returned None). Prefers the live reference, then the
        open position's own mark/entry price (ccxt top-level, then venue-native ``info``),
        so an emergency flatten still carries a usable slippage bound."""
        if reference_price and reference_price > 0:
            return reference_price
        raw = (position or {}).get("raw") or {}
        for key in ("markPrice", "entryPrice", "lastPrice"):
            val = _to_float(raw.get(key), None)
            if val and val > 0:
                return val
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        for key in ("markPx", "entryPx", "avgPx", "avgEntryPx"):
            val = _to_float(info.get(key), None)
            if val and val > 0:
                return val
        return None

    def _contracts_for_notional(self, notional_usd: float, price: float, market_spec: dict) -> tuple[float, str]:
        """Returns ``(qty, skip_reason)``. skip_reason is "" when qty > 0;
        "below_instrument_min" when the size was zeroed by the venue's
        limits.amount.min / limits.cost.min; plain "zero_size" otherwise
        (no price, zero notional, step-floored with no venue minimum)."""
        contract_size = float(market_spec.get("contract_size") or 1.0)
        step = float(market_spec.get("step") or 1.0)
        min_amount = float(market_spec.get("min_amount") or 0.0)
        min_cost = float(market_spec.get("min_cost") or 0.0)
        if price <= 0 or contract_size <= 0:
            return 0.0, "zero_size"
        notional = float(notional_usd or 0.0)
        if notional <= 0:
            return 0.0, "zero_size"
        if min_cost > 0 and notional < min_cost:
            return 0.0, "below_instrument_min"
        raw = notional / (float(price) * contract_size)
        qty = _decimal_floor(max(0.0, raw), step)
        if min_amount > 0 and qty < min_amount:
            return 0.0, "below_instrument_min"
        if qty <= 0:
            return 0.0, "zero_size"
        return qty, ""

    def _amount_from_readiness(self, readiness: dict, market_spec: dict, reference_price: float | None) -> tuple[float, str]:
        explicit_amount = _to_float((readiness or {}).get("amount"), None)
        if explicit_amount is not None and explicit_amount > 0:
            step = float(market_spec.get("step") or 1.0)
            min_amount = float(market_spec.get("min_amount") or 0.0)
            qty = _decimal_floor(explicit_amount, step)
            if min_amount > 0 and qty < min_amount:
                return 0.0, "below_instrument_min"
            if qty <= 0:
                return 0.0, "zero_size"
            return qty, ""

        intent = (readiness or {}).get("execution_intent") or {}
        planned_notional = _to_float(intent.get("planned_notional_usd"), 0.0) or 0.0
        if planned_notional <= 0 or not reference_price or reference_price <= 0:
            return 0.0, "zero_size"
        return self._contracts_for_notional(planned_notional, reference_price, market_spec)

    def _sufficient_free_balance(self, client, market_spec, notional, leverage) -> bool:
        """Item A pre-trade balance check: is the settle-currency free balance enough
        to margin an open of ``notional`` at ``leverage``?

        OPEN leg only — the reduce-only close branch is NEVER gated by this
        (never-block-a-close invariant). Live mode only: demo/sandbox balances are
        arbitrary, so the check is skipped entirely under ``simulated_trading``.
        FAIL OPEN: a failed/absent/unparseable balance read must never block a
        submit — the venue stays the authority on margin rejection.
        """
        if bool(self.execution_cfg.get("simulated_trading", True)):
            return True
        try:
            lev = _to_float(leverage, None)
            if lev is None or lev <= 0:
                lev = 1.0
            required = (_to_float(notional, 0.0) or 0.0) / lev
            if required <= 0:
                return True
            settle = str(((market_spec or {}).get("market") or {}).get("settle") or "USDT")
            bal = client.fetch_balance()
            if not isinstance(bal, dict):
                return True  # fail-open: venue returned no balance data
            free = _to_float((bal.get("free") or {}).get(settle), None)
            if free is None:
                return True  # fail-open: settle currency absent from the response
            if free < required:
                logger.warning(
                    "insufficient_balance: free %s %.8f < required margin %.8f (notional=%.2f leverage=%.2f)",
                    settle, free, required, _to_float(notional, 0.0) or 0.0, lev,
                )
                return False
            return True
        except Exception as exc:
            logger.warning("balance check failed (fail-open): %s", redact_secrets(str(exc)))
            return True

    def _state_from_ccxt(self, order: dict) -> str:
        status = str((order or {}).get("status") or "").lower()
        filled = _to_float((order or {}).get("filled"), 0.0) or 0.0
        amount = _to_float((order or {}).get("amount"), 0.0) or 0.0

        if status in {"open", "new"}:
            return "partially_filled" if filled > 0.0 else "live"
        if status in {"closed", "filled"}:
            if amount > 0 and 0 < filled < amount:
                return "partially_filled"
            return "filled"
        if status in {"canceled", "cancelled", "expired", "rejected"}:
            return "canceled"
        if status in {"not_found"}:
            return "not_found"
        return "unknown"

    def _normalize_order(self, order: dict) -> dict:
        if not isinstance(order, dict):
            return empty_normalized_order(self.key, state="error", raw=order)
        params = order.get("info") or {}
        inst_id = params.get("instId") or _ccxt_symbol_to_inst_id(order.get("symbol")) or order.get("symbol")
        return {
            "exchange": self.key,
            "inst_id": inst_id,
            "ord_id": order.get("id") or None,
            "cl_ord_id": order.get("clientOrderId") or params.get("clOrdId") or None,
            "state": self._state_from_ccxt(order),
            "acc_fill_sz": _to_float(order.get("filled"), 0.0),
            "avg_px": _to_float(order.get("average"), None),
            "ord_type": order.get("type"),
            "side": order.get("side"),
            "pos_side": params.get("posSide"),
            "ts": order.get("timestamp"),
            "raw": order,
        }

    def _partial_fill_summary(self, executed_orders: list, client_order_id, current_side, current_contracts) -> dict:
        """Fill summary for a partial multi-leg submit (a later leg failed/timed out).

        Carries the LAST successfully submitted order so reconciliation can find the
        leg that did reach the venue (e.g. the executed close), plus the resulting
        position, instead of returning an empty summary that discards that state.
        """
        last_order = None
        for row in reversed(executed_orders or []):
            if isinstance(row.get("order"), dict):
                last_order = row.get("order")
                break
        fill = empty_fill_summary(client_order_id)
        if isinstance(last_order, dict):
            fill["status"] = "submit_partial"
            fill["order_id"] = last_order.get("id")
            fill["avg_fill_price"] = _to_float(last_order.get("average"), None)
            fill["filled_size"] = _to_float(last_order.get("filled"), None)
        fill["position_after_order"] = {"side": current_side, "contracts": current_contracts}
        return fill

    def execute(self, readiness: dict) -> dict:
        started = time.time()
        exchange_id = self._exchange_id()
        intent = (readiness or {}).get("execution_intent") or {}
        client_order_id = intent.get("client_order_id")
        # Distinct clOrdId per leg so a reversal's close + open are not rejected as a
        # duplicate clOrdId. Fall back to the single id when the split fields are absent.
        client_order_id_close = intent.get("client_order_id_close") or client_order_id
        client_order_id_open = intent.get("client_order_id_open") or client_order_id

        symbol = (
            (readiness or {}).get("ccxt_symbol")
            or _inst_id_to_ccxt_symbol((readiness or {}).get("inst_id") or ((readiness or {}).get("instrument") or {}).get("inst_id"))
            or _inst_id_to_ccxt_symbol((readiness or {}).get("symbol"))
        )
        direction = self._target_direction(readiness)
        # A close-only flatten (operator close) carries explicit CLOSE_LONG/CLOSE_SHORT
        # actions and has no target direction — the direction gate is a fallback guard
        # for the open path only, so skip it here. Do NOT synthesize a dummy direction:
        # _expanded_actions would turn a "nothing to close" case into an OPEN of that side.
        close_only = bool((readiness or {}).get("close_only"))
        if not close_only and direction not in {"long", "short"}:
            return self.normalized_result(
                ok=False,
                mode="submit_failed",
                elapsed_ms=round((time.time() - started) * 1000),
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": "invalid_direction"},
            )

        try:
            client = self._client(close_only=close_only)
            if not symbol:
                raise RuntimeError("symbol_unresolved")

            market_spec = self._market_spec(client, symbol)
            reference_price = self._reference_price(client, readiness)
            open_amount, open_skip_reason = self._amount_from_readiness(readiness, market_spec, reference_price)
            position = self._position_snapshot(client, symbol)
            current_side = str(position.get("side") or "flat")
            current_contracts = float(position.get("contracts") or 0.0)
            close_amount = _decimal_floor(current_contracts, market_spec.get("step") or 1.0)

            actions = self._expanded_actions(readiness, current_side)
            if not actions:
                raise RuntimeError("no_executable_actions")

            executed_orders = []
            order_type = str(self.execution_cfg.get("order_type") or "market").lower()
            price = reference_price if order_type in {"limit", "stop", "stop_limit"} else None
            # Hyperliquid needs a reference price even for market orders (slippage bound).
            if exchange_id == "hyperliquid":
                price = reference_price

            for action in actions:
                if action in {"CLOSE_LONG", "CLOSE_SHORT"}:
                    expected_side = "long" if action == "CLOSE_LONG" else "short"
                    # Skip only when the venue reports a DEFINITE opposite side. If the
                    # side is unknown (ccxt left it blank and no native field disambiguated
                    # it), trust the action's expected_side rather than skipping -- a real
                    # short reported with a blank side must stay closable (reduceOnly makes
                    # a wrong guess a no-op at the venue, so this is safe).
                    if current_side not in {expected_side, "unknown"}:
                        executed_orders.append({
                            "action": action,
                            "submitted": False,
                            "status": "skipped",
                            "reason": f"no_{expected_side}_position_to_close",
                        })
                        continue
                    if close_amount <= 0:
                        executed_orders.append({
                            "action": action,
                            "submitted": False,
                            "status": "skipped",
                            "reason": "zero_size",
                        })
                        continue
                    close_side = "sell" if expected_side == "long" else "buy"
                    params = self._order_params(
                        readiness=readiness,
                        reduce_only=True,
                        client_order_id=client_order_id_close,
                        exchange_id=exchange_id,
                    )
                    # Hyperliquid rejects an order with a None price. On a reduce-only
                    # close the reference feed being down must NOT block an emergency
                    # flatten, so fall back to the position's own mark/entry price.
                    close_price = price
                    if exchange_id == "hyperliquid" and close_price is None:
                        close_price = self._close_fallback_price(position, reference_price)
                    try:
                        order = client.create_order(symbol, order_type, close_side, close_amount, close_price, params)
                        self._record_hl_cloid(order, client_order_id_close, exchange_id)
                        executed_orders.append({
                            "action": action,
                            "submitted": True,
                            "status": "submitted",
                            "order": order,
                            "requested_amount": close_amount,
                        })
                        current_side = "flat"
                        current_contracts = 0.0
                    except Exception as exc:
                        if _is_timeout_error(exc):
                            # Submit may have reached the venue -> UNKNOWN, not a reject.
                            # Preserve any partial state (orders already placed) so
                            # reconciliation can find them.
                            return self.normalized_result(
                                ok=False,
                                mode="submit_timeout",
                                elapsed_ms=round((time.time() - started) * 1000),
                                fill_summary=self._partial_fill_summary(executed_orders, client_order_id, current_side, current_contracts),
                                payload={"error": redact_secrets(str(exc)), "action": action, "symbol": symbol, "executed_orders": executed_orders},
                            )
                        executed_orders.append({
                            "action": action,
                            "submitted": True,
                            "status": "rejected",
                            "error": redact_secrets(str(exc)),
                        })
                        break
                    continue

                if action in {"OPEN_LONG", "OPEN_SHORT"}:
                    target_side = "long" if action == "OPEN_LONG" else "short"
                    if current_side == target_side:
                        executed_orders.append({
                            "action": action,
                            "submitted": False,
                            "status": "skipped",
                            "reason": f"already_{target_side}_no_pyramid",
                        })
                        continue
                    if current_side in {"long", "short"}:
                        executed_orders.append({
                            "action": action,
                            "submitted": False,
                            "status": "blocked",
                            "reason": f"opposite_position_still_open_{current_side}",
                        })
                        continue
                    if open_amount <= 0:
                        executed_orders.append({
                            "action": action,
                            "submitted": False,
                            "status": "skipped",
                            # below_instrument_min when the size was zeroed by the
                            # venue's amount/cost minimum; plain zero_size otherwise.
                            "reason": open_skip_reason or "zero_size",
                        })
                        continue

                    # Item A: live-mode pre-trade balance check. OPEN leg only — the
                    # close branch above is never gated (never-block-a-close). Fail-open
                    # inside the helper; a positive "insufficient" reuses the standard
                    # skipped-leg contract so all-legs-skip maps to submit_failed →
                    # REJECTED, never an unmapped UNKNOWN mode.
                    if not self._sufficient_free_balance(
                        client,
                        market_spec,
                        _to_float(intent.get("planned_notional_usd"), 0.0) or 0.0,
                        (readiness or {}).get("leverage"),
                    ):
                        executed_orders.append({
                            "action": action,
                            "submitted": False,
                            "status": "skipped",
                            "reason": "insufficient_balance",
                        })
                        continue

                    # Leverage sync: push the strategy's configured leverage to the
                    # venue before the open — venues otherwise keep whatever leverage
                    # was last set on the instrument, silently diverging position
                    # leverage/liquidation price from the strategy config. All
                    # derivatives venues; per-venue params come from
                    # _leverage_params() (see its table). Spot-only venues
                    # (coinbase) are filtered by the setLeverage capability gate.
                    # Runs in demo AND live (unlike the balance check above: demo
                    # balances are fake, but demo leverage/margin state is real).
                    # FAIL OPEN: a set_leverage error must never block the open
                    # (this also absorbs kucoin's NotSupported for isolated).
                    lev = _to_float((readiness or {}).get("leverage"), None)
                    if lev and lev > 0 and (getattr(client, "has", {}) or {}).get("setLeverage"):
                        if float(lev).is_integer():
                            lev = int(lev)
                        td_mode = str((readiness or {}).get("td_mode") or self.execution_cfg.get("td_mode") or "isolated").lower()
                        try:
                            client.set_leverage(lev, symbol, params=self._leverage_params(exchange_id, td_mode, target_side))
                        except Exception as exc:
                            logger.warning("set_leverage failed (fail-open): %s", redact_secrets(str(exc)))

                    open_side = "buy" if target_side == "long" else "sell"
                    params = self._order_params(
                        readiness=readiness,
                        reduce_only=False,
                        client_order_id=client_order_id_open,
                        exchange_id=exchange_id,
                    )
                    try:
                        order = client.create_order(symbol, order_type, open_side, open_amount, price, params)
                        self._record_hl_cloid(order, client_order_id_open, exchange_id)
                        executed_orders.append({
                            "action": action,
                            "submitted": True,
                            "status": "submitted",
                            "order": order,
                            "requested_amount": open_amount,
                        })
                        current_side = target_side
                        current_contracts = open_amount
                    except Exception as exc:
                        if _is_timeout_error(exc):
                            # Submit may have reached the venue -> UNKNOWN, not a reject.
                            # Preserve the partial state (e.g. an executed close leg) so
                            # reconciliation can find it.
                            return self.normalized_result(
                                ok=False,
                                mode="submit_timeout",
                                elapsed_ms=round((time.time() - started) * 1000),
                                fill_summary=self._partial_fill_summary(executed_orders, client_order_id, current_side, current_contracts),
                                payload={"error": redact_secrets(str(exc)), "action": action, "symbol": symbol, "executed_orders": executed_orders},
                            )
                        executed_orders.append({
                            "action": action,
                            "submitted": True,
                            "status": "rejected",
                            "error": redact_secrets(str(exc)),
                        })
                        break
                    continue

            submitted = [row for row in executed_orders if row.get("submitted")]
            succeeded = [row for row in executed_orders if row.get("status") == "submitted"]
            bad = [row for row in executed_orders if row.get("status") in {"rejected", "blocked", "close_not_verified"}]
            partial = bool(succeeded) and bool(bad)
            if partial:
                status = "submit_partial"
            elif bad:
                status = str(bad[0].get("status") or "rejected")
            elif submitted:
                # Hyperliquid only: if every submitted leg came back FULLY filled, report
                # "filled" so the service records FILLED directly. Reconciliation on
                # Hyperliquid can't confirm the fill in time -- its reconcile executor
                # defaults to a single venue and the order-status endpoint lags minutes
                # after a market IOC fill -- so without this the order sits UNKNOWN until
                # the resolver timeout and pauses the symbol. Gated to Hyperliquid so
                # OKX's submit->reconcile path stays byte-identical.
                if (
                    exchange_id == "hyperliquid"
                    and succeeded
                    and all(_order_fully_filled(r.get("order"), r.get("requested_amount")) for r in succeeded)
                ):
                    status = "filled"
                else:
                    status = "submitted"
            else:
                status = "dry_run"

            last_order = None
            for row in reversed(executed_orders):
                if isinstance(row.get("order"), dict):
                    last_order = row.get("order")
                    break

            elapsed_ms = round((time.time() - started) * 1000)

            fill = empty_fill_summary(client_order_id)
            fill["status"] = status
            fill["order_id"] = (last_order or {}).get("id") if isinstance(last_order, dict) else None
            fill["avg_fill_price"] = _to_float((last_order or {}).get("average"), None) if isinstance(last_order, dict) else None
            fill["filled_size"] = _to_float((last_order or {}).get("filled"), None) if isinstance(last_order, dict) else None
            fill["position_after_order"] = {"side": current_side, "contracts": current_contracts}

            # A leg reached the venue while another failed -> partial submit. Venue state
            # is uncertain (the close may have executed), so this is NOT a flat reject:
            # surface mode "submit_partial" so the service records UNKNOWN, not REJECTED.
            if partial:
                ok = False
                mode = "submit_partial"
            else:
                ok = bool(submitted) and not bool(bad)
                mode = "submit_enabled" if ok else "submit_failed"

            return self.normalized_result(
                ok=ok,
                mode=mode,
                elapsed_ms=elapsed_ms,
                fill_summary=fill,
                payload={
                    "executed_orders": executed_orders,
                    "symbol": symbol,
                    "reference_price": reference_price,
                    "open_amount": open_amount,
                    "close_amount": close_amount,
                    "actions": actions,
                    "target_direction": direction,
                },
            )
        except Exception as exc:
            # A ccxt timeout/network error -> submit_timeout (UNKNOWN); any other
            # failure -> submit_exception (also UNKNOWN). Never a silent reject.
            mode = "submit_timeout" if _is_timeout_error(exc) else "submit_exception"
            return self.normalized_result(
                ok=False,
                mode=mode,
                elapsed_ms=round((time.time() - started) * 1000),
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": redact_secrets(str(exc)), "symbol": symbol, "side": direction},
            )

    def get_order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> dict:
        try:
            client = self._client(read_only=True)
            symbol = _inst_id_to_ccxt_symbol(inst_id) or inst_id

            if ord_id:
                order = client.fetch_order(ord_id, symbol=symbol)
                return self._normalize_order(order)

            if cl_ord_id:
                # On Hyperliquid, fetch_closed_orders lags right after a fill, so the
                # scan below sees not_found and the UNKNOWN-resolver spams
                # RECONCILE_MISMATCH until the symbol pauses. fetch_order accepts the
                # cloid against the live orderStatus endpoint (immediate), so try that
                # first. The clientOrderId returned by the scan endpoints is the hashed
                # cloid — not the raw HermX id — so the scan must compare against it too.
                is_hyperliquid = self._exchange_id() == "hyperliquid"
                match_id = _to_hyperliquid_cloid(str(cl_ord_id)) if is_hyperliquid else str(cl_ord_id)
                if is_hyperliquid:
                    try:
                        order = client.fetch_order(match_id, symbol=symbol)
                        if order:
                            return self._normalize_order(order)
                    except Exception:
                        pass
                for order in (client.fetch_open_orders(symbol=symbol) or []):
                    if str(order.get("clientOrderId") or "") == match_id:
                        return self._normalize_order(order)
                try:
                    for order in (client.fetch_closed_orders(symbol=symbol, limit=200) or []):
                        if str(order.get("clientOrderId") or "") == match_id:
                            return self._normalize_order(order)
                except Exception:
                    pass
                return empty_normalized_order(self.key, state="not_found", raw={"cl_ord_id": cl_ord_id, "symbol": symbol})

            return empty_normalized_order(self.key, state="not_found", raw={"error": "ord_id_or_cl_ord_id_required"})
        except Exception as exc:
            text = str(exc).lower()
            if "not found" in text or "order does not exist" in text:
                return empty_normalized_order(self.key, state="not_found", raw={"error": redact_secrets(str(exc))})
            return empty_normalized_order(self.key, state="error", raw={"error": redact_secrets(str(exc))})

    def get_open_orders(self, inst_id: str | None = None) -> list:
        try:
            client = self._client(read_only=True)
            symbol = _inst_id_to_ccxt_symbol(inst_id) if inst_id else None
            return [self._normalize_order(o) for o in (client.fetch_open_orders(symbol=symbol) or [])]
        except Exception:
            return []

    def get_order_history_raw(self, inst_ids: list[str] | None = None, limit: int = 100) -> list:
        try:
            client = self._client(read_only=True)
            rows: list[dict] = []
            targets = list(inst_ids or [])
            if not targets:
                targets = []

            for inst_id in targets:
                symbol = _inst_id_to_ccxt_symbol(inst_id) or inst_id
                try:
                    closed = client.fetch_closed_orders(symbol=symbol, limit=max(1, int(limit))) or []
                except Exception:
                    closed = []
                venue = self._exchange_id()
                for order in closed:
                    info = order.get("info") or {}
                    fee = order.get("fee") or {}
                    rows.append(
                        {
                            "instId": inst_id,
                            "ordId": order.get("id"),
                            "clOrdId": order.get("clientOrderId") or info.get("clOrdId"),
                            "side": order.get("side"),
                            "posSide": info.get("posSide"),
                            "tdMode": info.get("tdMode"),
                            "avgPx": order.get("average"),
                            "accFillSz": order.get("filled"),
                            "fillSz": info.get("fillSz") or order.get("filled"),
                            "sz": order.get("amount"),
                            "fee": fee.get("cost") if isinstance(fee, dict) else None,
                            "feeCcy": fee.get("currency") if isinstance(fee, dict) else None,
                            "pnl": info.get("pnl"),
                            "realized_pnl": _normalized_realized_pnl(order, info, venue),
                            "reduceOnly": _normalize_reduce_only(order, info),
                            "state": self._state_from_ccxt(order),
                            "lever": info.get("lever"),
                            "cTime": order.get("timestamp"),
                            "uTime": order.get("lastTradeTimestamp") or order.get("timestamp"),
                        }
                    )

            rows.sort(key=lambda row: str(row.get("uTime") or row.get("cTime") or ""), reverse=True)
            return rows
        except Exception:
            return []

    def get_order_history_archive(self, inst_id: str | None = None, limit: int = 100) -> list:
        try:
            client = self._client(read_only=True)
            symbol = _inst_id_to_ccxt_symbol(inst_id) if inst_id else None
            return [self._normalize_order(o) for o in (client.fetch_closed_orders(symbol=symbol, limit=max(1, int(limit))) or [])]
        except Exception:
            return []

    def get_positions(self, inst_id: str | None = None) -> list:
        try:
            client = self._client(read_only=True)
            symbols = [_inst_id_to_ccxt_symbol(inst_id) or inst_id] if inst_id else None
            rows = client.fetch_positions(symbols) if hasattr(client, "fetch_positions") else []
            out = []
            for row in (rows or []):
                contracts = _to_float(row.get("contracts"), 0.0) or 0.0
                side = str(row.get("side") or "").lower()
                signed = contracts if side == "long" else (-contracts if side == "short" else contracts)
                symbol_value = row.get("symbol")
                out.append(
                    {
                        "exchange": self.key,
                        "inst_id": _ccxt_symbol_to_inst_id(symbol_value) or symbol_value,
                        "pos": signed,
                        "pos_side": side or "net",
                        "avg_px": _to_float(row.get("entryPrice"), None),
                        "upl": _to_float(row.get("unrealizedPnl"), None),
                        "realized_pnl": _to_float(row.get("realizedPnl"), None),
                        "raw": row,
                    }
                )
            return out
        except Exception:
            return []

    def get_balance(self, ccy: str | None = None) -> list:
        try:
            client = self._client(read_only=True)
            bal = client.fetch_balance() or {}
            total = bal.get("total") or {}
            free = bal.get("free") or {}
            rows = []
            for currency, eq in total.items():
                if ccy and str(currency) != str(ccy):
                    continue
                rows.append(
                    {
                        "exchange": self.key,
                        "ccy": currency,
                        "eq": _to_float(eq, 0.0),
                        "avail": _to_float(free.get(currency), 0.0),
                        "raw": {"total": total.get(currency), "free": free.get(currency)},
                    }
                )
            return rows
        except Exception:
            return []

    def get_balance_summary(self, currency: str = "USDT") -> dict | None:
        """Single-currency ``{free, used, total, currency}`` equity as a dict, or None.

        DISTINCT from :meth:`get_balance` (the observe-only per-currency LIST query
        contract, unchanged): this returns ONE currency's unified figures for the B2
        equity-drift check (``check_balance_drift``). Only meaningful in LIVE mode -- a
        demo sandbox balance is arbitrary. Never raises: a failed venue read returns
        None so the drift check degrades instead of crashing."""
        try:
            client = self._client(read_only=True)
            bal = client.fetch_balance() or {}
            total = (bal.get("total") or {}).get(currency)
            free = (bal.get("free") or {}).get(currency)
            used = (bal.get("used") or {}).get(currency)
            return {
                "free": _to_float(free, 0.0) or 0.0,
                "used": _to_float(used, 0.0) or 0.0,
                "total": _to_float(total, 0.0) or 0.0,
                "currency": currency,
            }
        except Exception as exc:
            logger.warning("get_balance_summary failed for %s: %s", currency, redact_secrets(str(exc)))
            return None

    def health(self) -> dict:
        try:
            client = self._client(read_only=True)
            balance = client.fetch_balance() or {}
            pos_rows = client.fetch_positions() if hasattr(client, "fetch_positions") else []

            positions = []
            for row in (pos_rows or []):
                info = row.get("info") or {}
                symbol_value = row.get("symbol")
                inst_id = info.get("instId") or _ccxt_symbol_to_inst_id(symbol_value) or symbol_value
                side = str(row.get("side") or info.get("posSide") or "").lower()
                contracts = _to_float(row.get("contracts"), 0.0) or 0.0
                signed = contracts if side != "short" else -contracts
                positions.append(
                    {
                        "instId": inst_id,
                        "posSide": info.get("posSide") or (side if side in {"long", "short"} else "net"),
                        "pos": signed,
                        "avgPx": _to_float(row.get("entryPrice"), None),
                        "notionalUsd": _to_float(row.get("notional"), None),
                        "upl": _to_float(row.get("unrealizedPnl"), None),
                        "realizedPnl": _to_float(row.get("realizedPnl"), _to_float(info.get("realizedPnl"), None)),
                        "lever": row.get("leverage") or info.get("lever"),
                        "mgnMode": row.get("marginMode") or info.get("mgnMode"),
                        "imr": info.get("imr"),
                        "mmr": info.get("mmr"),
                        "markPx": _to_float(row.get("markPrice"), None),
                        "last": _to_float(info.get("last"), None),
                    }
                )

            account_info = balance.get("info") if isinstance(balance, dict) else {}
            currencies = sorted((balance.get("total") or {}).keys()) if isinstance(balance, dict) else []

            return {
                "ok": True,
                "generated_at": _utc_now(),
                "exchange": self.key,
                "account": {
                    "posMode": (account_info or {}).get("posMode"),
                    "currencies": currencies,
                },
                "positions": positions,
            }
        except Exception as exc:
            return {"ok": False, "exchange": self.key, "error": redact_secrets(str(exc))}
