"""Phase 6 / Layer C: strategy schema v2 (canonical) + loader canonicalization.

Proves:
  * the four LIVE strategy files (schema_version 2) validate against the schema,
  * a schema_version 2 strategy (generic `instrument` block + `submit_orders`,
    relaxed asset regex, CCXT-unified symbol) validates,
  * inline credentials are forbidden in a v2 strategy,
  * the loader shim canonicalizes the v2 `instrument` block in place and never
    injects the legacy `okx_inst_id` key, and
  * a v2 strategy file loads end-to-end and produces an execution readiness with
    the money-path fields (inst_id, td_mode, execution_intent, ...) intact.

Layer C removed the runtime v1 (`okx_inst_id`) bridge; strategy files are
canonical v2 on disk. The transitional v1 schema branch is intentionally still
present in strategy.schema.json (deferred), but no test depends on a v1 twin.

No network, no OKX subprocess: schema checks are pure jsonschema; the end-to-end
load test reloads webhook_receiver against an isolated temp SHADOW_ROOT (mirrors
the conftest harness) with execution hard-disabled by the dry-run corpus config.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import jsonschema
import pytest

from conftest import _SHADOW_ROOT, load_alert, write_engine_config

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


# The canonical v2 strategy (generic instrument block + submit_orders posture).
V2_TWIN = {
    "schema_version": 2,
    "strategy_id": "btcusdt_duo_base_dev_2h",
    "name": "BTCUSDT Duo Base Dev 2H",
    "indicator": "mxc duo-base v2.5",
    "timeframe": "2h",
    "instrument": {"exchange": "okx", "inst_id": "BTC-USDT-SWAP", "type": "swap"},
    "capital": {"budget_usd": 1500, "reinvest": True},
    "execution_mode": "demo",
    "submit_orders": True,
    "leverage": 2,
    "margin_mode": "isolated",
    "notes": "Active OKX sandbox/demo candidate. submit_orders=false makes it inert.",
}


# --------------------------------------------------------------------------- #
# Schema validation                                                            #
# --------------------------------------------------------------------------- #


def test_schema_is_well_formed():
    jsonschema.Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize("path", sorted(LIVE_STRATEGIES_DIR.glob("*.json")), ids=lambda p: p.name)
def test_live_strategy_files_validate(path):
    """The four production strategy files (schema_version 2) MUST validate."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert _is_valid(payload)


def test_v2_strategy_validates():
    assert _is_valid(V2_TWIN)


def test_v2_accepts_ccxt_unified_symbol_and_non_usdt_quote():
    """Relaxed instrument regex: not pinned to *USDT."""
    v2 = json.loads(json.dumps(V2_TWIN))
    v2["instrument"] = {"exchange": "okx", "inst_id": "BTC/USDT:USDT", "type": "swap"}
    assert _is_valid(v2)

    v2b = json.loads(json.dumps(V2_TWIN))
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


def test_wrong_schema_version_rejected():
    v3 = json.loads(json.dumps(V2_TWIN))
    v3["schema_version"] = 3
    assert not _is_valid(v3)


# --------------------------------------------------------------------------- #
# Loader shim: v2 instrument is canonicalized, no legacy key injection         #
# --------------------------------------------------------------------------- #


def _module():
    import webhook_receiver as module  # noqa: WPS433

    return module


def test_normalize_shim_canonicalizes_v2_instrument():
    m = _module()
    out = m.normalize_strategy_record(json.loads(json.dumps(V2_TWIN)))
    # Layer C: canonicalize the instrument block in place.
    assert out["instrument"]["inst_id"] == "BTC-USDT-SWAP"
    assert out["instrument"]["exchange"] == "okx"
    assert out["instrument"]["type"] == "swap"
    # The okx_submit_orders bridge is preserved (out of scope for this slice).
    assert out["okx_submit_orders"] is True


def test_normalize_shim_does_not_inject_legacy_okx_inst_id():
    m = _module()
    out = m.normalize_strategy_record(json.loads(json.dumps(V2_TWIN)))
    # Layer C removed the v1 bridge: no legacy okx_inst_id is synthesized.
    assert "okx_inst_id" not in out


def test_strategy_instrument_resolves_canonical_v2_row():
    m = _module()
    normalized = m.normalize_strategy_record(json.loads(json.dumps(V2_TWIN)))
    assert m.strategy_instrument(normalized) == {
        "exchange": "okx",
        "inst_id": "BTC-USDT-SWAP",
        "type": "swap",
    }


def test_normalize_is_idempotent_for_v2():
    m = _module()
    once = m.normalize_strategy_record(json.loads(json.dumps(V2_TWIN)))
    twice = m.normalize_strategy_record(json.loads(json.dumps(once)))
    assert m.strategy_instrument(once) == m.strategy_instrument(twice)
    assert once["instrument"]["inst_id"] == twice["instrument"]["inst_id"]
    assert bool(once["okx_submit_orders"]) == bool(twice["okx_submit_orders"])


# --------------------------------------------------------------------------- #
# End-to-end: a v2 strategy file loads, matches an alert, and is money-ready   #
# --------------------------------------------------------------------------- #


def _build_root_with_strategy(root: Path, strategy: dict) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    strategies_dir = root / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    (strategies_dir / f"{strategy['strategy_id']}.json").write_text(
        json.dumps(strategy, indent=2), encoding="utf-8"
    )
    write_engine_config(root)


def _load_module_at(root: Path):
    import webhook_receiver as module  # noqa: WPS433

    os.environ["SHADOW_ROOT"] = str(root)
    os.environ.pop("HERMX_LIVE_TRADING", None)
    importlib.reload(module)
    return module


def _readiness_for_btc_buy(module):
    payload = load_alert("strategy/btcusdt_buy.json")
    normalized = module.normalize(payload)
    ok, strategy, error = module.validate_strategy_alert(normalized)
    assert ok is True, error
    record = {"normalized": normalized, "strategy_config": strategy}
    return strategy, module.build_strategy_execution_readiness(record)


def test_v2_strategy_file_loads_and_is_money_ready(tmp_path):
    """A v2 strategy file routed through the real module load + matching +
    readiness path resolves via the canonical instrument block and carries the
    money-path execution fields."""
    orig_root = os.environ.get("SHADOW_ROOT")
    try:
        v2_root = tmp_path / "v2-root"
        _build_root_with_strategy(v2_root, V2_TWIN)
        m = _load_module_at(v2_root)
        loaded = m.STRATEGIES["btcusdt_duo_base_dev_2h"]
        # Layer C: the loaded v2 strategy resolves via the canonical instrument block.
        assert loaded["instrument"]["inst_id"] == "BTC-USDT-SWAP"
        assert "okx_inst_id" not in loaded
        assert loaded["okx_submit_orders"] is True

        _, readiness = _readiness_for_btc_buy(m)

        # Money-path readiness fields are present and internally consistent.
        assert readiness["inst_id"] == "BTC-USDT-SWAP"
        assert readiness["instrument"]["inst_id"] == readiness["inst_id"]
        assert readiness["td_mode"] == "isolated"
        assert readiness["expected_leverage"] == 2
        assert readiness["signal_side"] == "buy"
        intent = readiness["execution_intent"]
        assert intent["planned_notional_usd"] == 3000.0  # budget 1500 * leverage 2
        assert intent["actions"]
    finally:
        os.environ["SHADOW_ROOT"] = orig_root if orig_root is not None else str(_SHADOW_ROOT)
        _load_module_at(Path(os.environ["SHADOW_ROOT"]))
