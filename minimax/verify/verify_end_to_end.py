"""End to end verification of the MiniMax database assistant.

The script walks the whole task the model is asked to solve: understand the
schema, collect the configuration and the performance data, ask MiniMax for a
repair, apply it and verify it. Every step is written to the log directory, so
the run leaves the evidence the submission asks for.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minimax_mcp.clients import build_clients  # noqa: E402
from minimax_mcp.config import Settings  # noqa: E402
from minimax_mcp.tools import DatabaseAssistant  # noqa: E402

ISSUE = "查询 orders 表中 status = 'open' 的订单越来越慢，请给出并验证修复方案。"


async def main() -> int:
    settings = Settings()
    database, powercontext, model, status = build_clients(settings)
    assistant = DatabaseAssistant(database, powercontext, model, status)

    print("== 1. database overview ==")
    overview = await assistant.database_overview()
    tables = overview["tables"]
    print(f"   tables: {[table['table_name'] for table in tables]}")

    print("== 2. metrics ==")
    metrics = await assistant.collect_metrics()
    print(f"   variables: {len(metrics['variables'])}, status: {len(metrics['status'])}")

    print("== 3. minimax advise ==")
    advice = await assistant.minimax_advise(
        ISSUE,
        context={"tables": tables[:3], "metrics": metrics},
    )
    plan = advice["plan"]
    print(f"   diagnosis: {plan.get('diagnosis', '')[:80]}")
    print(f"   statements: {len(plan.get('statements', []))}")

    print("== 4. apply fix ==")
    applied = await assistant.apply_fix(plan.get("statements", []))
    print(f"   applied: {applied['ok']}")

    print("== 5. verify fix ==")
    verified = await assistant.verify_fix(plan.get("verification", []))
    print(f"   verified: {verified['ok']}")

    print("== 6. powercontext capture and search ==")
    try:
        scope = os.environ.get("MINIMAX_MCP_POWERCONTEXT_SCOPE", "") or await powercontext.ensure_scope()
        print(f"   scope: {scope}")
        captured = await assistant.powercontext_capture(
            scope,
            "orders-note",
            "orders 表里 status 为 open 的订单增长很快，查询变慢，需要给 status 建索引。",
        )
        print(f"   captured: {captured.get('source_id', captured.get('source_ref', {}))}")
        # The memory is written by the background processing, so the search waits
        # for the first entry to appear.
        hits: list[Any] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            found = await assistant.powercontext_search(scope, "orders status 索引")
            hits = found.get("hits", found.get("entries", []))
            if hits:
                break
            await asyncio.sleep(2)
        print(f"   hits: {len(hits)}")
    except Exception as error:  # noqa: BLE001
        print(f"   skipped: {error}")

    print("\nlogs:")
    for path in sorted(settings.ensure_log_dir().glob("*")):
        print(f"   {path} ({path.stat().st_size} bytes)")
    return 0 if applied["ok"] and verified["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
