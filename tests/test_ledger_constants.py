"""Regression guard for the Immediate Hotfix Track (REFACTOR_PLAN.md:138, :147).

Static-analyzes ``src/webhook_receiver.py`` and asserts that every ``*_LEDGER``
name *referenced* in the module is *defined* at module scope. This fails if the
undefined constants ``OKX_EXECUTION_PLAN_LEDGER`` / ``OKX_EXECUTION_LEDGER`` (or
any other unresolved ``*_LEDGER`` symbol) are reintroduced -- which previously
caused a silent NameError during readiness/execution logging.
"""
from __future__ import annotations

import ast
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "webhook_receiver.py"


def _module_level_assigned_ledger_names(tree: ast.Module) -> set[str]:
    """Names ending in ``_LEDGER`` assigned at module scope."""
    defined: set[str] = set()
    for node in tree.body:  # module-level statements only
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith("_LEDGER"):
                defined.add(target.id)
    return defined


def _referenced_ledger_names(tree: ast.Module) -> set[str]:
    """All loaded ``*_LEDGER`` names referenced anywhere in the module."""
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id.endswith("_LEDGER")
        ):
            referenced.add(node.id)
    return referenced


def test_no_undefined_ledger_symbols():
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MODULE_PATH))

    defined = _module_level_assigned_ledger_names(tree)
    referenced = _referenced_ledger_names(tree)
    undefined = referenced - defined

    assert not undefined, (
        "Undefined *_LEDGER symbol(s) referenced in webhook_receiver.py: "
        f"{sorted(undefined)}. Define them at module scope or use the correct "
        "constant (EXECUTION_PLAN_LEDGER / EXECUTION_LEDGER)."
    )


def test_legacy_okx_ledger_constants_not_referenced():
    """Explicit belt-and-suspenders check for the two known hotfix offenders."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MODULE_PATH))
    referenced = _referenced_ledger_names(tree)

    for forbidden in ("OKX_EXECUTION_PLAN_LEDGER", "OKX_EXECUTION_LEDGER"):
        assert forbidden not in referenced, (
            f"{forbidden} is undefined and must not be referenced; use the "
            "defined EXECUTION_PLAN_LEDGER / EXECUTION_LEDGER constants."
        )
