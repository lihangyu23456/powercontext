#!/usr/bin/env bash
# Check the companion service of the Dify NPC application end to end.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
VENV=/home/hsx/.venvs/pc

export NPC_SEEKDB_DATABASE="${NPC_SEEKDB_DATABASE:-npc_world}"
export NPC_POWERCONTEXT_URL="${NPC_POWERCONTEXT_URL:-http://127.0.0.1:8001}"

echo "=== seed the world setting into seekdb ==="
"$PYTHON" "$HERE/service/seed_world.py" || exit 1

echo "=== start the companion service ==="
"$PYTHON" "$HERE/service/app.py" --port "${NPC_SERVICE_PORT:-8002}" > /tmp/npc_service.log 2>&1 &
SERVICE_PID=$!
trap 'kill $SERVICE_PID 2>/dev/null' EXIT
sleep 3

echo "=== check one turn ==="
"$PYTHON" "$HERE/service/test_turn.py"
STATUS=$?
echo "=== service log ==="
tail -5 /tmp/npc_service.log
exit $STATUS
