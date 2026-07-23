from __future__ import annotations

from security.credentials import (
    redact_secrets,
    resolve_exchange_credentials,
    resolve_executor_env,
)


def test_resolve_executor_env_scopes_credentials_to_selected_exchange():
    source = {
        "PATH": "/usr/bin",
        "OKX_DEMO_API_KEY": "okx-demo-key",
        "OKX_DEMO_SECRET_KEY": "okx-demo-secret",
        "OKX_DEMO_PASSPHRASE": "okx-demo-pass",
        "KUCOIN_PAPER_API_KEY": "kucoin-key",
        "UNRELATED_SECRET": "should-not-leak",
    }
    env = resolve_executor_env("okx_demo", source, {"HERMX_LIVE_TRADING": "false"})

    assert env["PATH"] == "/usr/bin"
    assert env["OKX_API_KEY"] == "okx-demo-key"
    assert env["OKX_SECRET_KEY"] == "okx-demo-secret"
    assert env["OKX_PASSPHRASE"] == "okx-demo-pass"
    assert env["HERMX_LIVE_TRADING"] == "false"
    assert "KUCOIN_PAPER_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env


def test_resolve_okx_demo_prefers_demo_keys():
    env = {
        "OKX_API_KEY": "okx-live-key",
        "OKX_SECRET_KEY": "okx-live-secret",
        "OKX_PASSPHRASE": "okx-live-pass",
        "OKX_DEMO_API_KEY": "okx-demo-key",
        "OKX_DEMO_SECRET_KEY": "okx-demo-secret",
        "OKX_DEMO_PASSPHRASE": "okx-demo-pass",
    }
    creds = resolve_exchange_credentials("okx", env, mode="demo")
    assert creds["OKX_API_KEY"] == "okx-demo-key"
    assert creds["OKX_SECRET_KEY"] == "okx-demo-secret"
    assert creds["OKX_PASSPHRASE"] == "okx-demo-pass"


def test_resolve_okx_live_prefers_plain_keys():
    env = {
        "OKX_API_KEY": "okx-live-key",
        "OKX_SECRET_KEY": "okx-live-secret",
        "OKX_PASSPHRASE": "okx-live-pass",
        "OKX_DEMO_API_KEY": "okx-demo-key",
        "OKX_DEMO_SECRET_KEY": "okx-demo-secret",
        "OKX_DEMO_PASSPHRASE": "okx-demo-pass",
    }
    creds = resolve_exchange_credentials("okx", env, mode="live")
    assert creds["OKX_API_KEY"] == "okx-live-key"
    assert creds["OKX_SECRET_KEY"] == "okx-live-secret"
    assert creds["OKX_PASSPHRASE"] == "okx-live-pass"


def test_resolve_okx_default_mode_is_demo():
    env = {"OKX_API_KEY": "okx-live-key", "OKX_DEMO_API_KEY": "okx-demo-key"}
    creds = resolve_exchange_credentials("okx", env)
    assert creds["OKX_API_KEY"] == "okx-demo-key"


def test_resolve_kucoin_demo_vs_live():
    env = {
        "KUCOIN_API_KEY": "kucoin-live-key",
        "KUCOIN_SECRET": "kucoin-live-secret",
        "KUCOIN_PASSPHRASE": "kucoin-live-pass",
        "KUCOIN_PAPER_API_KEY": "kucoin-paper-key",
        "KUCOIN_PAPER_SECRET": "kucoin-paper-secret",
        "KUCOIN_PAPER_PASSPHRASE": "kucoin-paper-pass",
    }
    demo = resolve_exchange_credentials("kucoin", env, mode="demo")
    assert demo["KUCOIN_API_KEY"] == "kucoin-paper-key"
    assert demo["KUCOIN_SECRET"] == "kucoin-paper-secret"
    assert demo["KUCOIN_PASSPHRASE"] == "kucoin-paper-pass"

    live = resolve_exchange_credentials("kucoin", env, mode="live")
    assert live["KUCOIN_API_KEY"] == "kucoin-live-key"
    assert live["KUCOIN_SECRET"] == "kucoin-live-secret"
    assert live["KUCOIN_PASSPHRASE"] == "kucoin-live-pass"


def test_resolve_bybit_demo_vs_live():
    env = {
        "BYBIT_API_KEY": "bybit-live-key",
        "BYBIT_SECRET_KEY": "bybit-live-secret",
        "BYBIT_TESTNET_API_KEY": "bybit-testnet-key",
        "BYBIT_TESTNET_SECRET_KEY": "bybit-testnet-secret",
    }
    demo = resolve_exchange_credentials("bybit", env, mode="demo")
    assert demo["BYBIT_API_KEY"] == "bybit-testnet-key"
    assert demo["BYBIT_SECRET_KEY"] == "bybit-testnet-secret"

    live = resolve_exchange_credentials("bybit", env, mode="live")
    assert live["BYBIT_API_KEY"] == "bybit-live-key"
    assert live["BYBIT_SECRET_KEY"] == "bybit-live-secret"


def test_resolve_bitfinex_demo_vs_live():
    env = {
        "BITFINEX_API_KEY": "bitfinex-live-key",
        "BITFINEX_SECRET_KEY": "bitfinex-live-secret",
        "BITFINEX_PAPER_API_KEY": "bitfinex-paper-key",
        "BITFINEX_PAPER_SECRET_KEY": "bitfinex-paper-secret",
    }
    demo = resolve_exchange_credentials("bitfinex", env, mode="demo")
    assert demo["BITFINEX_API_KEY"] == "bitfinex-paper-key"
    assert demo["BITFINEX_SECRET_KEY"] == "bitfinex-paper-secret"

    live = resolve_exchange_credentials("bitfinex", env, mode="live")
    assert live["BITFINEX_API_KEY"] == "bitfinex-live-key"
    assert live["BITFINEX_SECRET_KEY"] == "bitfinex-live-secret"


def test_resolve_bitfinex2_alias_maps_to_same_keys():
    env = {
        "BITFINEX_API_KEY": "bitfinex-live-key",
        "BITFINEX_SECRET_KEY": "bitfinex-live-secret",
    }
    creds = resolve_exchange_credentials("bitfinex2", env, mode="live")
    assert creds["BITFINEX_API_KEY"] == "bitfinex-live-key"
    assert creds["BITFINEX_SECRET_KEY"] == "bitfinex-live-secret"


def test_resolve_hyperliquid_live_returns_pair_when_both_present():
    env = {
        "HYPERLIQUID_WALLET_ADDRESS": "0xlive",
        "HYPERLIQUID_PRIVATE_KEY": "live-pk",
    }
    creds = resolve_exchange_credentials("hyperliquid", env, mode="live")
    assert creds["HYPERLIQUID_WALLET_ADDRESS"] == "0xlive"
    assert creds["HYPERLIQUID_PRIVATE_KEY"] == "live-pk"


def test_resolve_hyperliquid_live_fails_closed_on_partial():
    env = {"HYPERLIQUID_WALLET_ADDRESS": "0xlive"}  # no private key
    creds = resolve_exchange_credentials("hyperliquid", env, mode="live")
    assert creds == {}


def test_resolve_hyperliquid_demo_prefers_testnet_pair():
    env = {
        "HYPERLIQUID_WALLET_ADDRESS": "0xlive",
        "HYPERLIQUID_PRIVATE_KEY": "live-pk",
        "HYPERLIQUID_TESTNET_WALLET_ADDRESS": "0xtestnet",
        "HYPERLIQUID_TESTNET_PRIVATE_KEY": "testnet-pk",
    }
    creds = resolve_exchange_credentials("hyperliquid", env, mode="demo")
    assert creds["HYPERLIQUID_WALLET_ADDRESS"] == "0xtestnet"
    assert creds["HYPERLIQUID_PRIVATE_KEY"] == "testnet-pk"


def test_redact_secrets_scrubs_known_values(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "real-okx-key")
    text = 'stderr: {"OKX_API_KEY":"real-okx-key","token":"abc"}'
    redacted = redact_secrets(text)
    assert "real-okx-key" not in redacted
    assert "<redacted>" in redacted


def test_redact_secrets_scrubs_bitfinex_values(monkeypatch):
    monkeypatch.setenv("BITFINEX_API_KEY", "real-bfx-key")
    monkeypatch.setenv("BITFINEX_SECRET_KEY", "real-bfx-secret")
    redacted = redact_secrets("auth failed for real-bfx-key with real-bfx-secret")
    assert "real-bfx-key" not in redacted
    assert "real-bfx-secret" not in redacted
    assert "<redacted>" in redacted
