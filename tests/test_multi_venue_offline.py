"""OFFLINE proofs for Multi-Exchange Enablement (REFACTOR_PLAN.md Phase 6, tasks 5-7).

Always run, no network. These lock down the money-safety invariants of the new
venues: fail-closed namespaced credentials (never borrow another venue's keys),
secret redaction, the Hyperliquid wallet/key adapter auth branch, and venue
resolution from the strategy instrument block -- including byte-identical v1/OKX.
"""
from __future__ import annotations

import types

import pytest

import executors.ccxt_adapter as ccxt_adapter
from executors.ccxt_adapter import CcxtExecutor
from executors.factory import ExecutorFactory
from execution.service import resolve_execution_config
from security.credentials import redact_secrets, resolve_exchange_credentials


# --- (a) Hyperliquid credential resolution: only HL keys, fail-closed on partial ---

def test_hyperliquid_credentials_resolve_only_hl_keys():
    source = {
        "HYPERLIQUID_WALLET_ADDRESS": "0xWALLET",
        "HYPERLIQUID_PRIVATE_KEY": "0xPRIVKEY",
        # Other venues present in the env must NEVER leak into the HL result.
        "OKX_API_KEY": "okx-key",
        "OKX_SECRET_KEY": "okx-secret",
        "OKX_PASSPHRASE": "okx-pass",
        "KUCOIN_API_KEY": "kucoin-key",
        "BYBIT_API_KEY": "bybit-key",
    }
    creds = resolve_exchange_credentials("hyperliquid", source)
    assert creds == {
        "HYPERLIQUID_WALLET_ADDRESS": "0xWALLET",
        "HYPERLIQUID_PRIVATE_KEY": "0xPRIVKEY",
    }


def test_hyperliquid_testnet_namespaced_env_preferred():
    source = {
        "HYPERLIQUID_TESTNET_WALLET_ADDRESS": "0xTESTWALLET",
        "HYPERLIQUID_TESTNET_PRIVATE_KEY": "0xTESTPRIV",
        "HYPERLIQUID_WALLET_ADDRESS": "0xLIVEWALLET",
        "HYPERLIQUID_PRIVATE_KEY": "0xLIVEPRIV",
    }
    creds = resolve_exchange_credentials("hyperliquid", source)
    assert creds["HYPERLIQUID_WALLET_ADDRESS"] == "0xTESTWALLET"
    assert creds["HYPERLIQUID_PRIVATE_KEY"] == "0xTESTPRIV"


@pytest.mark.parametrize(
    "source",
    [
        {"HYPERLIQUID_WALLET_ADDRESS": "0xWALLET"},          # missing private key
        {"HYPERLIQUID_PRIVATE_KEY": "0xPRIVKEY"},            # missing wallet
        {"HYPERLIQUID_WALLET_ADDRESS": "0xWALLET", "HYPERLIQUID_PRIVATE_KEY": ""},  # empty pk
        {},                                                  # nothing
    ],
)
def test_hyperliquid_credentials_fail_closed_on_partial(source):
    # A partial/missing set => {} (disarmed). The adapter then builds blank
    # wallet/key kwargs and cannot authenticate -> the venue stays disarmed.
    assert resolve_exchange_credentials("hyperliquid", source) == {}


def test_hyperliquid_never_borrows_other_venue_keys_when_absent():
    # HL creds absent, other venues fully present: HL must resolve to {}.
    source = {
        "OKX_API_KEY": "okx-key",
        "OKX_SECRET_KEY": "okx-secret",
        "OKX_PASSPHRASE": "okx-pass",
        "KUCOIN_API_KEY": "kucoin-key",
        "KUCOIN_SECRET": "kucoin-secret",
        "KUCOIN_PASSPHRASE": "kucoin-pass",
    }
    assert resolve_exchange_credentials("hyperliquid", source) == {}


# --- (b) redact_secrets masks HL keys ---

def test_redact_secrets_masks_hyperliquid_keys(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xWALLETSECRET")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xPRIVATEKEYSECRET")
    text = "error walletAddress=0xWALLETSECRET privateKey=0xPRIVATEKEYSECRET"
    redacted = redact_secrets(text)
    assert "0xWALLETSECRET" not in redacted
    assert "0xPRIVATEKEYSECRET" not in redacted
    assert "<redacted>" in redacted


# --- (c) ccxt_adapter builds the Hyperliquid wallet/key kwargs branch (mock, no network) ---

def test_ccxt_adapter_hyperliquid_auth_kwargs(monkeypatch):
    captured = {}

    class _FakeHyperliquid:
        def __init__(self, kwargs):
            captured.update(kwargs)

        def set_sandbox_mode(self, flag):
            captured["_sandbox"] = flag

    fake_ccxt = types.SimpleNamespace(hyperliquid=_FakeHyperliquid)
    monkeypatch.setattr(ccxt_adapter, "ccxt", fake_ccxt)
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xWALLET")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xPRIVKEY")
    # No HL testnet vars so the base namespaced names are used.
    monkeypatch.delenv("HYPERLIQUID_TESTNET_WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("HYPERLIQUID_TESTNET_PRIVATE_KEY", raising=False)

    config = {"execution": {"exchange": "ccxt", "ccxt_exchange": "hyperliquid", "simulated_trading": True}}
    executor = CcxtExecutor(config, ".")
    client = executor._client()

    assert isinstance(client, _FakeHyperliquid)
    # Wallet-based auth shape, NOT apiKey/secret/password.
    assert captured["walletAddress"] == "0xWALLET"
    assert captured["privateKey"] == "0xPRIVKEY"
    assert "apiKey" not in captured
    assert "secret" not in captured
    assert "password" not in captured
    # simulated_trading -> sandbox mode engaged.
    assert captured.get("_sandbox") is True


def test_ccxt_adapter_hyperliquid_disarmed_without_credentials(monkeypatch):
    # Fail-closed: missing HL creds => blank wallet/key kwargs (no auth, disarmed).
    captured = {}

    class _FakeHyperliquid:
        def __init__(self, kwargs):
            captured.update(kwargs)

        def set_sandbox_mode(self, flag):
            captured["_sandbox"] = flag

    fake_ccxt = types.SimpleNamespace(hyperliquid=_FakeHyperliquid)
    monkeypatch.setattr(ccxt_adapter, "ccxt", fake_ccxt)
    for var in (
        "HYPERLIQUID_WALLET_ADDRESS",
        "HYPERLIQUID_PRIVATE_KEY",
        "HYPERLIQUID_TESTNET_WALLET_ADDRESS",
        "HYPERLIQUID_TESTNET_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    config = {"execution": {"exchange": "ccxt", "ccxt_exchange": "hyperliquid", "simulated_trading": True}}
    executor = CcxtExecutor(config, ".")
    executor._client()

    assert captured["walletAddress"] == ""
    assert captured["privateKey"] == ""


# --- (d) instrument.exchange=kucoin resolves to the kucoin venue (dry-run, no network) ---

def test_instrument_exchange_kucoin_resolves_kucoin_venue():
    config = {"execution": {"exchange": "ccxt", "ccxt_exchange": "okx", "ccxt_default_type": "swap"}}
    readiness = {"instrument": {"exchange": "kucoin", "inst_id": "BTC-USDT-SWAP", "type": "swap"}}
    resolved = resolve_execution_config(config, readiness)

    assert resolved["execution"]["ccxt_exchange"] == "kucoin"
    # Adapter selector is unchanged -- only the active CCXT venue moved.
    assert resolved["execution"]["exchange"] == "ccxt"

    executor = ExecutorFactory.create(resolved, ".")
    assert isinstance(executor, CcxtExecutor)
    assert executor._exchange_id() == "kucoin"


# --- (e) a v1/OKX strategy still resolves to okx, byte-identical ---

def test_v1_okx_strategy_resolves_okx_identically():
    config = {"execution": {"exchange": "ccxt", "ccxt_exchange": "okx", "ccxt_default_type": "swap"}}
    # strategy_instrument() maps a v1 okx_inst_id strategy to instrument.exchange == "okx".
    okx_readiness = {"instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"}}

    with_instrument = resolve_execution_config(config, okx_readiness)
    without_instrument = resolve_execution_config(config, None)

    # Both resolve to okx, and the resulting execution block is byte-identical.
    assert with_instrument["execution"]["ccxt_exchange"] == "okx"
    assert without_instrument["execution"]["ccxt_exchange"] == "okx"
    assert with_instrument["execution"] == without_instrument["execution"]

    executor = ExecutorFactory.create(with_instrument, ".")
    assert isinstance(executor, CcxtExecutor)
    assert executor._exchange_id() == "okx"


def test_strategy_instrument_v1_shim_maps_to_okx():
    # The M1 shim: a v1 strategy (okx_inst_id only) presents instrument.exchange == okx.
    from webhook_receiver import strategy_instrument

    v1 = strategy_instrument({"okx_inst_id": "BTC-USDT-SWAP"})
    assert v1 == {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"}

    v2_kucoin = strategy_instrument({"instrument": {"exchange": "kucoin", "inst_id": "BTC-USDT-SWAP"}})
    assert v2_kucoin["exchange"] == "kucoin"
