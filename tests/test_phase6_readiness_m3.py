"""Phase 6 / M3: generic instruction/readiness wire format.

The strategy execution-readiness payload now exposes the exchange-agnostic
instruction shape from ARCHITECTURE.md (strategy_id, asset, target_side,
target_notional_usd, margin_mode, leverage, plus the generic ``instrument``
block) as THE contract going forward, while translated execution keys
(inst_id, td_mode, expected_leverage) remain present as adapter-derived
translations.

Proves:
  * readiness contains BOTH the agnostic fields AND translated execution fields,
  * every agnostic field is byte-identical to its translated twin / the
    execution_intent value the executor already consumes (order-equivalence:
    M3 changes representation, not the order),
  * loading the SAME canonical v2 strategy twice produces an identical agnostic
    and execution readiness shape (deterministic invariance via the loader shim).

Layer C: the runtime v1 (`okx_inst_id`) bridge is gone, so the readiness is
exercised with the canonical v2 strategy only. Reuses the isolated-temp-
SHADOW_ROOT reload harness from the schema-v2 test module so the readiness is
built by the REAL module load + matching path, execution hard-disabled by the
dry-run corpus config (no network, no OKX subprocess).
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path

from conftest import CORPUS_CONFIG, load_alert
from test_phase6_strategy_schema_v2 import V2_TWIN


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
    orig_root = os.environ.get("SHADOW_ROOT")
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
            os.environ["SHADOW_ROOT"] = orig_root


def test_agnostic_fields_are_byte_identical_to_execution_twins(tmp_path):
    """Order-equivalence: each agnostic field equals its translated twin / the
    execution_intent value the executor already consumes. M3 is representation
    only -- nothing the adapter reads to build the order changed."""
    orig_root = os.environ.get("SHADOW_ROOT")
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
            os.environ["SHADOW_ROOT"] = orig_root


def test_v2_readiness_is_deterministic_across_loads(tmp_path):
    """Loader invariance: the SAME canonical v2 strategy loaded into two isolated
    roots emits an identical agnostic AND execution readiness shape. This proves
    the agnostic+execution twin relationship is stable through the loader shim
    without depending on a v1 twin (Layer C removed the v1 bridge)."""
    orig_root = os.environ.get("SHADOW_ROOT")
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
            os.environ["SHADOW_ROOT"] = orig_root
