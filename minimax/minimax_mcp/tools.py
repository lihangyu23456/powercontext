"""The tools of the MiniMax database assistant MCP server.

Every tool answers one part of the task the model has to solve: understand the
schema, collect the configuration and the performance data, ask MiniMax for a
repair, apply the repair and verify it. The model call, the prompt and the
result of every step are written to the log directory so the run can be
reviewed afterwards.
"""

from __future__ import annotations

import json
from typing import Any

from minimax_mcp.clients import DatabaseError, MiniMaxClient, PowerContextClient, SeekDBClient, StatusLog

REPAIR_SYSTEM_PROMPT = """你是一名 OceanBase / seekdb 数据库专家。
你会收到一个数据库的结构信息、性能或配置数据，以及一条问题描述。
请给出可执行的修复方案，输出严格的 JSON：
{
  "diagnosis": "问题根因",
  "evidence": ["支撑根因的观察"],
  "statements": [{"sql": "修复语句", "purpose": "这条语句的作用"}],
  "verification": ["修复后如何验证"],
  "risk": "修复的风险"
}
不要输出 JSON 以外的任何内容。"""


class DatabaseAssistant:
    """Implements the tools that the MCP server exposes."""

    def __init__(
        self,
        database: SeekDBClient,
        powercontext: PowerContextClient,
        model: MiniMaxClient,
        status: StatusLog,
    ) -> None:
        self._database = database
        self._powercontext = powercontext
        self._model = model
        self._status = status

    async def database_overview(self, *, include_indexes: bool = True) -> dict[str, Any]:
        """Show every table of the current database with its columns and indexes."""

        self._status.write("database_overview", "start")
        try:
            tables = self._database.list_tables()
            for table in tables:
                name = table.get("table_name")
                table["columns"] = self._database.describe_table(name)
                if include_indexes:
                    table["indexes"] = self._database.list_indexes(name)
        except DatabaseError as error:
            self._status.write("database_overview", "failed", str(error))
            raise
        self._status.write("database_overview", "ok", f"{len(tables)} tables")
        return {"tables": tables}

    async def run_query(self, sql: str, *, limit: int = 200) -> dict[str, Any]:
        """Run one read only statement and return the rows."""

        self._status.write("run_query", "start", sql[:80])
        try:
            rows = self._database.query(sql)
        except DatabaseError as error:
            self._status.write("run_query", "failed", str(error))
            raise
        self._status.write("run_query", "ok", f"{len(rows)} rows")
        return {"sql": sql, "row_count": len(rows), "rows": rows[:limit]}

    async def collect_metrics(self) -> dict[str, Any]:
        """Collect the configuration and the runtime counters of the instance."""

        self._status.write("collect_metrics", "start")
        try:
            metrics = self._database.collect_metrics()
        except DatabaseError as error:
            self._status.write("collect_metrics", "failed", str(error))
            raise
        self._status.write("collect_metrics", "ok")
        return metrics

    async def minimax_advise(self, issue: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask MiniMax for a repair plan of the described issue."""

        self._status.write("minimax_advise", "start", issue[:80])
        user_prompt = json.dumps(
            {"issue": issue, "context": context or {}},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        answer = await self._model.complete(REPAIR_SYSTEM_PROMPT, user_prompt, tool_name="minimax_advise")
        plan = _parse_plan(answer)
        self._status.write("minimax_advise", "ok", f"{len(plan.get('statements', []))} statements")
        return {"plan": plan, "raw_answer": answer}

    async def apply_fix(self, statements: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply the statements of a repair plan one by one."""

        self._status.write("apply_fix", "start", f"{len(statements)} statements")
        results = []
        for statement in statements:
            sql = statement.get("sql") if isinstance(statement, dict) else str(statement)
            if not sql:
                continue
            try:
                rows = self._database.query(sql)
                results.append({"sql": sql, "ok": True, "row_count": len(rows)})
            except DatabaseError as error:
                results.append({"sql": sql, "ok": False, "error": str(error)})
                self._status.write("apply_fix", "failed", sql[:80])
                return {"applied": results, "ok": False}
        self._status.write("apply_fix", "ok", f"{len(results)} statements")
        return {"applied": results, "ok": True}

    async def verify_fix(self, checks: list[str]) -> dict[str, Any]:
        """Re-run the verification statements of a repair plan."""

        self._status.write("verify_fix", "start", f"{len(checks)} checks")
        results = []
        for sql in checks:
            try:
                rows = self._database.query(sql)
                results.append({"sql": sql, "ok": True, "row_count": len(rows), "rows": rows[:20]})
            except DatabaseError as error:
                results.append({"sql": sql, "ok": False, "error": str(error)})
        self._status.write("verify_fix", "ok", f"{len(results)} checks")
        return {"checks": results, "ok": all(item["ok"] for item in results)}

    async def powercontext_search(self, scope_id: str, query: str, *, limit: int = 5) -> dict[str, Any]:
        """Search the memory of one scope through PowerContext."""

        self._status.write("powercontext_search", "start", scope_id)
        result = await self._powercontext.search_memory(scope_id, query, limit=limit)
        self._status.write("powercontext_search", "ok")
        return result

    async def powercontext_capture(self, scope_id: str, source_id: str, content: str) -> dict[str, Any]:
        """Write one content source into a scope so the memory can use it."""

        self._status.write("powercontext_capture", "start", scope_id)
        result = await self._powercontext.capture_source(scope_id, source_id, content)
        self._status.write("powercontext_capture", "ok")
        return result


def _parse_plan(answer: str) -> dict[str, Any]:
    """Read the repair plan of the model, tolerating a fenced code block."""

    text = answer.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"diagnosis": answer, "evidence": [], "statements": [], "verification": [], "risk": ""}
    if not isinstance(parsed, dict):
        return {"diagnosis": answer, "evidence": [], "statements": [], "verification": [], "risk": ""}
    return parsed
