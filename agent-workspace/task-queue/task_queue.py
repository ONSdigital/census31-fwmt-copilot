#!/usr/bin/env python3
"""Persist and inspect structured workflow events."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY,
    command TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'reported', 'unsupported', 'failed', 'pushed-awaiting-ci')),
    report TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_running
    ON workflow_events(status) WHERE status = 'running';
"""


class IdempotencyConflict(ValueError):
    pass


class TaskQueue:
    def __init__(self, database):
        self.database = Path(database)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _event(row):
        if row is None:
            return None
        event = dict(row)
        event["payload"] = json.loads(event["payload"])
        event["report"] = json.loads(event["report"]) if event["report"] else None
        return event

    def enqueue_event(self, command, event_type, payload, idempotency_key):
        if (not isinstance(command, str) or not command.strip()
                or not isinstance(event_type, str) or not event_type.strip()
                or not isinstance(payload, dict) or not isinstance(idempotency_key, str)
                or not idempotency_key.strip() or len(idempotency_key) > 255):
            raise ValueError("Invalid workflow event")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM workflow_events WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    if (existing["command"], existing["event_type"], existing["payload"]) != (
                            command, event_type, serialized):
                        raise IdempotencyConflict("Idempotency key already used for another event")
                    return existing["id"], False, existing["status"]
                cursor = connection.execute(
                    "INSERT INTO workflow_events (command, event_type, payload, idempotency_key) "
                    "VALUES (?, ?, ?, ?)", (command, event_type, serialized, idempotency_key),
                )
                return cursor.lastrowid, True, "pending"

    def claim_event(self):
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM workflow_events WHERE status = 'running' LIMIT 1"
                ).fetchone():
                    raise ValueError("an event is already running; recover after confirming the worker stopped")
                row = connection.execute(
                    "SELECT * FROM workflow_events WHERE status = 'pending' ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    "UPDATE workflow_events SET status = 'running', attempts = attempts + 1 WHERE id = ?",
                    (row["id"],),
                )
                return self._event(connection.execute(
                    "SELECT * FROM workflow_events WHERE id = ?", (row["id"],)
                ).fetchone())

    def finish_event(self, event_id, status, report):
        if status not in ("reported", "unsupported", "failed", "pushed-awaiting-ci") or not isinstance(report, dict):
            raise ValueError("Invalid event result")
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE workflow_events SET status = ?, report = ? WHERE id = ? AND status = 'running'",
                    (status, json.dumps(report, sort_keys=True), event_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Event is not running")

    def recover_event(self):
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE workflow_events SET status = 'failed', "
                    "report = ? WHERE status = 'running'",
                    (json.dumps({"reason": "Worker interrupted; inspect checkpoints and remote before retry"}),),
                )
                return cursor.rowcount

    def retry_event(self, event_id):
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE workflow_events SET status = 'pending', report = NULL "
                    "WHERE id = ? AND status = 'failed'", (event_id,),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Event is not failed")

    def get_event(self, event_id):
        with closing(self._connect()) as connection:
            return self._event(connection.execute(
                "SELECT * FROM workflow_events WHERE id = ?", (event_id,)
            ).fetchone())

    def list_events(self):
        with closing(self._connect()) as connection:
            return [self._event(row) for row in connection.execute(
                "SELECT * FROM workflow_events ORDER BY id"
            )]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-db", type=Path, required=True, help="SQLite file on persistent storage")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-events", help="list structured workflow events and reports")
    commands.add_parser("recover-events", help="mark interrupted events failed for operator review")
    retry_event = commands.add_parser("retry-event", help="retry a reviewed failed event")
    retry_event.add_argument("event_id", type=int)
    args = parser.parse_args()

    try:
        queue = TaskQueue(args.queue_db)
        if args.command == "list-events":
            print(json.dumps(queue.list_events()))
        elif args.command == "recover-events":
            print(queue.recover_event())
        elif args.command == "retry-event":
            queue.retry_event(args.event_id)
    except (OSError, ValueError, sqlite3.Error) as error:
        print("Task queue error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())