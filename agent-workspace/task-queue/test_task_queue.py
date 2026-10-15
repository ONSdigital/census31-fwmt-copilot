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
        self.database = Path(temporary_directory.name) / "tasks.sqlite3"
        self.queue = TaskQueue(self.database)

    def test_claims_in_order_and_persists_completion(self):
        first_id = self.queue.enqueue("First task")
        second_id = self.queue.enqueue("Second task")

        first = self.queue.claim()
        self.assertEqual((first["id"], first["prompt"], first["attempts"]), (first_id, "First task", 1))
        with self.assertRaises(ValueError):
            TaskQueue(self.database).claim()

        self.queue.complete(first_id)
        second = TaskQueue(self.database).claim()
        self.assertEqual(second["id"], second_id)
        self.queue.complete(second_id)
        self.assertIsNone(self.queue.claim())
        self.assertEqual([task["status"] for task in TaskQueue(self.database).list_tasks()], ["done", "done"])

    def test_failure_requires_explicit_retry(self):
        task_id = self.queue.enqueue("Retry this")
        self.queue.claim()
        self.queue.fail(task_id, "Agent exited with status 1")

        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.list_tasks()[0]["last_error"], "Agent exited with status 1")
        self.queue.retry(task_id)
        retried = self.queue.claim()
        self.assertEqual((retried["id"], retried["attempts"]), (task_id, 2))

    def test_recover_marks_interrupted_task_failed(self):
        task_id = self.queue.enqueue("Possibly pushed changes")
        self.queue.claim()

        self.assertEqual(TaskQueue(self.database).recover(), 1)
        self.assertEqual(self.queue.list_tasks()[0]["status"], "failed")
        self.assertIn("interrupted", self.queue.list_tasks()[0]["last_error"])
        self.assertIsNone(self.queue.claim())
        with self.assertRaises(ValueError):
            self.queue.complete(task_id)

    def test_rejects_blank_tasks_and_invalid_transitions(self):
        with self.assertRaises(ValueError):
            self.queue.enqueue("  ")
        task_id = self.queue.enqueue("Valid")
        with self.assertRaises(ValueError):
            self.queue.complete(task_id)
        with self.assertRaises(ValueError):
            self.queue.retry(task_id)
        with self.assertRaises(ValueError):
            self.queue.fail(task_id, "Not running")

    def test_cli_round_trip(self):
        def run(*arguments):
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--queue-db", str(self.database), *arguments],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

        task_id = int(run("enqueue", "From the command line"))
        self.assertEqual(json.loads(run("claim"))["id"], task_id)
        run("fail", str(task_id), "Needs review")
        self.assertEqual(json.loads(run("list"))[0]["status"], "failed")
        run("retry", str(task_id))
        self.assertEqual(json.loads(run("claim"))["attempts"], 2)
        run("done", str(task_id))
        self.assertEqual(run("claim"), "")
        keyed_id = int(run("enqueue", "From a webhook", "--idempotency-key", "github:repo:run:4"))
        self.assertEqual(int(run("enqueue", "From a webhook", "--idempotency-key", "github:repo:run:4")), keyed_id)

    def test_idempotent_enqueue_preserves_original_status_and_rejects_conflicts(self):
        task_id, created, status = self.queue.enqueue_once("Review PR", "github:repo:run:1")
        self.assertEqual((created, status), (True, "pending"))
        self.queue.claim()
        self.queue.complete(task_id)

        self.assertEqual(self.queue.enqueue_once("Review PR", "github:repo:run:1"), (task_id, False, "done"))
        with self.assertRaises(IdempotencyConflict):
            self.queue.enqueue_once("Different PR", "github:repo:run:1")
        self.assertEqual(len(self.queue.list_tasks()), 1)
        self.assertNotEqual(self.queue.enqueue("Unkeyed"), self.queue.enqueue("Unkeyed"))

    def test_concurrent_duplicate_submissions_create_one_task(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(
                lambda _: self.queue.enqueue_once("Review PR", "github:repo:run:2"), range(12),
            ))

        self.assertEqual(len({result[0] for result in results}), 1)
        self.assertEqual(sum(created for _, created, _ in results), 1)
        self.assertEqual(len(self.queue.list_tasks()), 1)

    def test_migrates_existing_database_without_losing_tasks(self):
        legacy = self.database.parent / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, prompt TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT, started_at TEXT, finished_at TEXT, last_error TEXT)"
            )
            connection.execute("INSERT INTO tasks (prompt, status) VALUES ('Existing task', 'pending')")

        migrated = TaskQueue(legacy)
        self.assertEqual(migrated.list_tasks()[0]["prompt"], "Existing task")
        self.assertIsNone(migrated.list_tasks()[0]["idempotency_key"])
        self.assertEqual(migrated.enqueue_once("New task", "github:repo:run:3")[1], True)
        self.assertEqual(TaskQueue(legacy).enqueue_once("New task", "github:repo:run:3")[1], False)

    def test_rejects_invalid_idempotency_keys(self):
        for key in ("", "  ", "x" * 256, 123):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.queue.enqueue_once("Review PR", key)
        self.assertEqual(self.queue.list_tasks(), [])

    def test_cli_commands_and_idempotency_errors(self):
        def run(*arguments):
            with patch.object(sys, "argv", [str(SCRIPT), "--queue-db", str(self.database), *arguments]):
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
                    exit_code = main()
            return exit_code, output.getvalue().strip(), error.getvalue()

        key = "github:repo:run:5"
        task_id = int(run("enqueue", "Review PR", "--idempotency-key", key)[1])
        self.assertEqual(int(run("enqueue", "Review PR", "--idempotency-key", key)[1]), task_id)
        self.assertEqual(json.loads(run("claim")[1])["id"], task_id)
        self.assertEqual(run("fail", str(task_id), "Needs review")[0], 0)
        self.assertEqual(json.loads(run("list")[1])[0]["status"], "failed")
        self.assertEqual(run("retry", str(task_id))[0], 0)
        self.assertEqual(json.loads(run("claim")[1])["attempts"], 2)
        self.assertEqual(run("done", str(task_id))[0], 0)
        self.assertEqual(run("recover")[1], "0")
        self.assertEqual(run("claim")[1], "")
        self.assertEqual(run("enqueue", "Different PR", "--idempotency-key", key)[0], 1)
        self.assertIn("Task queue error", run("enqueue", "  ")[2])


if __name__ == "__main__":
    unittest.main()