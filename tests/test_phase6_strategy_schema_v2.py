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
load test reloads webhook_receiver against an isolated temp HERMX_ROOT (mirrors
the conftest harness) with execution hard-disabled by the dry-run corpus config.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import jsonschema
import pytest

from conftest import _HERMX_ROOT, load_alert, write_engine_config

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
    "leverage": 2,
    "margin_mode": "isolated",
    "notes": "Active OKX sandbox/demo candidate.",
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
    # The okx_submit_orders bridge is preserved for backward compat.
    assert out["okx_submit_orders"] is False


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
    assert once.get("okx_submit_orders") == twice.get("okx_submit_orders")


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

    os.environ["HERMX_ROOT"] = str(root)
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
    orig_root = os.environ.get("HERMX_ROOT")
    try:
        v2_root = tmp_path / "v2-root"
        _build_root_with_strategy(v2_root, V2_TWIN)
        m = _load_module_at(v2_root)
        loaded = m.STRATEGIES["btcusdt_duo_base_dev_2h"]
        # Layer C: the loaded v2 strategy resolves via the canonical instrument block.
        assert loaded["instrument"]["inst_id"] == "BTC-USDT-SWAP"
        assert "okx_inst_id" not in loaded
        assert loaded.get("okx_submit_orders") is False

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
        os.environ["HERMX_ROOT"] = orig_root if orig_root is not None else str(_HERMX_ROOT)
        _load_module_at(Path(os.environ["HERMX_ROOT"]))


# --------------------------------------------------------------------------- #
# Live v2 corpus is money-path safe (merged from test_phase6_strategy_migration).#
# Each production strategy file is canonical v2, validates, has a sane instrument #
# block, no legacy keys, and yields a stable/consistent readiness end-to-end.     #
# --------------------------------------------------------------------------- #

# Each live strategy file paired with the alert fixture that matches it.
MIGRATED = [
    ("btcusdt_duo_base_dev_2h", "strategy/btcusdt_buy.json"),
    ("ethusdt_duo_base_dev_2h", "strategy/ethusdt_buy.json"),
    ("solusdt_duo_base_dev_3h", "strategy/solusdt_sell.json"),
    ("xrpusdt_duo_base_dev_4h", "strategy/xrpusdt_buy.json"),
]


def _matches_branch(payload: dict, branch: str) -> bool:
    """True iff ``payload`` validates against exactly the named ($defs) branch."""
    schema = _schema()
    branch_schema = dict(schema)
    branch_schema["oneOf"] = [{"$ref": f"#/$defs/{branch}"}]
    return not list(jsonschema.Draft202012Validator(branch_schema).iter_errors(payload))


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

    orig_root = os.environ.get("HERMX_ROOT")
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
        budget = float((v2.get("capital") or {}).get("budget_usd"))
        assert intent["planned_notional_usd"] == budget * float(v2["leverage"])
        assert intent["client_order_id"]
        assert intent["actions"]
    finally:
        os.environ["HERMX_ROOT"] = orig_root if orig_root is not None else str(_HERMX_ROOT)
        _load_module_at(Path(os.environ["HERMX_ROOT"]))


# --------------------------------------------------------------------------- #
# M3 generic instruction/readiness wire format (merged from                     #
# test_phase6_readiness_m3). Readiness exposes the exchange-agnostic instruction #
# shape AND the translated execution keys, and the twin relationship is stable.  #
# --------------------------------------------------------------------------- #

AGNOSTIC_FIELDS = (
    "instrument",
    "strategy_id",
    "asset",
    "target_side",
    "target_notional_usd",
    "margin_mode",
    "leverage",
)

# Translated execution keys downstream (CCXT adapter, dashboard, ledgers) read.
EXEC_FIELDS = ("inst_id", "td_mode", "expected_leverage", "symbol", "signal_side")


def test_readiness_exposes_agnostic_and_execution_fields(tmp_path):
    """Readiness carries the generic instruction shape AND translated execution keys."""
    orig_root = os.environ.get("HERMX_ROOT")
    try:
        root = tmp_path / "v2-root"
        _build_root_with_strategy(root, V2_TWIN)
        m = _load_module_at(root)
        strategy, readiness = _readiness_for_btc_buy(m)

        # Agnostic instruction contract present (ARCHITECTURE.md shape).
        for key in AGNOSTIC_FIELDS:
            assert key in readiness, f"missing agnostic field {key!r}"
        # Translated execution keys still present and unchanged for adapter/dashboard.
        for key in EXEC_FIELDS:
            assert key in readiness, f"missing execution field {key!r}"

        # The generic instrument block resolves the same instrument as inst_id.
        assert readiness["instrument"] == {
            "exchange": "okx",
            "inst_id": "BTC-USDT-SWAP",
            "type": "swap",
        }
        assert readiness["instrument"]["inst_id"] == readiness["inst_id"]

        # Agnostic values match the strategy intent from ARCHITECTURE.md.
        assert readiness["strategy_id"] == "btcusdt_duo_base_dev_2h"
        assert readiness["asset"] == "BTCUSDT"
        assert readiness["target_side"] == "long"  # buy alert -> long
        assert readiness["margin_mode"] == "isolated"
        assert readiness["leverage"] == 2
        # budget 1500 * leverage 2 = 3000 notional.
        assert readiness["target_notional_usd"] == 3000.0
    finally:
        if orig_root is not None:
            os.environ["HERMX_ROOT"] = orig_root


def test_agnostic_fields_are_byte_identical_to_execution_twins(tmp_path):
    """Order-equivalence: each agnostic field equals its translated twin / the
    execution_intent value the executor already consumes. M3 is representation
    only -- nothing the adapter reads to build the order changed."""
    orig_root = os.environ.get("HERMX_ROOT")
    try:
        root = tmp_path / "v2-root"
        _build_root_with_strategy(root, V2_TWIN)
        m = _load_module_at(root)
        _, r = _readiness_for_btc_buy(m)

        intent = r["execution_intent"]
        # leverage / margin_mode are the SAME object the translated keys carry.
        assert r["leverage"] == r["expected_leverage"]
        assert r["margin_mode"] == r["td_mode"]
        # target_notional_usd is the exact planned_notional the executor sizes from.
        assert r["target_notional_usd"] == intent["planned_notional_usd"]
        # target_side is the agnostic view of the same signal_side the order uses.
        assert r["target_side"] == ("long" if r["signal_side"] == "buy" else "short")
        # instrument.inst_id is the agnostic view of inst_id the adapter translates.
        assert r["instrument"]["inst_id"] == r["inst_id"]

        # The exchange-agnostic order intent persisted to the journal is unchanged:
        # it still resolves inst_id from readiness and notional from the intent.
        order_intent = m._order_intent_from_readiness(r)
        assert order_intent["inst_id"] == r["inst_id"]
        assert order_intent["planned_notional_usd"] == r["target_notional_usd"]
    finally:
        if orig_root is not None:
            os.environ["HERMX_ROOT"] = orig_root


def test_v2_readiness_is_deterministic_across_loads(tmp_path):
    """Loader invariance: the SAME canonical v2 strategy loaded into two isolated
    roots emits an identical agnostic AND execution readiness shape. This proves
    the agnostic+execution twin relationship is stable through the loader shim
    without depending on a v1 twin (Layer C removed the v1 bridge)."""
    orig_root = os.environ.get("HERMX_ROOT")
    try:
        a_root = tmp_path / "v2-root-a"
        _build_root_with_strategy(a_root, V2_TWIN)
        m = _load_module_at(a_root)
        # Layer C: the loaded v2 strategy resolves via the canonical instrument block.
        assert m.STRATEGIES["btcusdt_duo_base_dev_2h"]["instrument"]["inst_id"] == "BTC-USDT-SWAP"
        _, a = _readiness_for_btc_buy(m)

        b_root = tmp_path / "v2-root-b"
        _build_root_with_strategy(b_root, V2_TWIN)
        m = _load_module_at(b_root)
        _, b = _readiness_for_btc_buy(m)

        for key in AGNOSTIC_FIELDS:
            assert a[key] == b[key], f"agnostic field {key!r} is not deterministic"
        # And the translated twins are stable too (no divergence introduced).
        for key in EXEC_FIELDS:
            assert a[key] == b[key]
        # The agnostic/execution twin relationship holds within a single load.
        assert a["instrument"]["inst_id"] == a["inst_id"]
        assert a["leverage"] == a["expected_leverage"]
        assert a["margin_mode"] == a["td_mode"]
    finally:
        if orig_root is not None:
            os.environ["HERMX_ROOT"] = orig_root
