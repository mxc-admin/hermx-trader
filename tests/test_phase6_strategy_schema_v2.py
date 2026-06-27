"""Phase 6 / M1: strategy schema v2 + v1->v2 loader shim.

Proves:
  * the four LIVE v1 strategy files still validate against the schema,
  * a schema_version 2 strategy (generic `instrument` block + `submit_orders`,
    relaxed asset regex, CCXT-unified symbol) validates,
  * inline credentials are forbidden in BOTH versions,
  * a v1 strategy and its v2 twin load to the SAME internal representation
    (same instrument + same legacy okx_inst_id/okx_submit_orders the execution
    readiness path reads), so v2 produces byte-identical execution behavior.

No network, no OKX subprocess: schema checks are pure jsonschema; the end-to-end
load test reloads webhook_receiver against an isolated temp SHADOW_ROOT (mirrors
the conftest harness) with execution hard-disabled by the dry-run corpus config.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path

import jsonschema
import pytest

from conftest import CORPUS_CONFIG, FIXTURES_DIR, _SHADOW_ROOT, load_alert

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "strategy.schema.json"
LIVE_STRATEGIES_DIR = REPO_ROOT / "strategies"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _is_valid(payload: dict) -> bool:
    return not list(_validator().iter_errors(payload))


# A v1 strategy and its exact v2 twin (same venue/instrument, same submit posture).
V1_TWIN = {
    "schema_version": 1,
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "name": "BTCUSDT Duo Base Dev 2H",
    "asset": "BTCUSDT",
    "okx_inst_id": "BTC-USDT-SWAP",
    "timeframe": "2h",
    "chart_type": "heikin_ashi",
    "indicator": "mxc duo-base",
    "indicator_version": "duo-base-2.5",
    "upper_band_mult": 1.40,
    "lower_band_mult": 0.95,
    "auto_alpha": False,
    "budget_usd": 1500,
    "leverage": 2,
    "margin_mode": "isolated",
    "execution_mode": "demo",
    "okx_submit_orders": True,
    "status": "active_demo",
}

V2_TWIN = {
    "schema_version": 2,
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "name": "BTCUSDT Duo Base Dev 2H",
    "asset": "BTCUSDT",
    "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
    "timeframe": "2h",
    "chart_type": "heikin_ashi",
    "indicator": "mxc duo-base",
    "indicator_version": "duo-base-2.5",
    "upper_band_mult": 1.40,
    "lower_band_mult": 0.95,
    "auto_alpha": False,
    "budget_usd": 1500,
    "leverage": 2,
    "margin_mode": "isolated",
    "execution_mode": "demo",
    "submit_orders": True,
    "status": "active_demo",
}


# --------------------------------------------------------------------------- #
# Schema validation                                                            #
# --------------------------------------------------------------------------- #


def test_schema_is_well_formed():
    jsonschema.Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize("path", sorted(LIVE_STRATEGIES_DIR.glob("*.json")), ids=lambda p: p.name)
def test_live_v1_strategy_files_still_validate(path):
    """The four production v1 files MUST keep validating (no migration yet)."""
    assert _is_valid(json.loads(path.read_text(encoding="utf-8")))


def test_v2_strategy_validates():
    assert _is_valid(V2_TWIN)


def test_v2_accepts_ccxt_unified_symbol_and_non_usdt_quote():
    """Relaxed asset/instrument regex: not pinned to *USDT."""
    v2 = json.loads(json.dumps(V2_TWIN))
    v2["asset"] = "BTC/USDT"
    v2["instrument"] = {"exchange": "okx", "inst_id": "BTC/USDT:USDT", "type": "swap"}
    assert _is_valid(v2)

    v2b = json.loads(json.dumps(V2_TWIN))
    v2b["asset"] = "ETHBTC"
    v2b["instrument"] = {"exchange": "kucoin", "inst_id": "ETH-BTC", "type": "spot"}
    assert _is_valid(v2b)


def test_v2_requires_instrument_block():
    v2 = json.loads(json.dumps(V2_TWIN))
    v2.pop("instrument")
    assert not _is_valid(v2)


@pytest.mark.parametrize(
    "cred_field",
    ["api_key", "apiKey", "secret", "secret_key", "passphrase", "private_key", "wallet", "token"],
)
def test_inline_credentials_forbidden_v2(cred_field):
    v2 = json.loads(json.dumps(V2_TWIN))
    v2[cred_field] = "leaked-value"
    assert not _is_valid(v2), f"{cred_field} must be rejected in a v2 strategy"


@pytest.mark.parametrize("cred_field", ["api_key", "secret_key", "passphrase", "private_key", "wallet"])
def test_inline_credentials_forbidden_v1(cred_field):
    v1 = json.loads(json.dumps(V1_TWIN))
    v1[cred_field] = "leaked-value"
    assert not _is_valid(v1), f"{cred_field} must be rejected in a v1 strategy"


def test_wrong_schema_version_rejected():
    v3 = json.loads(json.dumps(V2_TWIN))
    v3["schema_version"] = 3
    assert not _is_valid(v3)


# --------------------------------------------------------------------------- #
# Loader shim: v1 and v2 resolve to the SAME internal representation           #
# --------------------------------------------------------------------------- #


def _module():
    import webhook_receiver as module  # noqa: WPS433

    return module


def test_normalize_shim_bridges_v2_to_legacy_keys():
    m = _module()
    out = m.normalize_strategy_record(json.loads(json.dumps(V2_TWIN)))
    assert out["okx_inst_id"] == "BTC-USDT-SWAP"
    assert out["okx_submit_orders"] is True


def test_normalize_shim_leaves_v1_unchanged():
    m = _module()
    original = json.loads(json.dumps(V1_TWIN))
    out = m.normalize_strategy_record(json.loads(json.dumps(V1_TWIN)))
    assert out == original  # byte-identical: no new keys, nothing dropped


@pytest.mark.parametrize("row", [V1_TWIN, V2_TWIN], ids=["v1", "v2"])
def test_strategy_instrument_is_version_agnostic(row):
    m = _module()
    assert m.strategy_instrument(row) == {
        "exchange": "okx",
        "inst_id": "BTC-USDT-SWAP",
        "type": "swap",
    }


def test_v1_and_v2_resolve_identically():
    m = _module()
    v1 = m.normalize_strategy_record(json.loads(json.dumps(V1_TWIN)))
    v2 = m.normalize_strategy_record(json.loads(json.dumps(V2_TWIN)))
    assert m.strategy_instrument(v1) == m.strategy_instrument(v2)
    assert v1["okx_inst_id"] == v2["okx_inst_id"]
    assert bool(v1["okx_submit_orders"]) == bool(v2["okx_submit_orders"])


# --------------------------------------------------------------------------- #
# End-to-end: a v2 strategy file loads and matches an alert identically to v1  #
# --------------------------------------------------------------------------- #


def _build_root_with_strategy(root: Path, strategy: dict) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    (strategies_dir / f"{strategy['strategy_id']}.json").write_text(
        json.dumps(strategy, indent=2), encoding="utf-8"
    )
    shutil.copy(CORPUS_CONFIG, root / "shadow-config.json")


def _load_module_at(root: Path):
    import webhook_receiver as module  # noqa: WPS433

    os.environ["SHADOW_ROOT"] = str(root)
    os.environ.pop("HERMX_SUBMIT_ENABLED", None)
    importlib.reload(module)
    return module


def _readiness_for_btc_buy(module):
    payload = load_alert("strategy/btcusdt_buy.json")
    normalized = module.normalize(payload)
    ok, strategy, error = module.validate_strategy_alert(normalized)
    assert ok is True, error
    record = {"normalized": normalized, "strategy_config": strategy}
    return strategy, module.build_strategy_execution_readiness(record)


def test_v2_strategy_file_loads_and_matches_like_v1(tmp_path):
    """A v2 strategy file routed through the real module load + matching +
    readiness path produces the same instrument/readiness as its v1 twin."""
    orig_root = os.environ.get("SHADOW_ROOT")
    try:
        v1_root = tmp_path / "v1-root"
        _build_root_with_strategy(v1_root, V1_TWIN)
        m = _load_module_at(v1_root)
        assert "btcusdt_duo_base_dev_2h" in m.STRATEGIES
        v1_strategy, v1_readiness = _readiness_for_btc_buy(m)

        v2_root = tmp_path / "v2-root"
        _build_root_with_strategy(v2_root, V2_TWIN)
        m = _load_module_at(v2_root)
        loaded = m.STRATEGIES["btcusdt_duo_base_dev_2h"]
        # shim bridged the v2 instrument block to the legacy keys downstream reads
        assert loaded["okx_inst_id"] == "BTC-USDT-SWAP"
        assert loaded["okx_submit_orders"] is True
        v2_strategy, v2_readiness = _readiness_for_btc_buy(m)

        # Identical execution-relevant readiness across both versions.
        for key in (
            "okx_inst_id",
            "live_execution_enabled",
            "symbol",
            "expected_leverage",
            "signal_side",
        ):
            assert v1_readiness[key] == v2_readiness[key]
        assert v1_readiness["execution_intent"]["actions"] == v2_readiness["execution_intent"]["actions"]
        assert (
            v1_readiness["execution_intent"]["planned_notional_usd"]
            == v2_readiness["execution_intent"]["planned_notional_usd"]
        )
    finally:
        os.environ["SHADOW_ROOT"] = orig_root if orig_root is not None else str(_SHADOW_ROOT)
        _load_module_at(Path(os.environ["SHADOW_ROOT"]))
