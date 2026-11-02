#!/usr/bin/env bash
# Create the demo data the verification runs against.
set -uo pipefail
VENV=/home/hsx/.venvs/pc
"$VENV/bin/python" - <<'PY'
import pymysql

connection = pymysql.connect(
    unix_socket="/home/hsx/seekdb/run/sql.sock",
    user="root",
    password="",
    database="oceanbase",
    autocommit=True,
)
with connection.cursor() as cursor:
    cursor.execute("CREATE DATABASE IF NOT EXISTS minimax_demo")
    cursor.execute("USE minimax_demo")
    cursor.execute("DROP TABLE IF EXISTS orders")
    cursor.execute(
        """
        CREATE TABLE orders (
          id BIGINT NOT NULL PRIMARY KEY,
          customer_id BIGINT NOT NULL,
          status VARCHAR(16) NOT NULL,
          amount DECIMAL(12, 2) NOT NULL,
          created_at DATETIME NOT NULL
        )
        """
    )
    rows = []
    for index in range(20000):
        status = ("open", "paid", "cancelled")[index % 3]
        rows.append((index, index % 500, status, round(index * 1.5, 2), "2026-10-01 00:00:00"))
    cursor.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)", rows)
    cursor.execute("ANALYZE TABLE orders")
    cursor.execute("SELECT COUNT(*) FROM orders")
    print("orders rows:", cursor.fetchone()[0])
    cursor.execute("SELECT index_name FROM information_schema.statistics WHERE table_schema='minimax_demo' GROUP BY index_name")
    print("indexes:", [row[0] for row in cursor.fetchall()])
connection.close()
PY
