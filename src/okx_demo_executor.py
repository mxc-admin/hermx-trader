#!/usr/bin/env python3
"""OKX demo execution planner/executor for the MXC shadow system.

Default behavior is read-only / dry-run. It can read OKX demo account state and
turn an execution_readiness block into exchange-sized order intents without
sending orders. When both the readiness payload and environment explicitly allow
submission, it can also send OKX demo market orders.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path


BASE_URL = os.environ.get("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
USER_AGENT = os.environ.get("OKX_USER_AGENT", "Mozilla/5.0 (compatible; MXCShadow/1.0)")
SIMULATED_TRADING = os.environ.get("OKX_SIMULATED_TRADING", "1") != "0"
FORCE_IPV4 = os.environ.get("OKX_FORCE_IPV4", "1") != "0"
SUBMIT_ORDERS = os.environ.get("OKX_SUBMIT_ORDERS", "false").lower() == "true"
ENFORCE_LEVERAGE = os.environ.get("OKX_ENFORCE_LEVERAGE", "true").lower() != "false"


def install_ipv4_resolver() -> None:
    if not FORCE_IPV4:
        return
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_getaddrinfo


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


class OkxClient:
    def __init__(self) -> None:
        install_ipv4_resolver()
        self.api_key = require_env("OKX_API_KEY")
        self.secret_key = require_env("OKX_SECRET_KEY")
        self.passphrase = require_env("OKX_PASSPHRASE")

    def sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        prehash = f"{timestamp}{method.upper()}{path}{body}"
        digest = hmac.new(self.secret_key.encode(), prehash.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def request(self, method: str, path: str, body_obj: dict | None = None, *, private: bool = True) -> dict:
        method = method.upper()
        body = "" if body_obj is None else json.dumps(body_obj, separators=(",", ":"))
        timestamp = utc_ts()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if SIMULATED_TRADING:
            headers["x-simulated-trading"] = "1"
        if private:
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": self.sign(timestamp, method, path, body),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
        data = None if method == "GET" else body.encode()
        started = time.time()
        req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
                return {
                    "http_status": resp.status,
                    "elapsed_ms": round((time.time() - started) * 1000),
                    "payload": payload,
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw[:1000]}
            return {
                "http_status": exc.code,
                "elapsed_ms": round((time.time() - started) * 1000),
                "payload": payload,
            }

    def get(self, path: str, *, private: bool = True) -> dict:
        return self.request("GET", path, private=private)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)


def okx_data(response: dict) -> list[dict]:
    payload = response.get("payload") or {}
    if payload.get("code") != "0":
        raise RuntimeError(json.dumps(payload, ensure_ascii=False))
    return payload.get("data") or []


def decimal_floor(value: float | Decimal, step: str) -> str:
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    if d_step <= 0:
        return str(d_value)
    units = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return format(units * d_step, "f")


def instrument_map(client: OkxClient, inst_ids: list[str]) -> dict[str, dict]:
    data = okx_data(client.get("/api/v5/account/instruments?instType=SWAP"))
    wanted = set(inst_ids)
    return {row.get("instId"): row for row in data if row.get("instId") in wanted}


def positions_map(client: OkxClient) -> dict[str, dict]:
    data = okx_data(client.get("/api/v5/account/positions?instType=SWAP"))
    return {row.get("instId"): row for row in data if row.get("instId")}


def ticker_last(client: OkxClient, inst_id: str) -> float | None:
    data = okx_data(client.get(f"/api/v5/market/ticker?instId={urllib.parse.quote(inst_id)}", private=False))
    if not data:
        return None
    last = data[0].get("last")
    return float(last) if last not in (None, "") else None


def signed_position_contracts(position: dict | None) -> float:
    if not position:
        return 0.0
    pos = float(position.get("pos") or 0.0)
    pos_side = (position.get("posSide") or "net").lower()
    if pos_side == "short":
        return -abs(pos)
    if pos_side == "long":
        return abs(pos)
    # In OKX net_mode, pos may carry the sign.
    return pos


def position_side_from_signed(signed_pos: float) -> str:
    if signed_pos > 0:
        return "long"
    if signed_pos < 0:
        return "short"
    return "flat"


def account_pos_mode(client: OkxClient) -> str:
    data = okx_data(client.get("/api/v5/account/config"))
    if not data:
        return "net_mode"
    return str(data[0].get("posMode") or "net_mode")


def desired_td_mode(readiness: dict) -> str:
    return str(readiness.get("td_mode") or readiness.get("margin_mode") or "cross").lower()


def desired_leverage(readiness: dict) -> str | None:
    value = readiness.get("expected_leverage")
    if value in (None, ""):
        value = readiness.get("asset_leverage")
    if value in (None, ""):
        return None
    try:
        leverage = Decimal(str(value))
    except Exception:
        return None
    if leverage <= 0:
        return None
    return format(leverage.normalize(), "f")


def ensure_exchange_settings(client: OkxClient, readiness: dict) -> dict:
    """Set the OKX instrument leverage required by the strategy config."""
    inst_id = readiness.get("okx_inst_id")
    target_leverage = desired_leverage(readiness)
    td_mode = desired_td_mode(readiness)
    report = {
        "enforce_leverage": ENFORCE_LEVERAGE,
        "instId": inst_id,
        "target_leverage": target_leverage,
        "tdMode": td_mode,
        "changed": False,
        "ok": True,
        "response": None,
    }
    if not ENFORCE_LEVERAGE or not inst_id or not target_leverage:
        report["skipped_reason"] = "enforcement_disabled_or_missing_target"
        return report
    response = client.post(
        "/api/v5/account/set-leverage",
        {"instId": inst_id, "lever": target_leverage, "mgnMode": td_mode},
    )
    payload = response.get("payload") or {}
    report["response"] = response
    report["ok"] = payload.get("code") == "0"
    report["changed"] = report["ok"]
    if not report["ok"]:
        report["error"] = payload
    return report


def contracts_for_notional(notional_usd: float, price: float, instrument: dict) -> str:
    ct_val = float(instrument.get("ctVal") or 1.0)
    lot_sz = instrument.get("lotSz") or "1"
    raw_contracts = float(notional_usd or 0.0) / (float(price or 0.0) * ct_val)
    return decimal_floor(max(0.0, raw_contracts), lot_sz)


def order_side_for_action(action: str) -> str | None:
    if action in {"OPEN_LONG", "CLOSE_SHORT"}:
        return "buy"
    if action in {"OPEN_SHORT", "CLOSE_LONG"}:
        return "sell"
    return None


def pos_side_for_action(action: str, pos_mode: str) -> str | None:
    if pos_mode != "long_short_mode":
        return None
    if action in {"OPEN_LONG", "CLOSE_LONG"}:
        return "long"
    if action in {"OPEN_SHORT", "CLOSE_SHORT"}:
        return "short"
    return None


def close_position_side_for_action(action: str) -> str | None:
    if action == "CLOSE_LONG":
        return "long"
    if action == "CLOSE_SHORT":
        return "short"
    return None


def open_position_side_for_action(action: str) -> str | None:
    if action == "OPEN_LONG":
        return "long"
    if action == "OPEN_SHORT":
        return "short"
    return None


def short_client_order_id(base: str, index: int) -> str:
    digest = hashlib.sha1(f"{base}:{index}:{time.time_ns()}".encode()).hexdigest()[:20]
    return f"mxc{index}{digest}"[:32]


def plan_orders(client: OkxClient, readiness: dict) -> dict:
    inst_id = readiness.get("okx_inst_id")
    if not inst_id:
        raise RuntimeError("execution_readiness missing okx_inst_id")
    instruments = instrument_map(client, [inst_id])
    instrument = instruments.get(inst_id)
    if not instrument:
        raise RuntimeError(f"instrument not available in OKX account: {inst_id}")
    positions = positions_map(client)
    position = positions.get(inst_id)
    pos_mode = account_pos_mode(client)
    signed_pos = signed_position_contracts(position)
    target_td_mode = desired_td_mode(readiness)
    current_td_mode = str((position or {}).get("mgnMode") or target_td_mode or "cross").lower()
    exec_intent = readiness.get("execution_intent") or {}
    actions = list(exec_intent.get("actions") or [])
    signal_price = readiness.get("signal_price")
    last_price = ticker_last(client, inst_id)
    reference_price = float(last_price or signal_price or exec_intent.get("paper_execution_price") or 0.0)
    desired_notional = float(exec_intent.get("planned_notional_usd") or 0.0)
    desired_contracts = contracts_for_notional(desired_notional, reference_price, instrument)
    close_contracts = decimal_floor(abs(signed_pos), instrument.get("lotSz") or "1")
    orders = []
    for action in actions:
        side = order_side_for_action(action)
        if not side:
            continue
        close_side = close_position_side_for_action(action)
        open_side = open_position_side_for_action(action)
        reduce_only = close_side is not None
        actual_side = position_side_from_signed(signed_pos)
        size = close_contracts if reduce_only else desired_contracts
        if close_side:
            if actual_side != close_side:
                orders.append(
                    {
                        "action": action,
                        "skipped": True,
                        "reason": f"no_{close_side}_position_to_close",
                        "expected_position_side": close_side,
                        "actual_position_side": actual_side,
                    }
                )
                continue
            orders.append(
                {
                    "action": action,
                    "instId": inst_id,
                    "tdMode": current_td_mode,
                    "posSide": pos_side_for_action(action, pos_mode),
                    "method": "close_position",
                    "expected_position_side": close_side,
                    "submit": bool(SUBMIT_ORDERS and readiness.get("live_execution_enabled")),
                }
            )
            continue
        if open_side and actual_side == open_side:
            orders.append(
                {
                    "action": action,
                    "skipped": True,
                    "reason": f"already_{open_side}_no_pyramid",
                    "expected_position_side": open_side,
                    "actual_position_side": actual_side,
                }
            )
            continue
        if Decimal(str(size)) <= 0:
            orders.append(
                {
                    "action": action,
                    "skipped": True,
                    "reason": "zero_size",
                    "side": side,
                    "reduceOnly": reduce_only,
                }
            )
            continue
        orders.append(
            {
                "action": action,
                "instId": inst_id,
                "tdMode": current_td_mode if reduce_only else target_td_mode,
                "side": side,
                "posSide": pos_side_for_action(action, pos_mode),
                "method": "place_order",
                "expected_position_side": open_side,
                "ordType": "market",
                "sz": size,
                "reduceOnly": reduce_only if pos_mode != "long_short_mode" else None,
                "submit": bool(SUBMIT_ORDERS and readiness.get("live_execution_enabled")),
            }
        )
    return {
        "generated_at": utc_ts(),
        "mode": "submit_enabled" if SUBMIT_ORDERS and readiness.get("live_execution_enabled") else "dry_run_no_order",
        "simulated_trading": SIMULATED_TRADING,
        "forced_ipv4": FORCE_IPV4,
        "instId": inst_id,
        "signal_side": readiness.get("signal_side"),
        "signal_price": signal_price,
        "last_price": last_price,
        "reference_price": reference_price,
        "instrument": {
            "lotSz": instrument.get("lotSz"),
            "minSz": instrument.get("minSz"),
            "ctVal": instrument.get("ctVal"),
            "ctValCcy": instrument.get("ctValCcy"),
            "lever": instrument.get("lever"),
        },
        "expected_settings": {
            "tdMode": target_td_mode,
            "leverage": desired_leverage(readiness),
            "close_tdMode": current_td_mode,
            "open_tdMode": target_td_mode,
        },
        "current_position": {
            "posMode": pos_mode,
            "signed_contracts": signed_pos,
            "raw": {
                key: position.get(key)
                for key in ("instId", "posSide", "pos", "avgPx", "notionalUsd", "upl", "lever", "mgnMode")
                if position
            },
        },
        "execution_policy": readiness.get("execution_policy"),
        "shadow_policy": readiness.get("shadow_policy"),
        "execution_intent": exec_intent,
        "shadow_comparison": readiness.get("shadow_comparison"),
        "desired_contracts": desired_contracts,
        "orders": orders,
    }


def compact_order_body(order: dict, client_order_id: str) -> dict:
    body = {
        "instId": order["instId"],
        "tdMode": order.get("tdMode") or "cross",
        "side": order["side"],
        "ordType": order.get("ordType") or "market",
        "sz": str(order["sz"]),
        "clOrdId": client_order_id,
    }
    if order.get("posSide"):
        body["posSide"] = order["posSide"]
    if order.get("reduceOnly") is not None:
        body["reduceOnly"] = "true" if order.get("reduceOnly") else "false"
    return body


def compact_close_position_body(order: dict) -> dict:
    body = {
        "instId": order["instId"],
        "mgnMode": order.get("tdMode") or "cross",
    }
    if order.get("posSide"):
        body["posSide"] = order["posSide"]
    return body


def order_ok(payload: dict) -> bool:
    if (payload or {}).get("code") != "0":
        return False
    data = payload.get("data") or []
    return bool(data) and all(str(row.get("sCode", "0")) == "0" for row in data)


def fetch_order(client: OkxClient, inst_id: str, ord_id: str | None, cl_ord_id: str | None) -> dict | None:
    if not ord_id and not cl_ord_id:
        return None
    query = {"instId": inst_id}
    if ord_id:
        query["ordId"] = ord_id
    elif cl_ord_id:
        query["clOrdId"] = cl_ord_id
    path = "/api/v5/trade/order?" + urllib.parse.urlencode(query)
    try:
        data = okx_data(client.get(path))
        return data[0] if data else None
    except Exception:
        return None


def current_position_side(client: OkxClient, inst_id: str) -> tuple[str, float, dict | None]:
    position = positions_map(client).get(inst_id)
    signed_pos = signed_position_contracts(position)
    return position_side_from_signed(signed_pos), signed_pos, position


def close_position_verified(client: OkxClient, inst_id: str, closed_side: str) -> tuple[bool, dict]:
    side, signed_pos, position = current_position_side(client, inst_id)
    return side != closed_side, {
        "actual_position_side": side,
        "signed_contracts": signed_pos,
        "raw": position,
    }


def execute_orders(client: OkxClient, readiness: dict) -> dict:
    exchange_settings = ensure_exchange_settings(client, readiness)
    if not exchange_settings.get("ok"):
        return {
            "generated_at": utc_ts(),
            "mode": "submit_blocked_exchange_settings",
            "exchange_settings": exchange_settings,
            "executed_orders": [],
            "okx_fill_summary": {
                "status": "blocked_exchange_settings",
                "order_id": None,
                "client_order_id": (readiness.get("execution_intent") or {}).get("client_order_id"),
                "avg_fill_price": None,
                "filled_size": None,
                "fee_usd": None,
                "slippage_pct": None,
                "position_after_order": positions_map(client).get(readiness.get("okx_inst_id")),
            },
        }
    plan = plan_orders(client, readiness)
    base_id = ((readiness.get("execution_intent") or {}).get("client_order_id") or "mxc-order")
    executed = []
    for index, order in enumerate(plan.get("orders") or [], start=1):
        result = {"action": order.get("action"), "planned": order}
        if order.get("skipped"):
            result.update({"submitted": False, "status": "skipped", "reason": order.get("reason")})
            executed.append(result)
            continue
        if not order.get("submit"):
            result.update({"submitted": False, "status": "dry_run"})
            executed.append(result)
            continue
        cl_ord_id = short_client_order_id(base_id, index)
        method = order.get("method") or "place_order"
        if method == "close_position":
            body = compact_close_position_body(order)
            started = time.time()
            response = client.post("/api/v5/trade/close-position", body)
            elapsed_ms = round((time.time() - started) * 1000)
            payload = response.get("payload") or {}
            ok = order_ok(payload)
            time.sleep(0.5)
            verified, position_after = close_position_verified(client, order.get("instId"), order.get("expected_position_side"))
            status = "filled" if ok and verified else ("close_not_verified" if ok else "rejected")
            result.update(
                {
                    "submitted": True,
                    "status": status,
                    "request": body,
                    "response": response,
                    "elapsed_ms": elapsed_ms,
                    "ordId": None,
                    "clOrdId": cl_ord_id,
                    "order_state": None,
                    "position_after_close": position_after,
                }
            )
            executed.append(result)
            continue

        open_side = order.get("expected_position_side")
        actual_side, signed_pos, position = current_position_side(client, order.get("instId"))
        if open_side and actual_side == open_side:
            result.update(
                {
                    "submitted": False,
                    "status": "skipped",
                    "reason": f"already_{open_side}_no_pyramid",
                    "position_before_open": {"actual_position_side": actual_side, "signed_contracts": signed_pos, "raw": position},
                }
            )
            executed.append(result)
            continue
        if open_side and actual_side != "flat":
            result.update(
                {
                    "submitted": False,
                    "status": "blocked",
                    "reason": f"opposite_position_still_open_{actual_side}",
                    "position_before_open": {"actual_position_side": actual_side, "signed_contracts": signed_pos, "raw": position},
                }
            )
            executed.append(result)
            continue

        body = compact_order_body(order, cl_ord_id)
        started = time.time()
        response = client.post("/api/v5/trade/order", body)
        elapsed_ms = round((time.time() - started) * 1000)
        payload = response.get("payload") or {}
        data = payload.get("data") or []
        ord_id = data[0].get("ordId") if data else None
        ok = order_ok(payload)
        time.sleep(0.25)
        order_state = fetch_order(client, order.get("instId"), ord_id, cl_ord_id) if ok else None
        result.update(
            {
                "submitted": True,
                "status": "submitted" if ok else "rejected",
                "request": body,
                "response": response,
                "elapsed_ms": elapsed_ms,
                "ordId": ord_id,
                "clOrdId": cl_ord_id,
                "order_state": order_state,
            }
        )
        executed.append(result)
    fill_rows = [row.get("order_state") for row in executed if row.get("order_state")]
    fill_price = None
    filled_size = None
    fee_usd = 0.0
    for row in fill_rows:
        if row.get("avgPx") not in (None, "", "0"):
            fill_price = float(row.get("avgPx"))
        if row.get("accFillSz") not in (None, ""):
            filled_size = row.get("accFillSz")
        if row.get("fee") not in (None, ""):
            try:
                fee_usd += abs(float(row.get("fee")))
            except Exception:
                pass
    submitted = [row for row in executed if row.get("submitted")]
    bad_statuses = {"rejected", "blocked", "close_not_verified"}
    bad = [row for row in executed if row.get("status") in bad_statuses]
    if bad:
        status = str(bad[0].get("status") or "blocked")
    elif submitted and all(
        row.get("status") == "filled" or (row.get("order_state") or {}).get("state") in {"filled", "partially_filled"}
        for row in submitted
    ):
        status = "filled"
    elif submitted:
        status = "submitted"
    else:
        status = "dry_run"
    signal_price = readiness.get("signal_price")
    slippage_pct = None
    if fill_price is not None and signal_price:
        slippage_pct = (float(fill_price) / float(signal_price) - 1.0) * 100.0
    return {
        "generated_at": utc_ts(),
        "mode": "submit_enabled" if SUBMIT_ORDERS and readiness.get("live_execution_enabled") else "dry_run_no_order",
        "exchange_settings": exchange_settings,
        "plan": plan,
        "executed_orders": executed,
        "okx_fill_summary": {
            "status": status,
            "order_id": next((row.get("ordId") for row in reversed(executed) if row.get("ordId")), None),
            "client_order_id": next((row.get("clOrdId") for row in reversed(executed) if row.get("clOrdId")), (readiness.get("execution_intent") or {}).get("client_order_id")),
            "avg_fill_price": fill_price,
            "filled_size": filled_size,
            "fee_usd": round(fee_usd, 8) if fee_usd else None,
            "slippage_pct": round(slippage_pct, 6) if slippage_pct is not None else None,
            "position_after_order": positions_map(client).get(readiness.get("okx_inst_id")),
        },
    }


def account_snapshot(client: OkxClient) -> dict:
    config = okx_data(client.get("/api/v5/account/config"))
    balance = okx_data(client.get("/api/v5/account/balance"))
    positions = okx_data(client.get("/api/v5/account/positions?instType=SWAP"))
    instruments = okx_data(client.get("/api/v5/account/instruments?instType=SWAP"))
    ids = {row.get("instId") for row in instruments}
    cfg = config[0] if config else {}
    bal = balance[0] if balance else {}
    return {
        "ok": True,
        "generated_at": utc_ts(),
        "simulated_trading": SIMULATED_TRADING,
        "forced_ipv4": FORCE_IPV4,
        "account": {
            "acctLv": cfg.get("acctLv"),
            "posMode": cfg.get("posMode"),
            "level": cfg.get("level"),
            "totalEq": bal.get("totalEq"),
            "details_count": len(bal.get("details") or []),
            "currencies": [row.get("ccy") for row in (bal.get("details") or [])],
        },
        "positions": [
            {key: row.get(key) for key in ("instId", "posSide", "pos", "avgPx", "notionalUsd", "upl", "realizedPnl", "lever", "mgnMode", "imr", "mmr")}
            for row in positions
        ],
        "targets_available": {
            "XRP-USDT-SWAP": "XRP-USDT-SWAP" in ids,
            "SOL-USDT-SWAP": "SOL-USDT-SWAP" in ids,
            "ETH-USDT-SWAP": "ETH-USDT-SWAP" in ids,
            "BTC-USDT-SWAP": "BTC-USDT-SWAP" in ids,
        },
    }


def order_history_snapshot(client: OkxClient, inst_ids: list[str] | None = None, limit: int = 100) -> dict:
    """Read recent filled SWAP order history for dashboard reconciliation."""
    inst_ids = inst_ids or ["XRP-USDT-SWAP", "SOL-USDT-SWAP", "ETH-USDT-SWAP", "BTC-USDT-SWAP"]
    rows: list[dict] = []
    for inst_id in inst_ids:
        query = {
            "instType": "SWAP",
            "instId": inst_id,
            "state": "filled",
            "limit": str(limit),
        }
        path = "/api/v5/trade/orders-history?" + urllib.parse.urlencode(query)
        data = okx_data(client.get(path))
        for row in data:
            rows.append(
                {
                    key: row.get(key)
                    for key in (
                        "instId",
                        "ordId",
                        "clOrdId",
                        "side",
                        "posSide",
                        "tdMode",
                        "avgPx",
                        "accFillSz",
                        "fillSz",
                        "sz",
                        "fee",
                        "feeCcy",
                        "pnl",
                        "reduceOnly",
                        "state",
                        "lever",
                        "cTime",
                        "uTime",
                    )
                }
            )
    rows.sort(key=lambda row: str(row.get("uTime") or row.get("cTime") or ""), reverse=True)
    return {
        "ok": True,
        "generated_at": utc_ts(),
        "simulated_trading": SIMULATED_TRADING,
        "rows": rows,
    }


def load_readiness(path: str | None) -> dict:
    if path:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        obj = json.loads(sys.stdin.read())
    if "execution_readiness" in obj:
        return obj["execution_readiness"]
    return obj


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX demo execution planner")
    parser.add_argument("command", choices=["health", "plan", "execute", "orders-history"], help="health reads account state; plan builds order intents; execute can submit demo orders when enabled")
    parser.add_argument("--readiness-file", help="JSON file containing execution_readiness")
    parser.add_argument("--inst-id", action="append", help="Optional OKX instrument id for orders-history. Can be repeated.")
    parser.add_argument("--limit", type=int, default=100, help="Per-instrument order-history limit.")
    args = parser.parse_args()
    client = OkxClient()
    if args.command == "health":
        print(json.dumps(account_snapshot(client), ensure_ascii=False, indent=2))
        return 0
    if args.command == "orders-history":
        print(json.dumps(order_history_snapshot(client, args.inst_id, args.limit), ensure_ascii=False, indent=2))
        return 0
    readiness = load_readiness(args.readiness_file)
    if args.command == "plan":
        print(json.dumps(plan_orders(client, readiness), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(execute_orders(client, readiness), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
