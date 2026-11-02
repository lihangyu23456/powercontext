#!/usr/bin/env bash
# Run the end to end verification of the MiniMax database assistant.
#
#   ./verify/run_verification.sh            # offline, reproducible
#   MINIMAX_API_KEY=... ./verify/run_verification.sh   # against the real endpoint
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
LOG_DIR="${MINIMAX_MCP_LOG_DIR:-$ROOT/logs}"
PYTHON="${PYTHON:-python3}"
mkdir -p "$LOG_DIR"
export MINIMAX_MCP_LOG_DIR="$LOG_DIR"
# Applying the repair plan writes to the database, so the verification turns the
# write guard on for the whole run.
export MINIMAX_MCP_ALLOW_WRITES="${MINIMAX_MCP_ALLOW_WRITES:-1}"

# The offline stand in keeps the run reproducible without a key or a network.
if [ -z "${MINIMAX_API_KEY:-}" ] && [ -z "${MINIMAX_OFFLINE_BASE_URL:-}" ]; then
  PORT="${OFFLINE_PORT:-8123}"
  "$PYTHON" "$HERE/offline_minimax.py" > "$LOG_DIR/offline_minimax.log" 2>&1 &
  OFFLINE_PID=$!
  trap 'kill $OFFLINE_PID 2>/dev/null' EXIT
  sleep 1
  export MINIMAX_OFFLINE_BASE_URL="http://127.0.0.1:$PORT/v1"
  echo "using the offline MiniMax endpoint on 127.0.0.1:$PORT"
fi

echo "=== 0. reset the demo data ==="
if [ "${MINIMAX_MCP_SKIP_DEMO_DATA:-0}" != "1" ]; then
  "$PYTHON" - <<'PY' || exit 1
import pymysql

connection = pymysql.connect(
    unix_socket="/home/hsx/seekdb/run/sql.sock",
    user="root",
    password="",
    database="oceanbase",
    autocommit=True,
)
with connection.cursor() as cursor:
    cursor.execute("DROP DATABASE IF EXISTS minimax_demo")
    cursor.execute("CREATE DATABASE minimax_demo")
    cursor.execute("USE minimax_demo")
    cursor.execute(
        """
        CREATE TABLE orders (
          id BIGINT NOT NULL PRIMARY KEY,
          customer_id BIGINT NOT NULL,
          status VARCHAR(16) NOT NULL,
          amount DECIMAL(12, 2) NOT NULL,
          created_at DATETIME NOT NULL
        )
        """
    )
    rows = []
    for index in range(20000):
        status = ("open", "paid", "cancelled")[index % 3]
        rows.append((index, index % 500, status, round(index * 1.5, 2), "2026-10-01 00:00:00"))
    cursor.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)", rows)
    cursor.execute("ANALYZE TABLE orders")
    cursor.execute("SELECT COUNT(*) FROM orders")
    print(f"   orders rows: {cursor.fetchone()[0]}")
connection.close()
PY
fi

echo "=== 1. end to end verification ==="
"$PYTHON" "$HERE/verify_end_to_end.py"
STATUS=$?

echo "=== 2. the server answers over MCP ==="
"$PYTHON" "$ROOT/verify/mcp_smoke_test.py"
SMOKE=$?

echo "=== 3. the recorded model calls ==="
if [ -f "$LOG_DIR/minimax_calls.jsonl" ]; then
  "$PYTHON" - "$LOG_DIR/minimax_calls.jsonl" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    calls = [json.loads(line) for line in handle if line.strip()]
print(f"{len(calls)} recorded MiniMax call(s)")
for call in calls:
    print(f"  tool={call['tool']} model={call['model']} status={call.get('http_status')}")
    print(f"  prompt: {call['user_prompt'][:100]}")
PY
else
  echo "no model call was recorded"
  SMOKE=1
fi

echo "verification status: $STATUS, smoke status: $SMOKE"
[ "$STATUS" -eq 0 ] && [ "$SMOKE" -eq 0 ]
