"""The MCP server that lets a MiniMax model work on seekdb and PowerContext.

The server speaks the Model Context Protocol over stdio, so any MCP client can
attach it. Every tool is thin: the work happens in the clients, the model call
and the status log are the only side effects.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from minimax_mcp.clients import build_clients
from minimax_mcp.config import Settings
from minimax_mcp.tools import DatabaseAssistant

SERVER_NAME = "minimax-database-assistant"
INSTRUCTIONS = """让 MiniMax 模型可以直接诊断并修复 seekdb / OceanBase 上的问题，
并通过 PowerContext 检索记忆。

典型流程：
1. database_overview  —— 了解结构
2. collect_metrics / run_query —— 拿到配置与性能数据
3. minimax_advise —— 让 MiniMax 给出修复方案
4. apply_fix —— 执行修复
5. verify_fix —— 验证修复
"""


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties, "required": required},
    )


TOOLS = [
    _tool(
        "database_overview",
        "列出当前库的所有表、列与索引，用于了解数据库结构。",
        {"include_indexes": {"type": "boolean", "description": "是否附带索引信息，默认 true"}},
        [],
    ),
    _tool(
        "run_query",
        "执行一条只读 SQL 并返回结果行，用于取性能或配置数据。",
        {
            "sql": {"type": "string", "description": "要执行的只读 SQL"},
            "limit": {"type": "integer", "description": "最多返回多少行，默认 200"},
        },
        ["sql"],
    ),
    _tool(
        "collect_metrics",
        "采集实例的运行配置与状态计数器，返回 variables 与 status。",
        {},
        [],
    ),
    _tool(
        "minimax_advise",
        "把问题描述与上下文交给 MiniMax 模型，得到结构化的修复方案。",
        {
            "issue": {"type": "string", "description": "问题描述"},
            "context": {"type": "object", "description": "补充上下文，例如结构或指标"},
        },
        ["issue"],
    ),
    _tool(
        "apply_fix",
        "执行修复方案里的语句，逐条返回执行结果。",
        {
            "statements": {
                "type": "array",
                "description": "语句列表，每项为 {\"sql\": ..., \"purpose\": ...}",
                "items": {"type": "object"},
            }
        },
        ["statements"],
    ),
    _tool(
        "verify_fix",
        "执行验证语句，确认修复是否生效。",
        {
            "checks": {
                "type": "array",
                "description": "验证语句列表",
                "items": {"type": "string"},
            }
        },
        ["checks"],
    ),
    _tool(
        "powercontext_search",
        "通过 PowerContext 检索某个 scope 的记忆。",
        {
            "scope_id": {"type": "string", "description": "scope 标识"},
            "query": {"type": "string", "description": "检索词"},
            "limit": {"type": "integer", "description": "返回条数，默认 5"},
        },
        ["scope_id", "query"],
    ),
    _tool(
        "powercontext_capture",
        "把一段内容写入某个 scope 的来源，让记忆可以用它。",
        {
            "scope_id": {"type": "string", "description": "scope 标识"},
            "source_id": {"type": "string", "description": "来源标识"},
            "content": {"type": "string", "description": "来源正文"},
        },
        ["scope_id", "source_id", "content"],
    ),
]


async def serve(settings: Settings | None = None) -> None:
    """Run the MCP server on stdio until the client disconnects."""

    resolved = settings or Settings()
    database, powercontext, model, status = build_clients(resolved)
    assistant = DatabaseAssistant(database, powercontext, model, status)
    server = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            if name == "database_overview":
                result = await assistant.database_overview(
                    include_indexes=bool(arguments.get("include_indexes", True))
                )
            elif name == "run_query":
                result = await assistant.run_query(
                    str(arguments["sql"]), limit=int(arguments.get("limit", 200))
                )
            elif name == "collect_metrics":
                result = await assistant.collect_metrics()
            elif name == "minimax_advise":
                result = await assistant.minimax_advise(
                    str(arguments["issue"]), context=arguments.get("context")
                )
            elif name == "apply_fix":
                result = await assistant.apply_fix(list(arguments.get("statements") or []))
            elif name == "verify_fix":
                result = await assistant.verify_fix(list(arguments.get("checks") or []))
            elif name == "powercontext_search":
                result = await assistant.powercontext_search(
                    str(arguments["scope_id"]),
                    str(arguments["query"]),
                    limit=int(arguments.get("limit", 5)),
                )
            elif name == "powercontext_capture":
                result = await assistant.powercontext_capture(
                    str(arguments["scope_id"]),
                    str(arguments["source_id"]),
                    str(arguments["content"]),
                )
            else:
                raise ValueError(f"unknown tool {name}")
        except Exception as error:  # noqa: BLE001
            return [TextContent(type="text", text=json.dumps({"error": str(error)}, ensure_ascii=False))]
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """The entry point of the ``minimax-mcp`` command."""

    asyncio.run(serve())


if __name__ == "__main__":
    main()
