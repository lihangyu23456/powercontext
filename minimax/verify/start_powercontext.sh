#!/usr/bin/env bash
# Start a local PowerContext server for the MiniMax assistant verification.
#
# The server uses the local SQLite database by default and the offline mock
# model endpoint, so the run needs no key and no network.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
VENV=/home/hsx/.venvs/pc

export POWERCONTEXT_SERVER_HTTP__HOST="${POWERCONTEXT_SERVER_HTTP__HOST:-127.0.0.1}"
export POWERCONTEXT_SERVER_HTTP__PORT="${POWERCONTEXT_SERVER_HTTP__PORT:-8001}"
export POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL="${POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL:-openai-chat:gpt-4o-mini}"
export POWERCONTEXT_SERVER_INFERENCE_GENERATION_BASE_URL="${POWERCONTEXT_SERVER_INFERENCE_GENERATION_BASE_URL:-http://127.0.0.1:8000/v1}"
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL="${POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL:-openai:count-test-embedding}"
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BASE_URL="${POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BASE_URL:-http://127.0.0.1:8000/v1}"
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION="${POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION:-768}"
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID="${POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID:-count-test}"
# The memory is generated on a short schedule, so the verification does not have
# to wait for a long wave. The count threshold field is strict typed, so the
# schedule keeps the environment configuration simple.
export POWERCONTEXT_SERVER_RUNTIME_MEMORY_SCHEDULE_SECONDS="${POWERCONTEXT_SERVER_RUNTIME_MEMORY_SCHEDULE_SECONDS:-5}"

cd "$HERE"
# A fresh local database keeps the processing manifest consistent with the
# configuration above, so the runtime can bootstrap it on a fresh start.
if [ "${MINIMAX_MCP_KEEP_POWERCONTEXT_DB:-0}" != "1" ]; then
  rm -f /home/hsx/.local/share/powercontext/powercontext.db \
        /home/hsx/.local/share/powercontext/powercontext.db-wal \
        /home/hsx/.local/share/powercontext/powercontext.db-shm
fi
exec "$VENV/bin/python" -m uvicorn powercontext_app:app --host "$POWERCONTEXT_SERVER_HTTP__HOST" --port "$POWERCONTEXT_SERVER_HTTP__PORT" --log-level warning