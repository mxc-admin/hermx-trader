"""Phase 6 / Layer C: live strategy files are canonical v2, money-path safe.

The four production strategy files are schema_version 2 (generic ``instrument``
block + ``submit_orders``). Layer C removed the runtime v1 (``okx_inst_id``)
bridge, so this test no longer reconstructs or validates a v1 twin. It instead
LOCKS the live v2 corpus as money-path safe:

  * each on-disk file is schema_version 2 and validates against the schema,
  * the ``instrument`` block is sane (okx swap, inst_id matches the file),
  * no legacy ``okx_inst_id`` / ``okx_submit_orders`` keys remain on disk,
  * routed through the REAL module load + alert matching + execution-readiness
    path, the readiness payload is internally consistent and stable across loads
    (inst_id, td_mode, planned_notional_usd, client_order_id, signal_side, and
    the M3 exchange-agnostic instrument/intent fields).

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
def test_live_file_is_canonical_v2(strategy_id, alert_rel):
    """The on-disk file is canonical v2 with a sane instrument block and no
    leftover legacy keys."""
    v2 = json.loads((LIVE_STRATEGIES_DIR / f"{strategy_id}.json").read_text(encoding="utf-8"))
    assert v2["schema_version"] == 2
    assert v2["instrument"] == {
        "exchange": "okx",
        "inst_id": v2["instrument"]["inst_id"],
        "type": "swap",
    }
    assert v2["instrument"]["inst_id"].endswith("-USDT-SWAP")
    assert "okx_inst_id" not in v2 and "okx_submit_orders" not in v2
    assert _is_valid(v2) and _matches_branch(v2, "strategy_v2")


@pytest.mark.parametrize("strategy_id,alert_rel", MIGRATED, ids=[m[0] for m in MIGRATED])
def test_live_v2_readiness_stable_and_consistent(strategy_id, alert_rel, tmp_path):
    """End-to-end: the live v2 file produces an internally-consistent execution
    readiness that is stable (deterministic) across two isolated loads."""
    v2 = json.loads((LIVE_STRATEGIES_DIR / f"{strategy_id}.json").read_text(encoding="utf-8"))

    orig_root = os.environ.get("SHADOW_ROOT")
    try:
        a_root = tmp_path / "v2-root-a"
        _build_root_with_strategy(a_root, v2)
        m = _load_module_at(a_root)
        loaded = m.STRATEGIES[strategy_id]
        # Layer C: resolves via the canonical instrument block; bridge preserved.
        assert loaded["instrument"]["inst_id"] == v2["instrument"]["inst_id"]
        assert "okx_inst_id" not in loaded
        assert loaded["okx_submit_orders"] is bool(v2.get("submit_orders", False))
        readiness_a = _readiness(m, strategy_id, alert_rel)

        b_root = tmp_path / "v2-root-b"
        _build_root_with_strategy(b_root, v2)
        m = _load_module_at(b_root)
        readiness_b = _readiness(m, strategy_id, alert_rel)

        # Deterministic across loads -- the corpus is reproducible.
        assert readiness_a == readiness_b

        # Money-critical readiness fields are present and internally consistent.
        assert readiness_a["inst_id"] == v2["instrument"]["inst_id"]
        assert readiness_a["instrument"] == v2["instrument"]  # M3 agnostic block
        assert readiness_a["instrument"]["inst_id"] == readiness_a["inst_id"]
        assert readiness_a["td_mode"] == v2["margin_mode"]
        assert readiness_a["expected_leverage"] == v2["leverage"]
        assert readiness_a["signal_side"] in {"buy", "sell"}
        intent = readiness_a["execution_intent"]
        assert intent["planned_notional_usd"] == float(v2["budget_usd"]) * float(v2["leverage"])
        assert intent["client_order_id"]
        assert intent["actions"]
    finally:
        os.environ["SHADOW_ROOT"] = orig_root if orig_root is not None else str(_SHADOW_ROOT)
        _load_module_at(Path(os.environ["SHADOW_ROOT"]))
