"""Clients of the database, of PowerContext and of the MiniMax model endpoint."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pymysql

from minimax_mcp.config import MiniMaxConfig, PowerContextConfig, SeekDBConfig, Settings


class DatabaseError(RuntimeError):
    """The database refused a statement or the connection failed."""


def _ensure_powercontext_importable() -> None:
    """Make the PowerContext SDK importable from a local checkout.

    ``MINIMAX_MCP_POWERCONTEXT_SRC`` points at the ``src`` directory of a
    PowerContext checkout, which lets the server run against a local build
    without installing the package first.
    """

    source = os.environ.get("MINIMAX_MCP_POWERCONTEXT_SRC", "")
    if not source:
        return
    if source not in sys.path:
        sys.path.insert(0, source)


class ModelError(RuntimeError):
    """The model endpoint refused a request or answered without content."""


class SeekDBClient:
    """A thin read oriented wrapper around the seekdb MySQL protocol."""

    WRITE_PREFIXES = ("insert", "update", "delete", "replace", "truncate", "create", "drop", "alter", "grant", "revoke")

    def __init__(self, config: SeekDBConfig, *, allow_writes: bool = False) -> None:
        self._config = config
        self._allow_writes = allow_writes

    def _connect(self) -> pymysql.connections.Connection:
        try:
            return pymysql.connect(**self._config.connect_kwargs())  # type: ignore[arg-type]
        except Exception as error:  # noqa: BLE001
            raise DatabaseError(f"cannot connect to seekdb: {error}") from error

    def _check_writable(self, sql: str) -> None:
        if self._allow_writes:
            return
        head = sql.strip().lower()
        if head.startswith(self.WRITE_PREFIXES):
            raise DatabaseError("write statements are disabled, set MINIMAX_MCP_ALLOW_WRITES=1 to enable them")

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run one statement and return its rows as dictionaries."""

        self._check_writable(sql)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                if cursor.description is None:
                    return []
                names = [column[0] for column in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
        except DatabaseError:
            raise
        except Exception as error:  # noqa: BLE001
            raise DatabaseError(f"statement failed: {error}") from error
        finally:
            connection.close()

    def list_tables(self) -> list[dict[str, Any]]:
        """Return every user table of the current database with its row estimate."""

        return self.query(
            "SELECT table_name, table_rows, table_type, engine, table_comment "
            "FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY table_name"
        )

    def describe_table(self, table: str) -> list[dict[str, Any]]:
        """Return the columns of one table."""

        return self.query(
            "SELECT column_name, column_type, is_nullable, column_key, column_default, column_comment "
            "FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )

    def list_indexes(self, table: str) -> list[dict[str, Any]]:
        """Return the indexes of one table."""

        return self.query(
            "SELECT index_name, seq_in_index, column_name, non_unique, index_type "
            "FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = %s ORDER BY index_name, seq_in_index",
            (table,),
        )

    def collect_metrics(self) -> dict[str, Any]:
        """Collect the configuration and the runtime counters of the instance."""

        variables = {}
        for name in (
            "ob_query_timeout",
            "ob_trx_timeout",
            "ob_sql_work_area_percentage",
            "max_connections",
            "innodb_buffer_pool_size",
            "ob_enable_px_plan_dupelim",
        ):
            rows = self.query(f"SHOW VARIABLES LIKE '{name}'")
            if rows:
                variables[name] = next(iter(rows[0].values()))
        counters = {}
        for name in ("Queries", "Com_select", "Com_insert", "Com_update", "Com_delete", "Threads_connected"):
            rows = self.query(f"SHOW GLOBAL STATUS LIKE '{name}'")
            if rows:
                counters[name] = next(iter(rows[0].values()))
        return {"variables": variables, "status": counters, "captured_at": datetime.now(UTC).isoformat()}


@dataclass
class PowerContextClient:
    """A wrapper around the PowerContext HTTP API."""

    config: PowerContextConfig

    async def search_memory(self, scope_id: str, query: str, *, limit: int = 5) -> dict[str, Any]:
        """Search the memory of one scope."""

        _ensure_powercontext_importable()
        from powercontext.client import PowerContextClient
        from powercontext.client.client import SearchMemoryRequest

        async with PowerContextClient(
            self.config.base_url,
            token=self.config.api_key or None,
            timeout=self.config.timeout_seconds,
        ) as client:
            request = SearchMemoryRequest(scope_id=scope_id, query=query, limit=limit)
            response = await client.search_memory(request)
            return response.model_dump(mode="json")

    async def list_scopes(self) -> list[dict[str, Any]]:
        """List the scopes the server exposes."""

        _ensure_powercontext_importable()
        from powercontext.client import PowerContextClient

        async with PowerContextClient(
            self.config.base_url,
            token=self.config.api_key or None,
            timeout=self.config.timeout_seconds,
        ) as client:
            page = await client.list_scopes()
            return page.model_dump(mode="json")

    async def ensure_scope(self, title: str = "MiniMax demo", summary: str = "Scope used by the MiniMax assistant verification.") -> str:
        """Return one usable scope, creating it when the server has none."""

        _ensure_powercontext_importable()
        from powercontext.client import PowerContextClient
        from powercontext.client.client import CreateScopeRequest

        async with PowerContextClient(
            self.config.base_url,
            token=self.config.api_key or None,
            timeout=self.config.timeout_seconds,
        ) as client:
            page = await client.list_scopes()
            scopes = page.model_dump(mode="json").get("scopes", [])
            if scopes:
                return scopes[0].get("scope_id") or scopes[0].get("id")
            request = CreateScopeRequest(
                title=title,
                summary=summary,
                idempotency_key="minimax-assistant-demo",
            )
            created = await client.create_scope(request)
            return created.scope_id

    async def capture_source(self, scope_id: str, source_id: str, content: str) -> dict[str, Any]:
        """Write one content source into a scope so the memory can use it."""

        _ensure_powercontext_importable()
        from powercontext.client import PowerContextClient
        from powercontext.client.client import CaptureContentSourceRequest

        async with PowerContextClient(
            self.config.base_url,
            token=self.config.api_key or None,
            timeout=self.config.timeout_seconds,
        ) as client:
            request = CaptureContentSourceRequest(scope_id=scope_id, source_id=source_id, content=content)
            response = await client.capture_content_source(request)
            return response.model_dump(mode="json")


class MiniMaxClient:
    """Calls the MiniMax chat completion endpoint and records every call."""

    def __init__(self, config: MiniMaxConfig, *, log_dir: Path) -> None:
        self._config = config
        self._log_dir = log_dir

    @property
    def _endpoint(self) -> str:
        base = self._config.offline_base_url or self._config.base_url
        return base.rstrip("/") + "/chat/completions"

    def _record(self, payload: dict[str, Any]) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        with (self._log_dir / "minimax_calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def complete(self, system_prompt: str, user_prompt: str, *, tool_name: str) -> str:
        """Ask the model for one answer and record the request and the answer."""

        started = time.monotonic()
        body = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        record: dict[str, Any] = {
            "tool": tool_name,
            "model": self._config.model,
            "endpoint": self._endpoint,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "request_body": body,
            "requested_at": datetime.now(UTC).isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                response = await client.post(self._endpoint, json=body, headers=headers)
                record["http_status"] = response.status_code
                response.raise_for_status()
                payload = response.json()
        except Exception as error:  # noqa: BLE001
            record["error"] = f"{type(error).__name__}: {error}"
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self._record(record)
            raise ModelError(f"the model endpoint failed: {error}") from error
        record["response"] = payload
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        self._record(record)
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelError("the model endpoint answered without content") from error


class StatusLog:
    """Writes the status log of every tool call."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def write(self, tool: str, status: str, detail: str = "") -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now(UTC).isoformat()} tool={tool} status={status}"
        if detail:
            line += f" detail={detail}"
        with (self._directory / "status.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def build_clients(settings: Settings) -> tuple[SeekDBClient, PowerContextClient, MiniMaxClient, StatusLog]:
    """Build every client of the server from one configuration."""

    log_dir = settings.ensure_log_dir()
    return (
        SeekDBClient(settings.seekdb, allow_writes=settings.allow_writes),
        PowerContextClient(settings.powercontext),
        MiniMaxClient(settings.minimax, log_dir=log_dir),
        StatusLog(log_dir),
    )
