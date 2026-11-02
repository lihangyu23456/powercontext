#!/usr/bin/env bash
# Measure the model size, the quantization effect and the local embedding cost.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="${NODE:-node}"
cd "$HERE"
if [ ! -d node_modules ]; then
  echo "=== install the measurement dependencies ==="
  "$NODE" -m npm install --no-audit --no-fund || exit 1
fi
echo "=== measure ==="
"$NODE" measure.mjs --json "$HERE/report.json"
