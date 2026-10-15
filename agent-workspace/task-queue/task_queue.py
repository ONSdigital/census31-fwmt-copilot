#!/usr/bin/env python3
"""Persist and inspect a single-worker queue of agent task prompts."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    prompt TEXT NOT NULL CHECK (length(trim(prompt)) > 0),
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_next ON tasks(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_running ON tasks(status) WHERE status = 'running';
"""


class IdempotencyConflict(ValueError):
    pass


class TaskQueue:
    def __init__(self, database):
        self.database = Path(database)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            with connection:
                columns = {column["name"] for column in connection.execute("PRAGMA table_info(tasks)")}
                if "idempotency_key" not in columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency_key "
                    "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
                )

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def enqueue(self, prompt, idempotency_key=None):
        return self.enqueue_once(prompt, idempotency_key)[0]

    def enqueue_once(self, prompt, idempotency_key=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task prompt must not be blank")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255
        ):
            raise ValueError("idempotency key must be a nonblank string of at most 255 characters")
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if idempotency_key is not None:
                    existing = connection.execute(
                        "SELECT id, prompt, status FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
                    ).fetchone()
                    if existing is not None:
                        if existing["prompt"] != prompt:
                            raise IdempotencyConflict("idempotency key already belongs to a different prompt")
                        return existing["id"], False, existing["status"]
                cursor = connection.execute(
                    "INSERT INTO tasks (prompt, idempotency_key) VALUES (?, ?)", (prompt, idempotency_key)
                )
                return cursor.lastrowid, True, "pending"

    def claim(self):
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM tasks WHERE status = 'running' LIMIT 1").fetchone():
                    raise ValueError("a task is already running; recover it after confirming the worker stopped")
                task = connection.execute(
                    "SELECT id FROM tasks WHERE status = 'pending' ORDER BY id LIMIT 1"
                ).fetchone()
                if task is None:
                    return None
                connection.execute(
                    "UPDATE tasks SET status = 'running', attempts = attempts + 1, "
                    "started_at = CURRENT_TIMESTAMP, finished_at = NULL, last_error = NULL WHERE id = ?",
                    (task["id"],),
                )
                return dict(connection.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone())

    def _finish(self, task_id, status, error=None):
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE tasks SET status = ?, finished_at = CURRENT_TIMESTAMP, last_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (status, error, task_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("task is not running: {}".format(task_id))

    def complete(self, task_id):
        self._finish(task_id, "done")

    def fail(self, task_id, error):
        if not isinstance(error, str) or not error.strip():
            raise ValueError("failure reason must not be blank")
        self._finish(task_id, "failed", error)

    def retry(self, task_id):
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE tasks SET status = 'pending', started_at = NULL, finished_at = NULL, "
                    "last_error = NULL WHERE id = ? AND status = 'failed'",
                    (task_id,),
                )
                if cursor.rowcount != 1:
                    raise ValueError("task is not failed: {}".format(task_id))

    def recover(self):
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE tasks SET status = 'failed', finished_at = CURRENT_TIMESTAMP, "
                    "last_error = 'Worker interrupted; review before retrying' WHERE status = 'running'"
                )
                return cursor.rowcount

    def list_tasks(self):
        with closing(self._connect()) as connection:
            return [dict(task) for task in connection.execute("SELECT * FROM tasks ORDER BY id")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-db", type=Path, required=True, help="SQLite file on persistent storage")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("claim", help="claim the oldest pending task")
    commands.add_parser("recover", help="mark an interrupted running task as failed")
    commands.add_parser("list", help="list tasks as JSON")
    enqueue = commands.add_parser("enqueue", help="append a task prompt")
    enqueue.add_argument("prompt")
    enqueue.add_argument("--idempotency-key")
    for command in ("done", "retry"):
        action = commands.add_parser(command)
        action.add_argument("task_id", type=int)
    fail = commands.add_parser("fail")
    fail.add_argument("task_id", type=int)
    fail.add_argument("reason")
    args = parser.parse_args()

    try:
        queue = TaskQueue(args.queue_db)
        if args.command == "enqueue":
            print(queue.enqueue(args.prompt, args.idempotency_key))
        elif args.command == "claim":
            task = queue.claim()
            if task is not None:
                print(json.dumps(task))
        elif args.command == "done":
            queue.complete(args.task_id)
        elif args.command == "fail":
            queue.fail(args.task_id, args.reason)
        elif args.command == "retry":
            queue.retry(args.task_id)
        elif args.command == "recover":
            print(queue.recover())
        elif args.command == "list":
            print(json.dumps(queue.list_tasks()))
    except (OSError, ValueError, sqlite3.Error) as error:
        print("Task queue error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())