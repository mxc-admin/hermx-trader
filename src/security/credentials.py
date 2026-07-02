#!/usr/bin/env python3
from __future__ import annotations

import os
import re

_SAFE_PASSTHROUGH_ENV = (
    "PATH",
    "PYTHONPATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)

_SECRET_VALUE_RE = re.compile(
    r'((?:api[_-]?key|secret[_-]?key|passphrase|token|authorization)"?\s*[:=]\s*")([^"]+)(")',
    re.IGNORECASE,
)


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no"}


def _pick_first(source_env: dict, *names: str) -> str | None:
    for name in names:
        value = source_env.get(name)
        if value is not None and str(value).strip() != "":
            return str(value)
    return None


def resolve_exchange_credentials(exchange: str, source_env: dict | None = None, mode: str = "demo") -> dict:
    """Return ONLY the selected exchange credential vars.

    Inputs support both legacy and namespaced env names; outputs are normalized to
    the env names expected by the active adapter process.

    `mode` controls the `_pick_first` preference order:
    - "demo" (default): prefer demo/testnet/paper/sandbox keys, plain keys as fallback
      (backward-compatible default).
    - "live": prefer plain keys, demo/testnet keys as fallback.
    """
    env = source_env or os.environ
    key = str(exchange or "").strip().lower()
    live = str(mode or "").strip().lower() == "live"
    out: dict[str, str] = {}

    if key in {"okx", "okx_api", "okx_sandbox", "okx_demo"}:
        if live:
            api_key = _pick_first(env, "OKX_API_KEY", "OKX_DEMO_API_KEY")
            secret_key = _pick_first(env, "OKX_SECRET_KEY", "OKX_DEMO_SECRET_KEY")
            passphrase = _pick_first(env, "OKX_PASSPHRASE", "OKX_DEMO_PASSPHRASE")
        else:
            api_key = _pick_first(env, "OKX_DEMO_API_KEY", "OKX_API_KEY")
            secret_key = _pick_first(env, "OKX_DEMO_SECRET_KEY", "OKX_SECRET_KEY")
            passphrase = _pick_first(env, "OKX_DEMO_PASSPHRASE", "OKX_PASSPHRASE")
        if api_key:
            out["OKX_API_KEY"] = api_key
        if secret_key:
            out["OKX_SECRET_KEY"] = secret_key
        if passphrase:
            out["OKX_PASSPHRASE"] = passphrase
        return out

    if key in {"kucoin", "kucoin_paper"}:
        if live:
            api_key = _pick_first(env, "KUCOIN_API_KEY", "KUCOIN_PAPER_API_KEY")
            secret_key = _pick_first(env, "KUCOIN_SECRET", "KUCOIN_SECRET_KEY", "KUCOIN_PAPER_SECRET")
            passphrase = _pick_first(env, "KUCOIN_PASSPHRASE", "KUCOIN_PAPER_PASSPHRASE")
        else:
            api_key = _pick_first(env, "KUCOIN_PAPER_API_KEY", "KUCOIN_API_KEY")
            secret_key = _pick_first(env, "KUCOIN_PAPER_SECRET", "KUCOIN_SECRET", "KUCOIN_SECRET_KEY")
            passphrase = _pick_first(env, "KUCOIN_PAPER_PASSPHRASE", "KUCOIN_PASSPHRASE")
        if api_key:
            out["KUCOIN_API_KEY"] = api_key
        if secret_key:
            out["KUCOIN_SECRET"] = secret_key
        if passphrase:
            out["KUCOIN_PASSPHRASE"] = passphrase
        return out

    if key in {"bybit", "bybit_testnet"}:
        if live:
            api_key = _pick_first(env, "BYBIT_API_KEY", "BYBIT_TESTNET_API_KEY")
            secret_key = _pick_first(env, "BYBIT_SECRET_KEY", "BYBIT_TESTNET_SECRET_KEY")
        else:
            api_key = _pick_first(env, "BYBIT_TESTNET_API_KEY", "BYBIT_API_KEY")
            secret_key = _pick_first(env, "BYBIT_TESTNET_SECRET_KEY", "BYBIT_SECRET_KEY")
        if api_key:
            out["BYBIT_API_KEY"] = api_key
        if secret_key:
            out["BYBIT_SECRET_KEY"] = secret_key
        return out

    if key in {"binance", "binance_testnet"}:
        if live:
            api_key = _pick_first(env, "BINANCE_API_KEY", "BINANCE_TESTNET_API_KEY")
            secret_key = _pick_first(env, "BINANCE_SECRET_KEY", "BINANCE_TESTNET_SECRET_KEY")
        else:
            api_key = _pick_first(env, "BINANCE_TESTNET_API_KEY", "BINANCE_API_KEY")
            secret_key = _pick_first(env, "BINANCE_TESTNET_SECRET_KEY", "BINANCE_SECRET_KEY")
        if api_key:
            out["BINANCE_API_KEY"] = api_key
        if secret_key:
            out["BINANCE_SECRET_KEY"] = secret_key
        return out

    if key in {"bitget", "bitget_demo"}:
        if live:
            api_key = _pick_first(env, "BITGET_API_KEY", "BITGET_DEMO_API_KEY")
            secret_key = _pick_first(env, "BITGET_SECRET_KEY", "BITGET_DEMO_SECRET_KEY")
            passphrase = _pick_first(env, "BITGET_PASSPHRASE", "BITGET_DEMO_PASSPHRASE")
        else:
            api_key = _pick_first(env, "BITGET_DEMO_API_KEY", "BITGET_API_KEY")
            secret_key = _pick_first(env, "BITGET_DEMO_SECRET_KEY", "BITGET_SECRET_KEY")
            passphrase = _pick_first(env, "BITGET_DEMO_PASSPHRASE", "BITGET_PASSPHRASE")
        if api_key:
            out["BITGET_API_KEY"] = api_key
        if secret_key:
            out["BITGET_SECRET_KEY"] = secret_key
        if passphrase:
            out["BITGET_PASSPHRASE"] = passphrase
        return out

    if key in {"gate", "gateio", "gate_io", "gate_testnet"}:
        if live:
            api_key = _pick_first(env, "GATE_API_KEY", "GATE_TESTNET_API_KEY")
            secret_key = _pick_first(env, "GATE_SECRET_KEY", "GATE_TESTNET_SECRET_KEY")
        else:
            api_key = _pick_first(env, "GATE_TESTNET_API_KEY", "GATE_API_KEY")
            secret_key = _pick_first(env, "GATE_TESTNET_SECRET_KEY", "GATE_SECRET_KEY")
        if api_key:
            out["GATE_API_KEY"] = api_key
        if secret_key:
            out["GATE_SECRET_KEY"] = secret_key
        return out

    if key in {"coinbase", "coinbase_sandbox", "coinbase_advanced"}:
        if live:
            api_key = _pick_first(env, "COINBASE_API_KEY", "COINBASE_SANDBOX_API_KEY")
            secret_key = _pick_first(env, "COINBASE_SECRET_KEY", "COINBASE_SANDBOX_SECRET_KEY")
        else:
            api_key = _pick_first(env, "COINBASE_SANDBOX_API_KEY", "COINBASE_API_KEY")
            secret_key = _pick_first(env, "COINBASE_SANDBOX_SECRET_KEY", "COINBASE_SECRET_KEY")
        if api_key:
            out["COINBASE_API_KEY"] = api_key
        if secret_key:
            out["COINBASE_SECRET_KEY"] = secret_key
        return out

    if key in {"hyperliquid", "hyperliquid_testnet"}:
        # Hyperliquid auth differs from the apiKey/secret/passphrase venues: it is a
        # wallet address + private key (NO passphrase). Namespaced env names mirror
        # the existing <EXCHANGE>_<SANDBOX>_<FIELD> -> <EXCHANGE>_<FIELD> convention
        # (cf. BYBIT_TESTNET_* -> BYBIT_*), with TESTNET as Hyperliquid's sandbox tag.
        # FAIL CLOSED: only return the pair when BOTH are present, so a partial set
        # yields {} (disarmed) and can NEVER borrow another venue's keys.
        if live:
            wallet = _pick_first(env, "HYPERLIQUID_WALLET_ADDRESS", "HYPERLIQUID_TESTNET_WALLET_ADDRESS")
            private_key = _pick_first(env, "HYPERLIQUID_PRIVATE_KEY", "HYPERLIQUID_TESTNET_PRIVATE_KEY")
        else:
            wallet = _pick_first(env, "HYPERLIQUID_TESTNET_WALLET_ADDRESS", "HYPERLIQUID_WALLET_ADDRESS")
            private_key = _pick_first(env, "HYPERLIQUID_TESTNET_PRIVATE_KEY", "HYPERLIQUID_PRIVATE_KEY")
        if wallet and private_key:
            out["HYPERLIQUID_WALLET_ADDRESS"] = wallet
            out["HYPERLIQUID_PRIVATE_KEY"] = private_key
        return out

    return out


def resolve_executor_env(exchange: str, source_env: dict | None = None, extra_env: dict | None = None) -> dict:
    """Least-privilege subprocess env: safe runtime vars + selected-exchange credentials."""
    src = source_env or os.environ
    out: dict[str, str] = {}
    for key in _SAFE_PASSTHROUGH_ENV:
        if key in src:
            out[key] = str(src[key])
    out.update(resolve_exchange_credentials(exchange, src))
    if extra_env:
        for key, value in extra_env.items():
            if value is None:
                continue
            out[str(key)] = str(value)
    return out


def redact_secrets(text: str | None) -> str:
    if not text:
        return ""
    redacted = _SECRET_VALUE_RE.sub(r"\1<redacted>\3", str(text))
    for key in (
        "OKX_API_KEY",
        "OKX_SECRET_KEY",
        "OKX_PASSPHRASE",
        "KUCOIN_API_KEY",
        "KUCOIN_SECRET",
        "KUCOIN_PAPER_SECRET",
        "KUCOIN_PASSPHRASE",
        "BYBIT_API_KEY",
        "BYBIT_SECRET_KEY",
        "BYBIT_TESTNET_API_KEY",
        "BYBIT_TESTNET_SECRET_KEY",
        "BINANCE_API_KEY",
        "BINANCE_SECRET_KEY",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_SECRET_KEY",
        "BITGET_API_KEY",
        "BITGET_SECRET_KEY",
        "BITGET_PASSPHRASE",
        "BITGET_DEMO_API_KEY",
        "BITGET_DEMO_SECRET_KEY",
        "BITGET_DEMO_PASSPHRASE",
        "GATE_API_KEY",
        "GATE_SECRET_KEY",
        "GATE_TESTNET_API_KEY",
        "GATE_TESTNET_SECRET_KEY",
        "COINBASE_API_KEY",
        "COINBASE_SECRET_KEY",
        "COINBASE_SANDBOX_API_KEY",
        "COINBASE_SANDBOX_SECRET_KEY",
        "HYPERLIQUID_WALLET_ADDRESS",
        "HYPERLIQUID_PRIVATE_KEY",
        "HYPERLIQUID_TESTNET_WALLET_ADDRESS",
        "HYPERLIQUID_TESTNET_PRIVATE_KEY",
    ):
        value = os.environ.get(key)
        if _truthy(value):
            redacted = redacted.replace(str(value), "<redacted>")
    return redacted
