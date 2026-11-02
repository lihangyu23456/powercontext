"""Seed the world setting of the NPC into seekdb.

    python service/seed_world.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import seekdb_query  # noqa: E402

SETTING = [
    (
        "阿澜",
        "灯塔看守人阿澜，负责在雾夜里为船只点灯。她说话简短、克制，习惯先描述环境再回答问题。",
        "灯塔 雾 夜晚 航行",
    ),
    (
        "阿澜",
        "阿澜记得每一次被关掉的灯，也记得每一次因此迟到的船。她不会责怪任何人，但会把这件事记很久。",
        "灯 记忆 迟到 船",
    ),
    (
        "老陈",
        "码头的老陈修了四十年船坞，什么都见过，最怕别人把话说明白。他喜欢用反问回答问题。",
        "码头 船坞 修理 反问",
    ),
    (
        "小满",
        "镇上的信使小满，跑得比谁都快，但方向感极差。她的信送到过三个不同的港口。",
        "信使 送信 港口 方向",
    ),
]


def main() -> int:
    seekdb_query("CREATE DATABASE IF NOT EXISTS npc_world", database="oceanbase")
    seekdb_query("USE npc_world", database="oceanbase")
    seekdb_query(
        """
        CREATE TABLE IF NOT EXISTS npc_setting (
          npc       VARCHAR(64) NOT NULL,
          setting   TEXT NOT NULL,
          keywords  VARCHAR(256) NOT NULL,
          PRIMARY KEY (npc, keywords)
        )
        """
    )
    for npc, setting, keywords in SETTING:
        seekdb_query(
            "REPLACE INTO npc_setting(npc, setting, keywords) VALUES (%s, %s, %s)",
            (npc, setting, keywords),
        )
    rows = seekdb_query("SELECT npc, COUNT(*) AS n FROM npc_setting GROUP BY npc")
    print(f"seeded npc_setting: {rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
