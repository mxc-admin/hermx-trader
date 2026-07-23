"""Tests for the submit-time cloid attribution map (P&L Master Plan, Phase 7b)."""
from __future__ import annotations

from pnl_cloid_map import load_cloid_mappings, record_cloid_mapping, resolve_cloid, _map_path


def test_record_and_resolve_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-abc-123", "987654321", "hyperliquid")
    assert resolve_cloid("987654321", "hyperliquid") == "mxc-abc-123"


def test_resolve_unknown_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    assert resolve_cloid("000", "hyperliquid") is None


def test_resolve_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    assert resolve_cloid("111", "hyperliquid") is None


def test_resolve_none_inputs_return_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-abc", "111", "hyperliquid")
    assert resolve_cloid(None, "hyperliquid") is None
    assert resolve_cloid("111", None) is None


def test_different_exchange_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-abc", "111", "hyperliquid")
    assert resolve_cloid("111", "okx") is None


def test_exchange_match_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-abc", "111", "Hyperliquid")
    assert resolve_cloid("111", "HYPERLIQUID") == "mxc-abc"


def test_hex_cloid_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-abc", "0xdeadbeef", "hyperliquid")
    assert resolve_cloid("0xdeadbeef", "hyperliquid") == "mxc-abc"


def test_latest_mapping_wins_on_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-old", "111", "hyperliquid")
    record_cloid_mapping("mxc-new", "111", "hyperliquid")
    assert resolve_cloid("111", "hyperliquid") == "mxc-new"


def test_corrupt_line_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    path = _map_path()
    path.write_text('not json\n{"cloid":"222","exchange":"hyperliquid","mxc_id":"mxc-ok"}\n')
    assert resolve_cloid("222", "hyperliquid") == "mxc-ok"


def test_bulk_load_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    assert load_cloid_mappings() == {}


def test_bulk_load_matches_resolve_semantics(tmp_path, monkeypatch):
    # Last-wins per (exchange, cloid), exchange lowercased, corrupt lines skipped —
    # the same answers resolve_cloid gives, without a per-row file read.
    monkeypatch.setenv("HERMX_DATA_DIR", str(tmp_path))
    record_cloid_mapping("mxc-old", "111", "hyperliquid")
    record_cloid_mapping("mxc-new", "111", "Hyperliquid")
    record_cloid_mapping("mxc-hex", "0xdeadbeef", "hyperliquid")
    with open(_map_path(), "a", encoding="utf-8") as f:
        f.write("not json\n")
    mappings = load_cloid_mappings()
    assert mappings[("hyperliquid", "111")] == "mxc-new"
    assert mappings[("hyperliquid", "0xdeadbeef")] == "mxc-hex"
