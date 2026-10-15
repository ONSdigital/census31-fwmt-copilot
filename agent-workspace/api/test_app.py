import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "task-queue"))
from task_queue import TaskQueue

from app import create_app


class AgentRequestTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.database = self.root / "queue.sqlite3"
        self.token_file = self.root / "api-token"
        self.token_file.write_text("example-api-token\n", encoding="utf-8")
        environment = patch.dict(os.environ, {
            "AGENT_API_TOKEN_FILE": str(self.token_file),
            "TASK_QUEUE_DB": str(self.database),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_authenticated_request_enqueues_prompt_and_returns_message_id(self):
        with TestClient(create_app()) as client:
            response = client.post(
                "/agent-requests",
                json={"prompt": "  Review the job-service change  "},
                headers={"Authorization": "Bearer example-api-token"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"messageId": 1, "status": "pending"})
        self.assertEqual(TaskQueue(self.database).list_tasks()[0]["prompt"], "Review the job-service change")

    def test_missing_and_wrong_credentials_cannot_enqueue(self):
        with TestClient(create_app()) as client:
            for headers in ({}, {"Authorization": "Bearer wrong-token"}, {"Authorization": "Basic example-api-token"}):
                response = client.post("/agent-requests", json={"prompt": "Not authorized"}, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

        self.assertEqual(TaskQueue(self.database).list_tasks(), [])

    def test_rejects_blank_and_oversized_prompts(self):
        with TestClient(create_app()) as client:
            for prompt in (" \n ", "x" * 4097):
                response = client.post(
                    "/agent-requests",
                    json={"prompt": prompt},
                    headers={"Authorization": "Bearer example-api-token"},
                )
                self.assertEqual(response.status_code, 422)

        self.assertEqual(TaskQueue(self.database).list_tasks(), [])

    def test_refuses_to_start_without_readable_nonempty_secret(self):
        for token_file in ("", str(self.root / "missing-token")):
            with self.subTest(token_file=token_file):
                with patch.dict(os.environ, {"AGENT_API_TOKEN_FILE": token_file}):
                    with self.assertRaises(RuntimeError):
                        with TestClient(create_app()):
                            pass

        self.token_file.write_text(" \n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            with TestClient(create_app()):
                pass

    def test_refuses_to_start_without_writable_queue_path(self):
        for database in ("", str(self.root / "missing-parent" / "queue.sqlite3")):
            with self.subTest(database=database):
                with patch.dict(os.environ, {"TASK_QUEUE_DB": database}):
                    with self.assertRaises(RuntimeError):
                        with TestClient(create_app()):
                            pass

    def test_idempotent_replay_and_conflict(self):
        headers = {"Authorization": "Bearer example-api-token"}
        with TestClient(create_app()) as client:
            first = client.post("/agent-requests", json={"prompt": "Review PR", "idempotencyKey": "  github:repo:run:1  "}, headers=headers)
            self.assertEqual(first.status_code, 201)
            task_id = first.json()["messageId"]
            queue = TaskQueue(self.database)
            queue.claim()
            queue.complete(task_id)

            replay = client.post("/agent-requests", json={"prompt": "Review PR", "idempotencyKey": "github:repo:run:1"}, headers=headers)
            conflict = client.post("/agent-requests", json={"prompt": "Different PR", "idempotencyKey": "github:repo:run:1"}, headers=headers)

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), {"messageId": task_id, "status": "done"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(TaskQueue(self.database).list_tasks()), 1)

    def test_rejects_invalid_idempotency_keys(self):
        with TestClient(create_app()) as client:
            for key in ("", "  ", "x" * 256):
                response = client.post(
                    "/agent-requests", json={"prompt": "Review PR", "idempotencyKey": key},
                    headers={"Authorization": "Bearer example-api-token"},
                )
                self.assertEqual(response.status_code, 422)

        self.assertEqual(TaskQueue(self.database).list_tasks(), [])


if __name__ == "__main__":
    unittest.main()