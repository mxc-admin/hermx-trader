"""Phase 6 / task 4: live strategy file migration v1 -> v2, behavior-preserving.

The four production strategy files were migrated from schema_version 1
(``okx_inst_id`` + ``okx_submit_orders``) to schema_version 2 (generic
``instrument`` block + ``submit_orders``). This test LOCKS that migration as
byte-for-byte behavior-preserving for the money path:

  * each migrated file on disk validates against the v2 branch of the schema,
  * its in-test reconstructed v1 twin (instrument -> okx_inst_id,
    submit_orders -> okx_submit_orders) validates against the v1 branch,
  * routed through the REAL module load + alert matching + execution-readiness
    path, the v2 file and its v1 twin produce an IDENTICAL readiness payload
    (okx_inst_id, td_mode, planned_notional_usd, client_order_id, signal_side,
    and the M3 exchange-agnostic instrument/intent fields are all unchanged).

No network, no OKX subprocess: each file is loaded into an isolated temp
SHADOW_ROOT with execution hard-disabled by the dry-run corpus config (mirrors
the conftest harness + tests/test_phase6_strategy_schema_v2.py).
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path

import jsonschema
import pytest

from conftest import CORPUS_CONFIG, _SHADOW_ROOT, load_alert

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "strategy.schema.json"
LIVE_STRATEGIES_DIR = REPO_ROOT / "strategies"

# Each live strategy file paired with the alert fixture that matches it.
MIGRATED = [
    ("btcusdt_duo_base_dev_2h", "strategy/btcusdt_buy.json"),
    ("ethusdt_duo_base_dev_2h", "strategy/ethusdt_buy.json"),
    ("solusdt_duo_base_dev_3h", "strategy/solusdt_sell.json"),
    ("xrpusdt_duo_base_dev_4h", "strategy/xrpusdt_buy.json"),
]


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _is_valid(payload: dict) -> bool:
    return not list(_validator().iter_errors(payload))


def _matches_branch(payload: dict, branch: str) -> bool:
    """True iff ``payload`` validates against exactly the named ($defs) branch."""
    schema = _schema()
    branch_schema = dict(schema)
    branch_schema["oneOf"] = [{"$ref": f"#/$defs/{branch}"}]
    return not list(jsonschema.Draft202012Validator(branch_schema).iter_errors(payload))


def _reconstruct_v1(v2: dict) -> dict:
    """Rebuild the ORIGINAL v1 form of a migrated v2 strategy file in-test.

    Inverse of the task-4 migration: instrument.inst_id -> okx_inst_id,
    submit_orders -> okx_submit_orders, schema_version 2 -> 1. Every other field
    is carried over untouched so the only delta is the schema restructuring.
    """
    v1 = json.loads(json.dumps(v2))
    instrument = v1.pop("instrument")
    assert instrument["exchange"] == "okx", "migration must keep exchange=okx"
    v1["schema_version"] = 1
    v1["okx_inst_id"] = instrument["inst_id"]
    if "submit_orders" in v1:
        v1["okx_submit_orders"] = bool(v1.pop("submit_orders"))
    return v1


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


def _readiness(module, strategy_id: str, alert_rel: str) -> dict:
    payload = load_alert(alert_rel)
    normalized = module.normalize(payload)
    ok, strategy, error = module.validate_strategy_alert(normalized)
    assert ok is True, error
    record = {"normalized": normalized, "strategy_config": strategy}
    return module.build_strategy_execution_readiness(record)


@pytest.mark.parametrize("strategy_id,alert_rel", MIGRATED, ids=[m[0] for m in MIGRATED])
def test_migrated_file_is_v2_and_twin_is_v1(strategy_id, alert_rel):
    """The on-disk file is now v2; its reconstructed twin is the original v1."""
    v2 = json.loads((LIVE_STRATEGIES_DIR / f"{strategy_id}.json").read_text(encoding="utf-8"))
    assert v2["schema_version"] == 2
    assert v2["instrument"] == {
        "exchange": "okx",
        "inst_id": v2["instrument"]["inst_id"],
        "type": "swap",
    }
    assert "okx_inst_id" not in v2 and "okx_submit_orders" not in v2
    assert _is_valid(v2) and _matches_branch(v2, "strategy_v2")

    v1 = _reconstruct_v1(v2)
    assert _is_valid(v1) and _matches_branch(v1, "strategy_v1")


@pytest.mark.parametrize("strategy_id,alert_rel", MIGRATED, ids=[m[0] for m in MIGRATED])
def test_migrated_v2_readiness_identical_to_v1(strategy_id, alert_rel, tmp_path):
    """End-to-end: the migrated v2 file and its v1 twin produce a byte-identical
    execution-readiness payload through the real load + match + readiness path."""
    v2 = json.loads((LIVE_STRATEGIES_DIR / f"{strategy_id}.json").read_text(encoding="utf-8"))
    v1 = _reconstruct_v1(v2)

    orig_root = os.environ.get("SHADOW_ROOT")
    try:
        v1_root = tmp_path / "v1-root"
        _build_root_with_strategy(v1_root, v1)
        m = _load_module_at(v1_root)
        assert strategy_id in m.STRATEGIES
        v1_readiness = _readiness(m, strategy_id, alert_rel)

        v2_root = tmp_path / "v2-root"
        _build_root_with_strategy(v2_root, v2)
        m = _load_module_at(v2_root)
        loaded = m.STRATEGIES[strategy_id]
        # the M1 shim bridged the v2 instrument block to the legacy keys downstream reads
        assert loaded["okx_inst_id"] == v2["instrument"]["inst_id"]
        assert loaded["okx_submit_orders"] is bool(v2.get("submit_orders", False))
        v2_readiness = _readiness(m, strategy_id, alert_rel)

        # The whole readiness payload is identical -- orders are unchanged.
        assert v1_readiness == v2_readiness

        # Spell out the money-critical fields the migration must not perturb.
        assert v1_readiness["okx_inst_id"] == v2_readiness["okx_inst_id"] == v2["instrument"]["inst_id"]
        assert v1_readiness["td_mode"] == v2_readiness["td_mode"]
        assert v1_readiness["signal_side"] == v2_readiness["signal_side"]
        assert v1_readiness["instrument"] == v2_readiness["instrument"]  # M3 agnostic block
        v1_intent, v2_intent = v1_readiness["execution_intent"], v2_readiness["execution_intent"]
        assert v1_intent["planned_notional_usd"] == v2_intent["planned_notional_usd"]
        assert v1_intent["client_order_id"] == v2_intent["client_order_id"]
        assert v1_intent["actions"] == v2_intent["actions"]
    finally:
        os.environ["SHADOW_ROOT"] = orig_root if orig_root is not None else str(_SHADOW_ROOT)
        _load_module_at(Path(os.environ["SHADOW_ROOT"]))
