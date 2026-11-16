"""Continuous writer: proves exports never block normal source writes.

Issues one small committed statement every ~0.1-0.3s (inserts of users,
projects, tasks; updates of task titles and user names) and logs each commit.
Ids come from the explicit `writer_seq` sequence, so columns keep no defaults.
"""

import os
import random
import time
from datetime import datetime, timezone

import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://exporter:exporter@localhost:5432/source"
)


def tick(conn, n: int) -> tuple[str, str]:
    op = random.choice(
        ["insert_user", "insert_project", "insert_task", "update_task", "update_user"]
    )
    with conn.cursor() as cur:
        if op == "insert_user":
            cur.execute(
                "INSERT INTO users (id, name, email, team_id)"
                " VALUES (nextval('writer_seq'), %s, NULL, %s) RETURNING id",
                (f"writer-user-{n}", random.choice([1, 2, None])),
            )
            detail = f"id={cur.fetchone()[0]}"
        elif op == "insert_project":
            cur.execute(
                "INSERT INTO projects (id, name, owner_id)"
                " SELECT nextval('writer_seq'), %s, id FROM users"
                " ORDER BY random() LIMIT 1 RETURNING id",
                (f"writer-proj-{n}",),
            )
            detail = f"id={cur.fetchone()[0]}"
        elif op == "insert_task":
            cur.execute(
                "INSERT INTO tasks (id, title, project_id, assignee_id, parent_id)"
                " SELECT nextval('writer_seq'), %s, p.id, NULL,"
                "   CASE WHEN random() < 0.5"
                "        THEN (SELECT id FROM tasks ORDER BY random() LIMIT 1) END"
                " FROM projects p ORDER BY random() LIMIT 1 RETURNING id",
                (f"writer-task-{n}",),
            )
            detail = f"id={cur.fetchone()[0]}"
        elif op == "update_task":
            cur.execute(
                "UPDATE tasks SET title = title || '.'"
                " WHERE id = (SELECT id FROM tasks ORDER BY random() LIMIT 1)"
            )
            detail = f"rows={cur.rowcount}"
        else:  # update_user
            cur.execute(
                "UPDATE users SET name = name || '~'"
                " WHERE id = (SELECT id FROM users ORDER BY random() LIMIT 1)"
            )
            detail = f"rows={cur.rowcount}"
    return op, detail


def main() -> None:
    n = 0
    while True:
        try:
            # autocommit: every tick is one self-contained committed statement.
            with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
                while True:
                    n += 1
                    op, detail = tick(conn, n)
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                    print(f"writer: committed #{n} op={op} {detail} at {ts}Z", flush=True)
                    time.sleep(random.uniform(0.1, 0.3))
        except psycopg.Error as exc:
            print(f"writer: reconnecting after error: {exc}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
