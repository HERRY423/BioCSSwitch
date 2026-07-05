#!/usr/bin/env python3
"""Audit the expert-review ledger for bio_eval gold cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cases import CASES  # noqa: E402


LEDGER = ROOT / "gold_calibration.json"
SCHEMA = "bio_eval/gold-calibration/1"
ALLOWED = {"needs_expert_review", "approved", "rejected", "superseded"}


def gold_case_ids() -> List[str]:
    return sorted(
        c["id"]
        for c in CASES
        if (c.get("rubric") or {}).get("gold")
    )


def load_ledger(path: Path = LEDGER) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[gold] cannot read {path}: {exc}") from exc


def check_ledger(data: Dict[str, Any], strict: bool = False) -> List[str]:
    errors: List[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}")

    cases = data.get("cases")
    if not isinstance(cases, dict):
        return errors + ["cases must be an object"]

    expected = set(gold_case_ids())
    present = set(cases)
    for cid in sorted(expected - present):
        errors.append(f"missing calibration entry for gold case {cid}")
    for cid in sorted(present - expected):
        errors.append(f"ledger has entry for non-gold or removed case {cid}")

    for cid in sorted(expected & present):
        entry = cases.get(cid) or {}
        status = entry.get("status")
        if status not in ALLOWED:
            errors.append(f"{cid}: invalid status {status!r}")
            continue
        if strict and status != "approved":
            errors.append(f"{cid}: status {status!r} is not approved")
        if status == "approved":
            if not entry.get("reviewer"):
                errors.append(f"{cid}: approved entry needs reviewer")
            if not entry.get("reviewed_at"):
                errors.append(f"{cid}: approved entry needs reviewed_at")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate the ledger")
    ap.add_argument("--strict", action="store_true", help="require every gold case to be approved")
    args = ap.parse_args()

    data = load_ledger()
    errors = check_ledger(data, strict=args.strict)
    if errors:
        print("[gold] calibration ledger is not ready:")
        for err in errors:
            print(f"  - {err}")
        return 1
    mode = "strict" if args.strict else "coverage"
    print(f"[gold] PASS ({mode}); gold cases: {', '.join(gold_case_ids())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
