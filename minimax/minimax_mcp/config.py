"""Configuration of the MiniMax database assistant MCP server.

Every setting can be given through the environment so the server can be started
without editing files. The defaults describe a local development setup: a
seekdb instance on a unix socket, a PowerContext server on localhost and the
MiniMax chat completion endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


@dataclass(frozen=True)
class SeekDBConfig:
    """Connection settings of the seekdb instance that holds the data."""

    unix_socket: str = field(default_factory=lambda: _env("MINIMAX_MCP_SEEKDB_SOCKET", "/home/hsx/seekdb/run/sql.sock"))
    host: str = field(default_factory=lambda: _env("MINIMAX_MCP_SEEKDB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("MINIMAX_MCP_SEEKDB_PORT", "2881")))
    user: str = field(default_factory=lambda: _env("MINIMAX_MCP_SEEKDB_USER", "root"))
    password: str = field(default_factory=lambda: _env("MINIMAX_MCP_SEEKDB_PASSWORD", ""))
    database: str = field(default_factory=lambda: _env("MINIMAX_MCP_SEEKDB_DATABASE", "oceanbase"))

    def connect_kwargs(self) -> dict[str, object]:
        if self.unix_socket:
            return {
                "unix_socket": self.unix_socket,
                "user": self.user,
                "password": self.password,
                "database": self.database,
                "charset": "utf8mb4",
                "autocommit": True,
            }
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "autocommit": True,
        }


@dataclass(frozen=True)
class PowerContextConfig:
    """Connection settings of the PowerContext server."""

    base_url: str = field(default_factory=lambda: _env("MINIMAX_MCP_POWERCONTEXT_URL", "http://127.0.0.1:8000"))
    api_key: str = field(default_factory=lambda: _env("MINIMAX_MCP_POWERCONTEXT_TOKEN", ""))
    timeout_seconds: float = field(default_factory=lambda: float(_env("MINIMAX_MCP_POWERCONTEXT_TIMEOUT", "30")))


@dataclass(frozen=True)
class MiniMaxConfig:
    """Settings of the MiniMax chat completion endpoint."""

    api_key: str = field(default_factory=lambda: _env("MINIMAX_API_KEY", ""))
    base_url: str = field(default_factory=lambda: _env("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"))
    model: str = field(default_factory=lambda: _env("MINIMAX_MODEL", "MiniMax-M2"))
    temperature: float = field(default_factory=lambda: float(_env("MINIMAX_TEMPERATURE", "0.2")))
    max_tokens: int = field(default_factory=lambda: int(_env("MINIMAX_MAX_TOKENS", "2048")))
    timeout_seconds: float = field(default_factory=lambda: float(_env("MINIMAX_TIMEOUT", "120")))
    # An offline endpoint keeps the verification reproducible without a key.
    offline_base_url: str = field(default_factory=lambda: _env("MINIMAX_OFFLINE_BASE_URL", ""))


@dataclass(frozen=True)
class LogConfig:
    """Where the MiniMax call records and the status log are written."""

    directory: Path = field(default_factory=lambda: Path(_env("MINIMAX_MCP_LOG_DIR", "logs")))

    @property
    def calls_file(self) -> Path:
        return self.directory / "minimax_calls.jsonl"

    @property
    def status_file(self) -> Path:
        return self.directory / "status.log"


@dataclass(frozen=True)
class Settings:
    """The complete configuration of the server."""

    seekdb: SeekDBConfig = field(default_factory=SeekDBConfig)
    powercontext: PowerContextConfig = field(default_factory=PowerContextConfig)
    minimax: MiniMaxConfig = field(default_factory=MiniMaxConfig)
    logs: LogConfig = field(default_factory=LogConfig)
    # Only read statements are allowed unless the caller opts in explicitly.
    allow_writes: bool = field(default_factory=lambda: _env("MINIMAX_MCP_ALLOW_WRITES", "0") == "1")

    def ensure_log_dir(self) -> Path:
        self.logs.directory.mkdir(parents=True, exist_ok=True)
        return self.logs.directory
