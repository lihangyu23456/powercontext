"""The companion service of the Dify NPC application.

Dify drives the conversation; this service gives it the two things it needs
before every reply: the world setting the NPC lives in (seekdb) and what this
player has done before (PowerContext memory).

The service is deliberately small and dependency light so it can be started next
to Dify with one command.

    python service/app.py --port 8002

It exposes one endpoint:

    POST /npc/turn
      {"npc": "阿澜", "player": "player-1", "scope": "npc-game",
       "message": "我把路灯全关了", "session": "s-42"}

      -> {"setting": "...", "memories": ["..."], "note": "..."}

The service writes the player's turn into the PowerContext memory as well, so
the NPC really does remember what happened in an earlier session.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SEEKDB_SOCKET = os.environ.get("NPC_SEEKDB_SOCKET", "/home/hsx/seekdb/run/sql.sock")
SEEKDB_HOST = os.environ.get("NPC_SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.environ.get("NPC_SEEKDB_PORT", "2881"))
SEEKDB_USER = os.environ.get("NPC_SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.environ.get("NPC_SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.environ.get("NPC_SEEKDB_DATABASE", "npc_world")
POWERCONTEXT_URL = os.environ.get("NPC_POWERCONTEXT_URL", "http://127.0.0.1:8000")
POWERCONTEXT_TOKEN = os.environ.get("NPC_POWERCONTEXT_TOKEN", "")


def seekdb_query(sql: str, parameters: tuple[Any, ...] = (), *, database: str | None = None) -> list[dict[str, Any]]:
    """Run one statement on seekdb and return the rows as dictionaries."""

    import pymysql

    kwargs: dict[str, Any] = {
        "user": SEEKDB_USER,
        "password": SEEKDB_PASSWORD,
        "charset": "utf8mb4",
        "autocommit": True,
    }
    kwargs["database"] = database if database is not None else SEEKDB_DATABASE
    if SEEKDB_SOCKET:
        kwargs["unix_socket"] = SEEKDB_SOCKET
    else:
        kwargs["host"] = SEEKDB_HOST
        kwargs["port"] = SEEKDB_PORT
    connection = pymysql.connect(**kwargs)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            if cursor.description is None:
                return []
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def powercontext_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call one PowerContext HTTP endpoint."""

    url = POWERCONTEXT_URL.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if POWERCONTEXT_TOKEN:
        request.add_header("Authorization", f"Bearer {POWERCONTEXT_TOKEN}")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def load_setting(npc: str, message: str) -> str:
    """Read the world setting from seekdb with a keyword search."""

    keywords = [word for word in message.replace("，", " ").replace("。", " ").split() if word][:4]
    rows: list[dict[str, Any]] = []
    try:
        if keywords:
            condition = " OR ".join(["setting LIKE %s"] * len(keywords))
            rows = seekdb_query(
                f"SELECT setting FROM npc_setting WHERE npc = %s AND ({condition}) LIMIT 3",
                (npc, *[f"%{word}%" for word in keywords]),
            )
        if not rows:
            rows = seekdb_query("SELECT setting FROM npc_setting WHERE npc = %s LIMIT 3", (npc,))
    except Exception as error:  # noqa: BLE001
        return f"（设定暂时取不到：{error}）"
    if not rows:
        return "（这个 NPC 还没有登记设定）"
    return "\n".join(str(row["setting"]) for row in rows)


def ensure_scope(scope: str) -> str:
    """Return the scope id of the named scope, creating it when it is missing.

    A scope is addressed by its id, while the application refers to it by a
    readable name, so the name is resolved to the id first.
    """

    try:
        request = urllib.request.Request(
            POWERCONTEXT_URL.rstrip("/") + "/v1/scopes?limit=200",
            method="GET",
        )
        if POWERCONTEXT_TOKEN:
            request.add_header("Authorization", f"Bearer {POWERCONTEXT_TOKEN}")
        with urllib.request.urlopen(request, timeout=30) as response:
            listing = json.loads(response.read().decode("utf-8"))
        for item in listing.get("items", []):
            if item.get("title") == scope:
                return str(item["scope_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        result = powercontext_request(
            "/v1/scopes",
            {"title": scope, "summary": scope, "idempotency_key": scope},
        )
        return str(result.get("scope_id") or scope)
    except Exception:
        return scope


def recall_memories(scope: str, message: str) -> list[str]:
    """Recall what this scope remembers about the player."""

    try:
        result = powercontext_request(
            "/v1/memory/search",
            {"scope_id": scope, "query": message, "limit": 5},
        )
    except Exception as error:  # noqa: BLE001
        return [f"（记忆暂时取不到：{error}）"]
    entries = result.get("entries") or result.get("hits") or []
    memories = []
    for entry in entries:
        if isinstance(entry, dict):
            text = entry.get("text") or entry.get("content") or json.dumps(entry, ensure_ascii=False)
        else:
            text = str(entry)
        memories.append(text)
    return memories


def remember_turn(scope: str, player: str, npc: str, message: str) -> str:
    """Write the player's turn into the memory so the next session can recall it."""

    source_id = f"{player}-{abs(hash(message)) % 10**8}"
    content = f"玩家 {player} 对 NPC {npc} 说：{message}"
    try:
        powercontext_request(
            "/v1/sources/content",
            {"scope_id": scope, "source_id": source_id, "content": content},
        )
    except Exception as error:  # noqa: BLE001
        return f"（这一轮没能写进记忆：{error}）"
    return content


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: ANN001, ARG002
        return

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/npc/turn":
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send({"error": "invalid json"}, 400)
            return
        npc = str(payload.get("npc") or "阿澜")
        player = str(payload.get("player") or "player")
        scope = str(payload.get("scope") or "npc-game")
        message = str(payload.get("message") or "")
        scope = ensure_scope(scope)
        setting = load_setting(npc, message)
        memories = recall_memories(scope, message)
        note = remember_turn(scope, player, npc, message)
        self._send({"setting": setting, "memories": memories, "note": note})

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="The companion service of the Dify NPC application.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer((arguments.host, arguments.port), Handler)
    server.daemon_threads = True
    print(f"npc companion service on {arguments.host}:{arguments.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
