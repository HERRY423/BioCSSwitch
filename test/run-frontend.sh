#!/usr/bin/env bash
# S0 frontend 层：语法检查 + Vitest 纯逻辑回归。无 node/npm → env-blocked。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
if ! command -v node >/dev/null 2>&1; then
  echo "S0_LAYER frontend env-blocked (no node)"; exit 0
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "S0_LAYER frontend env-blocked (no npm)"; exit 0
fi
fail=0
for f in desktop/src/main.js desktop/src/ui-logic.js desktop/src/ui-logic.test.js; do
  if node --check "$f"; then echo "ok - node --check $f"; else echo "NOT ok - $f"; fail=1; fi
done
if [ ! -x desktop/node_modules/.bin/vitest ]; then
  echo "NOT ok - frontend dependencies missing; run npm ci in desktop"
  fail=1
elif (cd desktop && npm test); then
  echo "ok - Vitest"
else
  echo "NOT ok - Vitest"
  fail=1
fi
if [ "$fail" -eq 0 ]; then echo "S0_LAYER frontend pass"; exit 0; else echo "S0_LAYER frontend fail"; exit 1; fi
