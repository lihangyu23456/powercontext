"""Self tests of the tool layer of the MiniMax database assistant."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minimax_mcp.tools import _parse_plan  # noqa: E402


def test_parse_plan_reads_plain_json() -> None:
    plan = _parse_plan('{"diagnosis": "no index", "statements": [{"sql": "CREATE INDEX i ON t (c)"}]}')
    assert plan["diagnosis"] == "no index"
    assert plan["statements"][0]["sql"] == "CREATE INDEX i ON t (c)"


def test_parse_plan_accepts_a_fenced_block() -> None:
    plan = _parse_plan('```json\n{"diagnosis": "fenced"}\n```')
    assert plan["diagnosis"] == "fenced"


def test_parse_plan_survives_a_broken_answer() -> None:
    plan = _parse_plan("the model did not answer with JSON")
    assert plan["statements"] == []
    assert plan["diagnosis"] == "the model did not answer with JSON"


def test_write_guard_blocks_statements() -> None:
    from minimax_mcp.clients import DatabaseError, SeekDBClient

    client = SeekDBClient.__new__(SeekDBClient)
    client._allow_writes = False
    client._check_writable("SELECT 1")
    try:
        client._check_writable("DROP TABLE orders")
    except DatabaseError as error:
        assert "write statements are disabled" in str(error)
    else:
        raise AssertionError("a write statement must be rejected")


def test_write_guard_allows_statements_when_enabled() -> None:
    from minimax_mcp.clients import SeekDBClient

    client = SeekDBClient.__new__(SeekDBClient)
    client._allow_writes = True
    client._check_writable("DROP TABLE orders")


def test_tools_are_declared() -> None:
    from minimax_mcp.server import TOOLS

    names = {tool.name for tool in TOOLS}
    assert names == {
        "database_overview",
        "run_query",
        "collect_metrics",
        "minimax_advise",
        "apply_fix",
        "verify_fix",
        "powercontext_search",
        "powercontext_capture",
    }
    for tool in TOOLS:
        assert tool.description
        assert tool.inputSchema["type"] == "object"
        json.dumps(tool.inputSchema)


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"{name}: ok")
    print("all tests passed")
