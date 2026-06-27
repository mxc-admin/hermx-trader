#!/usr/bin/env python3
"""CCXT-backed executor adapter.

CCXT is the exchange transport layer; HermX keeps risk/idempotency/journaling
above this adapter in the controlled execution API path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import os
import time

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    ccxt = None

from .base import BaseExecutor, empty_fill_summary, empty_normalized_order
from security.credentials import redact_secrets, resolve_exchange_credentials


def _to_float(value, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decimal_floor(value: float | Decimal, step: float | Decimal) -> float:
    d_value = Decimal(str(value or 0.0))
    d_step = Decimal(str(step or 0.0))
    if d_step <= 0:
        return float(d_value)
    units = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(units * d_step)


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
    try:
        seconds = float(os.environ.get("HERMX_SUBMIT_TIMEOUT_SECONDS", "45") or "45")
    except (TypeError, ValueError):
        seconds = 45.0
    return int(max(1.0, seconds) * 1000)


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


def _okx_inst_to_ccxt_symbol(inst_id: str | None) -> str | None:
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
    return text


def _ccxt_symbol_to_okx_inst(symbol: str | None) -> str | None:
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


class CcxtExecutor(BaseExecutor):
    key = "ccxt"

    def __init__(self, config: dict, root):
        super().__init__(config, root)
        self._cached_client = None

    def _exchange_id(self) -> str:
        return str(self.execution_cfg.get("ccxt_exchange") or self.execution_cfg.get("exchange") or "okx").lower()

    def _client(self):
        if self._cached_client is not None:
            return self._cached_client
        if ccxt is None:
            raise RuntimeError("ccxt_not_installed")

        exchange_id = self._exchange_id()
        exchange_cls = getattr(ccxt, exchange_id, None)
        if exchange_cls is None:
            raise ValueError(f"unsupported_ccxt_exchange:{exchange_id}")

        creds = resolve_exchange_credentials(exchange_id, os.environ)
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

        client = exchange_cls(kwargs)

        if bool(self.execution_cfg.get("simulated_trading", True)) and hasattr(client, "set_sandbox_mode"):
            try:
                client.set_sandbox_mode(True)
            except Exception:
                pass

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
            market = {}

        contract_size = _to_float((market or {}).get("contractSize"), 1.0) or 1.0
        precision_step = _step_from_precision(((market or {}).get("precision") or {}).get("amount"))
        min_amount = _to_float((((market or {}).get("limits") or {}).get("amount") or {}).get("min"), 0.0) or 0.0
        step = precision_step or min_amount or 1.0
        return {
            "market": market,
            "contract_size": max(contract_size, 1e-12),
            "step": max(float(step), 1e-12),
            "min_amount": max(float(min_amount), 0.0),
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
            contracts = abs(float(contracts or 0.0))
            side = str(row.get("side") or "").lower()
            if side not in {"long", "short"} and contracts > 0:
                side = "long"
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

        if not out and target:
            if target == "long" and current_side == "short":
                out.append("CLOSE_SHORT")
            elif target == "short" and current_side == "long":
                out.append("CLOSE_LONG")
            out.append(f"OPEN_{target.upper()}")
        return out

    def _order_params(self, *, readiness: dict, reduce_only: bool, position_side: str | None, client_order_id: str | None) -> dict:
        params = {}
        if client_order_id:
            params["clOrdId"] = str(client_order_id)
            params["clientOrderId"] = str(client_order_id)

        td_mode = str((readiness or {}).get("td_mode") or self.execution_cfg.get("td_mode") or "").lower()
        if td_mode:
            params["tdMode"] = td_mode

        if reduce_only:
            params["reduceOnly"] = True

        pos_mode = str(self.execution_cfg.get("ccxt_pos_mode") or "").lower()
        if pos_mode == "long_short_mode" and position_side in {"long", "short"}:
            params["posSide"] = position_side

        return params

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
            or _okx_inst_to_ccxt_symbol((readiness or {}).get("okx_inst_id") or (readiness or {}).get("inst_id"))
            or _okx_inst_to_ccxt_symbol((readiness or {}).get("symbol"))
        )
        if not symbol:
            return None
        try:
            ticker = client.fetch_ticker(symbol) or {}
            return _to_float(ticker.get("last"), None)
        except Exception:
            return None

    def _contracts_for_notional(self, notional_usd: float, price: float, market_spec: dict) -> float:
        contract_size = float(market_spec.get("contract_size") or 1.0)
        step = float(market_spec.get("step") or 1.0)
        min_amount = float(market_spec.get("min_amount") or 0.0)
        if price <= 0 or contract_size <= 0:
            return 0.0
        raw = float(notional_usd or 0.0) / (float(price) * contract_size)
        qty = _decimal_floor(max(0.0, raw), step)
        if min_amount > 0 and qty < min_amount:
            return 0.0
        return qty

    def _amount_from_readiness(self, readiness: dict, market_spec: dict, reference_price: float | None) -> float:
        explicit_amount = _to_float((readiness or {}).get("amount"), None)
        if explicit_amount is not None and explicit_amount > 0:
            step = float(market_spec.get("step") or 1.0)
            min_amount = float(market_spec.get("min_amount") or 0.0)
            qty = _decimal_floor(explicit_amount, step)
            if min_amount > 0 and qty < min_amount:
                return 0.0
            return qty

        intent = (readiness or {}).get("execution_intent") or {}
        planned_notional = _to_float(intent.get("planned_notional_usd"), 0.0) or 0.0
        if planned_notional <= 0 or not reference_price or reference_price <= 0:
            return 0.0
        return self._contracts_for_notional(planned_notional, reference_price, market_spec)

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
        inst_id = params.get("instId") or _ccxt_symbol_to_okx_inst(order.get("symbol")) or order.get("symbol")
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

    def execute(self, readiness: dict) -> dict:
        started = time.time()
        intent = (readiness or {}).get("execution_intent") or {}
        client_order_id = intent.get("client_order_id")

        symbol = (
            (readiness or {}).get("ccxt_symbol")
            or _okx_inst_to_ccxt_symbol((readiness or {}).get("okx_inst_id") or (readiness or {}).get("inst_id"))
            or _okx_inst_to_ccxt_symbol((readiness or {}).get("symbol"))
        )
        direction = self._target_direction(readiness)
        if direction not in {"long", "short"}:
            return self.normalized_result(
                ok=False,
                mode="submit_failed",
                elapsed_ms=round((time.time() - started) * 1000),
                fill_summary=empty_fill_summary(client_order_id),
                payload={"error": "invalid_direction"},
            )

        try:
            client = self._client()
            if not symbol:
                raise RuntimeError("symbol_unresolved")

            market_spec = self._market_spec(client, symbol)
            reference_price = self._reference_price(client, readiness)
            open_amount = self._amount_from_readiness(readiness, market_spec, reference_price)
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

            for action in actions:
                if action in {"CLOSE_LONG", "CLOSE_SHORT"}:
                    expected_side = "long" if action == "CLOSE_LONG" else "short"
                    if current_side != expected_side:
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
                        position_side=expected_side,
                        client_order_id=client_order_id,
                    )
                    try:
                        order = client.create_order(symbol, order_type, close_side, close_amount, price, params)
                        executed_orders.append({
                            "action": action,
                            "submitted": True,
                            "status": "submitted",
                            "order": order,
                        })
                        current_side = "flat"
                        current_contracts = 0.0
                    except Exception as exc:
                        if _is_timeout_error(exc):
                            # Submit may have reached the venue -> UNKNOWN, not a reject.
                            return self.normalized_result(
                                ok=False,
                                mode="submit_timeout",
                                elapsed_ms=round((time.time() - started) * 1000),
                                fill_summary=empty_fill_summary(client_order_id),
                                payload={"error": redact_secrets(str(exc)), "action": action, "symbol": symbol},
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
                            "reason": "zero_size",
                        })
                        continue

                    open_side = "buy" if target_side == "long" else "sell"
                    params = self._order_params(
                        readiness=readiness,
                        reduce_only=False,
                        position_side=target_side,
                        client_order_id=client_order_id,
                    )
                    try:
                        order = client.create_order(symbol, order_type, open_side, open_amount, price, params)
                        executed_orders.append({
                            "action": action,
                            "submitted": True,
                            "status": "submitted",
                            "order": order,
                        })
                        current_side = target_side
                        current_contracts = open_amount
                    except Exception as exc:
                        if _is_timeout_error(exc):
                            # Submit may have reached the venue -> UNKNOWN, not a reject.
                            return self.normalized_result(
                                ok=False,
                                mode="submit_timeout",
                                elapsed_ms=round((time.time() - started) * 1000),
                                fill_summary=empty_fill_summary(client_order_id),
                                payload={"error": redact_secrets(str(exc)), "action": action, "symbol": symbol},
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
            bad = [row for row in executed_orders if row.get("status") in {"rejected", "blocked", "close_not_verified"}]
            if bad:
                status = str(bad[0].get("status") or "rejected")
            elif submitted:
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
            client = self._client()
            symbol = _okx_inst_to_ccxt_symbol(inst_id) or inst_id

            if ord_id:
                order = client.fetch_order(ord_id, symbol=symbol)
                return self._normalize_order(order)

            if cl_ord_id:
                for order in (client.fetch_open_orders(symbol=symbol) or []):
                    if str(order.get("clientOrderId") or "") == str(cl_ord_id):
                        return self._normalize_order(order)
                try:
                    for order in (client.fetch_closed_orders(symbol=symbol, limit=200) or []):
                        if str(order.get("clientOrderId") or "") == str(cl_ord_id):
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
            client = self._client()
            symbol = _okx_inst_to_ccxt_symbol(inst_id) if inst_id else None
            return [self._normalize_order(o) for o in (client.fetch_open_orders(symbol=symbol) or [])]
        except Exception:
            return []

    def get_order_history_raw(self, inst_ids: list[str] | None = None, limit: int = 100) -> list:
        try:
            client = self._client()
            rows: list[dict] = []
            targets = list(inst_ids or [])
            if not targets:
                targets = []

            for inst_id in targets:
                symbol = _okx_inst_to_ccxt_symbol(inst_id) or inst_id
                try:
                    closed = client.fetch_closed_orders(symbol=symbol, limit=max(1, int(limit))) or []
                except Exception:
                    closed = []
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
                            "reduceOnly": info.get("reduceOnly"),
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
            client = self._client()
            symbol = _okx_inst_to_ccxt_symbol(inst_id) if inst_id else None
            return [self._normalize_order(o) for o in (client.fetch_closed_orders(symbol=symbol, limit=max(1, int(limit))) or [])]
        except Exception:
            return []

    def get_positions(self, inst_id: str | None = None) -> list:
        try:
            client = self._client()
            symbols = [_okx_inst_to_ccxt_symbol(inst_id) or inst_id] if inst_id else None
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
                        "inst_id": _ccxt_symbol_to_okx_inst(symbol_value) or symbol_value,
                        "pos": signed,
                        "pos_side": side or "net",
                        "avg_px": _to_float(row.get("entryPrice"), None),
                        "upl": _to_float(row.get("unrealizedPnl"), None),
                        "raw": row,
                    }
                )
            return out
        except Exception:
            return []

    def get_balance(self, ccy: str | None = None) -> list:
        try:
            client = self._client()
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

    def health(self) -> dict:
        try:
            client = self._client()
            balance = client.fetch_balance() or {}
            pos_rows = client.fetch_positions() if hasattr(client, "fetch_positions") else []

            positions = []
            for row in (pos_rows or []):
                info = row.get("info") or {}
                symbol_value = row.get("symbol")
                inst_id = info.get("instId") or _ccxt_symbol_to_okx_inst(symbol_value) or symbol_value
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
                        "realizedPnl": _to_float(info.get("realizedPnl"), None),
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
