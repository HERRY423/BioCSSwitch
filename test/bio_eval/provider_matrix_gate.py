#!/usr/bin/env python3
"""Release gate for provider-matrix result files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent))

from run import _RESULTS_DIR, _render_matrix_md, build_provider_matrix  # noqa: E402


def _valid_result_files(results_dir: Path) -> List[Path]:
    files: List[Path] = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data.get("summary"), dict) and isinstance(data.get("results"), list):
            files.append(path)
    return files


def check_matrix(rows: List[Dict[str, Any]], min_providers: int, min_repeat: int) -> List[str]:
    errors: List[str] = []
    if len(rows) < min_providers:
        errors.append(f"need at least {min_providers} providers, found {len(rows)}")
    for row in rows:
        provider = row.get("provider") or "<unknown>"
        repeat = row.get("repeat") or 1
        if repeat < min_repeat:
            errors.append(f"{provider}: repeat {repeat} < {min_repeat}")
        if row.get("overall") is None:
            errors.append(f"{provider}: missing overall score")
        if row.get("redteam") is None:
            errors.append(f"{provider}: missing redteam score")
        if row.get("tool_call") is None:
            errors.append(f"{provider}: missing tool_call score")
        if row.get("stability_stdev") is None:
            errors.append(f"{provider}: missing stability stdev; run with --repeat >= 2")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", type=Path, default=_RESULTS_DIR)
    ap.add_argument("--min-providers", type=int, default=2)
    ap.add_argument("--min-repeat", type=int, default=3)
    args = ap.parse_args()

    files = _valid_result_files(args.results_dir)
    matrix = build_provider_matrix(files)
    rows = matrix.get("rows") or []
    if rows:
        print(_render_matrix_md(matrix))
    errors = check_matrix(rows, args.min_providers, args.min_repeat)
    if errors:
        print("[provider-matrix] release gate failed:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"[provider-matrix] PASS; providers={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
