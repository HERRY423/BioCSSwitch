#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN_BUILD=0
STRICT_EXPERT=0
REQUIRE_PROVIDER_MATRIX=0
CANARY_EVIDENCE=""

usage() {
  cat <<'USAGE'
Usage: bash scripts/release-verify.sh [options]

Options:
  --build                    Run npm ci + tauri build after tests.
  --strict-expert            Require all gold calibration entries to be approved.
  --require-provider-matrix  Require >=2 provider result files with repeat>=3.
  --canary-evidence PATH     Validate a manually captured canary evidence JSON.
  -h, --help                 Show this help.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      RUN_BUILD=1
      shift
      ;;
    --strict-expert)
      STRICT_EXPERT=1
      shift
      ;;
    --require-provider-matrix)
      REQUIRE_PROVIDER_MATRIX=1
      shift
      ;;
    --canary-evidence)
      CANARY_EVIDENCE="${2:-}"
      if [[ -z "$CANARY_EVIDENCE" ]]; then
        echo "--canary-evidence needs a path" >&2
        exit 2
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[release] missing required command: $1" >&2
    return 1
  fi
}

echo "== Python offline gates =="
need_cmd python3
python3 test/test_bio_offline.py
python3 test/bio_eval/run.py --selftest

echo "== Gold calibration gate =="
if [[ "$STRICT_EXPERT" -eq 1 ]]; then
  python3 test/bio_eval/gold_calibration.py --check --strict
else
  python3 test/bio_eval/gold_calibration.py --check
fi

if [[ -n "$CANARY_EVIDENCE" ]]; then
  echo "== Claude Science canary evidence =="
  python3 scripts/check-canary-evidence.py "$CANARY_EVIDENCE"
else
  echo "== Claude Science canary evidence =="
  echo "[release] not required in this run; pass --canary-evidence PATH for release candidates"
fi

if [[ "$REQUIRE_PROVIDER_MATRIX" -eq 1 ]]; then
  echo "== Provider matrix gate =="
  python3 test/bio_eval/provider_matrix_gate.py --min-providers 2 --min-repeat 3
else
  echo "== Provider matrix gate =="
  echo "[release] not required in this run; pass --require-provider-matrix for release candidates"
fi

echo "== Rust tests =="
if command -v cargo >/dev/null 2>&1; then
  (cd desktop/src-tauri && cargo test)
else
  echo "[release] cargo is missing; Rust/Tauri build evidence is BLOCKED" >&2
  exit 1
fi

if [[ "$RUN_BUILD" -eq 1 ]]; then
  echo "== Tauri build =="
  need_cmd node
  need_cmd npm
  (cd desktop && npm ci)
  (cd desktop && npm run tauri build)
fi

echo "== Release verification complete =="
