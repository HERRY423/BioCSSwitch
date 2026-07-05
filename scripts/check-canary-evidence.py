#!/usr/bin/env python3
"""Validate a manually captured Claude Science canary evidence artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


REQUIRED_SCHEMA = "csswitch/canary-evidence/1"


def _load(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[canary] cannot read {path}: {exc}") from exc


def validate(data: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if data.get("schema") != REQUIRED_SCHEMA:
        errors.append(f"schema must be {REQUIRED_SCHEMA!r}")
    for key in ("checked_at", "science_version", "marker"):
        if not data.get(key):
            errors.append(f"missing {key}")
    for key in ("mcp", "skill"):
        obj = data.get(key)
        if not isinstance(obj, dict):
            errors.append(f"missing {key} object")
            continue
        if obj.get("status") != "passed":
            errors.append(f"{key}.status must be 'passed'")
        if not obj.get("note"):
            errors.append(f"missing {key}.note")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence", type=Path)
    args = ap.parse_args()

    data = _load(args.evidence)
    errors = validate(data)
    if errors:
        print("[canary] evidence is not release-ready:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(
        "[canary] PASS "
        f"science={data.get('science_version')} marker={str(data.get('marker'))[:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
