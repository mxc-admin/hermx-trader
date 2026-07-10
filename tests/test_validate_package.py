"""Regression test for scripts/validate_package.py.

The python-compile loop used ``(ROOT/"src").glob("*.py")`` which only saw top-level src
files -- code in subpackages (``src/executors/``, ``src/webhook/`` …) was never compiled,
so a syntax error there passed validation silently. It now uses ``rglob`` (recursive). This
test proves a file in a nested subpackage is actually validated.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "validate_package.py"


def _load():
    spec = importlib.util.spec_from_file_location("hermx_validate_package", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nested_src_file_is_validated(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    nested = tmp_path / "src" / "executors"
    nested.mkdir(parents=True)
    # A syntactically broken file in a subpackage -- only caught if the glob recurses.
    (nested / "broken.py").write_text("def (\n", encoding="utf-8")

    rc = mod.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "python compile failed" in out
    assert "src/executors/broken.py" in out
