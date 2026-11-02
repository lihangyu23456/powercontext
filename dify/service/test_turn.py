"""End to end check of the companion service.

    python service/test_turn.py

The check covers the three things the application depends on: the world setting
comes from seekdb, the memory comes from PowerContext, and the player's turn is
written back so the next session can recall it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = os.environ.get("NPC_SERVICE_URL", "http://127.0.0.1:8002")


def call(payload: dict) -> dict:
    request = urllib.request.Request(
        BASE.rstrip("/") + "/npc/turn",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    failures = []
    first = call(
        {
            "npc": "阿澜",
            "player": "tester",
            "scope": "npc-game-test",
            "message": "我把灯塔的路灯全关了",
        }
    )
    print("first turn ->", json.dumps(first, ensure_ascii=False)[:400])
    if "灯塔" not in first["setting"]:
        failures.append("the setting from seekdb is missing")
    if not isinstance(first["memories"], list):
        failures.append("the memory list is not a list")
    if "路灯" not in first["note"]:
        failures.append("the turn was not written into the memory")

    # The memory is written by the background processing, so the recall needs a
    # moment before the earlier turn shows up.
    recalled = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        second = call(
            {
                "npc": "阿澜",
                "player": "tester",
                "scope": "npc-game-test",
                "message": "灯塔的灯还亮着吗",
            }
        )
        recalled = second["memories"]
        if any("路灯" in text for text in recalled):
            break
        time.sleep(3)
    print("recalled ->", json.dumps(recalled, ensure_ascii=False)[:400])
    if not any("路灯" in text for text in recalled):
        failures.append("the earlier turn was not recalled from the memory")

    for problem in failures:
        print("FAIL:", problem)
    print("ok" if not failures else "failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
