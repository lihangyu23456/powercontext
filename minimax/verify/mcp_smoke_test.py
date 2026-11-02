"""Check that the MCP server starts, lists its tools and answers one call."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "minimax_mcp.server"],
        env=env,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print(f"tools: {names}")
            result = await session.call_tool("database_overview", {})
            text = result.content[0].text
            payload = json.loads(text)
            if "error" in payload:
                print(f"database_overview failed: {payload['error']}")
                return 1
            print(f"tables: {[table['table_name'] for table in payload['tables']]}")
            return 0 if len(names) >= 7 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
