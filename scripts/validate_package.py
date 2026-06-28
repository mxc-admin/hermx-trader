#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures: list[str] = []

    required = [
        "README.md",
        "INSTALL.md",
        "ARCHITECTURE.md",
        "src/webhook_receiver.py",
        "src/dashboard.py",
        "src/dashboard_core.py",
        "schemas/strategy.schema.json",
        "schemas/tradingview-alert.schema.json",
        "config/runtime.demo.json",
        "setup/env.example",
    ]
    for rel in required:
        if not (ROOT / rel).exists():
            failures.append(f"missing required file: {rel}")

    json_paths = list(ROOT.glob("*.json"))
    for subdir in ("config", "schemas", "strategies"):
        json_paths.extend((ROOT / subdir).rglob("*.json"))
    for path in json_paths:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"invalid json: {path.relative_to(ROOT)}: {exc}")

    for path in (ROOT / "src").glob("*.py"):
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            failures.append(f"python compile failed: {path.relative_to(ROOT)}: {exc}")

    strategies = sorted((ROOT / "strategies").glob("*.json"))
    if len(strategies) < 1:
        failures.append(f"expected at least 1 strategy file, found {len(strategies)}")

    if failures:
        print("Package validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Package validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
