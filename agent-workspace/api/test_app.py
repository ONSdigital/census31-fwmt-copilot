import os
import sqlite3
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
        self.headers = {"Authorization": "Bearer " + self.token_file.read_text().strip()}

    def event(self):
        return {
            "command": "triage-pr-build-failure", "type": "github",
            "payload": {
                "repository": "ONSdigital/census31-fwmt-common", "prNumber": 14,
                "prUrl": "https://github.com/ONSdigital/census31-fwmt-common/pull/14",
                "headSha": "a" * 40, "checkRunId": 116430236774,
                "runUrl": "https://console.cloud.google.com/cloud-build/builds/abc",
                "workflow": "common-build", "source": "google-cloud-build", "conclusion": "failure",
            },
            "idempotencyKey": "github:ONSdigital/census31-fwmt-common:pr:14:check:116430236774",
        }

    def test_structured_cloud_build_request_persists_event_and_deduplicates(self):
        event = self.event()
        event["payload"]["providerMetadata"] = {"extra": ["accepted", 42]}
        with TestClient(create_app()) as client:
            first = client.post("/agent-requests", json=event, headers=self.headers)
            self.assertEqual(first.status_code, 201)
            task_id = first.json()["messageId"]
            queue = TaskQueue(self.database)
            queue.claim_event()
            queue.finish_event(task_id, "reported", {"reason": "test"})
            replay = client.post("/agent-requests", json=event, headers=self.headers)
            conflict = client.post(
                "/agent-requests",
                json={**event, "payload": {**event["payload"], "workflow": "changed"}},
                headers=self.headers,
            )
        self.assertEqual(first.json(), {"messageId": task_id, "status": "pending"})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), {"messageId": task_id, "status": "reported"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(queue.get_event(task_id)["payload"], event["payload"])
        with sqlite3.connect(self.database) as connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            ).fetchone())

    def test_missing_and_wrong_credentials_cannot_enqueue(self):
        with TestClient(create_app()) as client:
            for headers in ({}, {"Authorization": "Bearer wrong-token"},
                            {"Authorization": "Basic example-api-token"}):
                response = client.post("/agent-requests", json=self.event(), headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(TaskQueue(self.database).list_events(), [])

    def test_rejects_bad_command_payload_and_body_size(self):
        event = self.event()
        with TestClient(create_app()) as client:
            for invalid in ({"prompt": "legacy"}, {**event, "command": "untrusted"},
                            {**event, "payload": []},
                            {**event, "payload": {**event["payload"], "checkRunId": "one"}},
                            {**event, "payload": {**event["payload"], "prUrl": "https://evil.invalid"}},
                            {**event, "payload": {**event["payload"], "conclusion": "success"}},
                            {**event, "payload": {**event["payload"], "workflow": "x" * 5000}}):
                self.assertEqual(client.post("/agent-requests", json=invalid, headers=self.headers).status_code, 422)
            oversized = client.post("/agent-requests", json={**event, "extra": "x" * 70000},
                                    headers=self.headers)
            self.assertEqual(oversized.status_code, 413)
            for key in ("", " ", "x" * 256):
                self.assertEqual(client.post("/agent-requests", json={**event, "idempotencyKey": key},
                                             headers=self.headers).status_code, 422)
        self.assertEqual(TaskQueue(self.database).list_events(), [])

    def test_unknown_provider_is_accepted_for_workflow_to_report_unsupported(self):
        event = self.event()
        event["payload"] = {"source": "other-ci", "prNumber": 14}
        with TestClient(create_app()) as client:
            response = client.post("/agent-requests", json=event, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(TaskQueue(self.database).get_event(response.json()["messageId"])["payload"], event["payload"])

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


if __name__ == "__main__":
    unittest.main()
