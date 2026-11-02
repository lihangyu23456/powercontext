"""A deterministic offline stand in for the MiniMax chat completion endpoint.

The verification must be reproducible without a network or an API key, so this
server answers the same request with the same repair plan every time. Point
``MINIMAX_OFFLINE_BASE_URL`` at it to record a run offline; the recorded
requests are the real prompts the server sends to the model.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAN = {
    "diagnosis": "The table keeps the filter column without an index, so every statement reads the whole table.",
    "evidence": [
        "information_schema.statistics has no index on the filter column.",
        "SHOW GLOBAL STATUS shows a growing Com_select without a matching range access.",
    ],
    "statements": [
        {
            "sql": "CREATE INDEX idx_orders_customer_status ON orders (customer_id, status)",
            "purpose": "Give the filter column an index so the statement can use a range access.",
        }
    ],
    "verification": [
        "EXPLAIN SELECT id FROM orders WHERE status = 'open'",
        "SELECT COUNT(*) FROM orders WHERE status = 'open'",
    ],
    "risk": "The index slows down writes a little and needs disk space.",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: ANN001, ARG002
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            request = {}
        answer = json.dumps(PLAN, ensure_ascii=False)
        body = json.dumps(
            {
                "id": "offline-minimax",
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model", "offline"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.environ.get("PORT", "8123"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"offline minimax endpoint on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
