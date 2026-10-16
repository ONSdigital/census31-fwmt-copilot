from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from task_queue import IdempotencyConflict, TaskQueue, main


SCRIPT = Path(__file__).with_name("task_queue.py")


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database = Path(temporary_directory.name) / "events.sqlite3"
        self.queue = TaskQueue(self.database)

    def test_new_database_contains_only_structured_event_table(self):
        with sqlite3.connect(self.database) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
        self.assertIn("workflow_events", tables)
        self.assertNotIn("tasks", tables)

    def test_existing_prompt_table_is_left_untouched(self):
        legacy = self.database.parent / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, prompt TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO tasks (prompt) VALUES ('Existing prompt')")

        TaskQueue(legacy)

        with sqlite3.connect(legacy) as connection:
            self.assertEqual(connection.execute("SELECT prompt FROM tasks").fetchall(), [("Existing prompt",)])

    def test_event_persists_payload_deduplicates_and_reports_terminal_result(self):
        payload = {"repository": "ONSdigital/common", "prNumber": 14, "headSha": "a" * 40}
        first = self.queue.enqueue_event("triage-pr-build-failure", "github", payload, "github:check:101")
        self.assertEqual(first[1:], (True, "pending"))
        self.assertEqual(TaskQueue(self.database).enqueue_event(
            "triage-pr-build-failure", "github", dict(reversed(list(payload.items()))), "github:check:101"
        ), (first[0], False, "pending"))
        with self.assertRaises(IdempotencyConflict):
            self.queue.enqueue_event("triage-pr-build-failure", "github", {"changed": True}, "github:check:101")
        task = TaskQueue(self.database).claim_event()
        self.assertEqual(task["payload"], payload)
        self.assertEqual(task["command"], "triage-pr-build-failure")
        TaskQueue(self.database).finish_event(first[0], "reported", {"reason": "other checks failed"})
        self.assertEqual(TaskQueue(self.database).get_event(first[0])["report"]["reason"], "other checks failed")
        self.assertIsNone(self.queue.claim_event())

    def test_interrupted_event_requires_review_before_retry(self):
        event_id = self.queue.enqueue_event(
            "triage-pr-build-failure", "github", {"source": "other"}, "key"
        )[0]
        self.assertEqual(self.queue.claim_event()["id"], event_id)
        with self.assertRaises(ValueError):
            TaskQueue(self.database).claim_event()
        self.assertEqual(TaskQueue(self.database).recover_event(), 1)
        self.assertEqual(TaskQueue(self.database).get_event(event_id)["status"], "failed")
        TaskQueue(self.database).retry_event(event_id)
        self.assertEqual(TaskQueue(self.database).claim_event()["attempts"], 2)

    def test_concurrent_duplicate_events_create_one_event(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(
                lambda _: self.queue.enqueue_event(
                    "triage-pr-build-failure", "github", {"checkRunId": 101}, "github:check:101"
                ),
                range(12),
            ))
        self.assertEqual(len({result[0] for result in results}), 1)
        self.assertEqual(sum(created for _, created, _ in results), 1)
        self.assertEqual(len(self.queue.list_events()), 1)

    def test_event_cli_commands(self):
        def run(*arguments):
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--queue-db", str(self.database), *arguments],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

        event_id = self.queue.enqueue_event(
            "triage-pr-build-failure", "github", {"source": "other"}, "github:check:102"
        )[0]
        self.assertEqual(json.loads(run("list-events"))[0]["id"], event_id)
        self.assertEqual(run("recover-events"), "0")
        self.queue.claim_event()
        self.assertEqual(run("recover-events"), "1")
        run("retry-event", str(event_id))
        self.assertEqual(json.loads(run("list-events"))[0]["status"], "pending")

    def test_prompt_queue_cli_commands_are_removed(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--queue-db", str(self.database), "enqueue", "Old prompt"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Old prompt", result.stdout)

    def test_cli_reports_invalid_event_action(self):
        def run(*arguments):
            with patch.object(sys, "argv", [str(SCRIPT), "--queue-db", str(self.database), *arguments]):
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
                    exit_code = main()
            return exit_code, output.getvalue(), error.getvalue()

        self.assertEqual(run("retry-event", "999")[0], 1)
        self.assertIn("Task queue error", run("retry-event", "999")[2])


if __name__ == "__main__":
    unittest.main()
