import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "task-queue"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_queue import TaskQueue

from worker import EventWorker, main


class EventWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.queue = TaskQueue(self.root / "events.sqlite3")

    def test_dispatches_event_and_persists_report(self):
        event_id = self.queue.enqueue_event(
            "triage-pr-build-failure", "github", {"source": "github-actions"}, "event:1"
        )[0]
        workflow = Mock()
        workflow.run.return_value = {"status": "unsupported", "reason": "CI provider not supported"}

        EventWorker(self.queue, workflow).run(until_empty=True)

        self.assertEqual(workflow.run.call_args.args[:3], (event_id, "triage-pr-build-failure", "github"))
        self.assertEqual(self.queue.get_event(event_id)["status"], "unsupported")

    def test_workflow_failure_is_recorded_for_operator_review(self):
        event_id = self.queue.enqueue_event(
            "triage-pr-build-failure", "github", {"source": "other"}, "event:2"
        )[0]
        workflow = Mock()
        workflow.run.side_effect = RuntimeError("build log unavailable")

        with redirect_stderr(io.StringIO()) as output:
            EventWorker(self.queue, workflow).run(until_empty=True)

        event = self.queue.get_event(event_id)
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["report"]["reason"], "build log unavailable")
        self.assertIn("Workflow failed", output.getvalue())

    def test_stops_without_claiming_new_event(self):
        worker = EventWorker(self.queue, Mock())

        def stop_during_wait(interval):
            self.assertEqual(interval, 2.0)
            worker._stop(None, None)

        with patch.object(worker.stopping, "wait", side_effect=stop_during_wait):
            worker.run()
        self.assertTrue(worker.stopping.is_set())

    def test_worker_cli_runs_event_workflow_by_default(self):
        arguments = [
            "worker.py", "--queue-db", str(self.queue.database), "--workspace", str(self.workspace),
            "--checkpoints", str(self.root / "checkpoints.db"),
            "--worktree-root", str(self.root / "worktrees"), "--until-empty",
        ]
        evidence_module = types.ModuleType("workflow.evidence")
        evidence_module.EvidenceClient = Mock()
        repair_module = types.ModuleType("workflow.repair")
        repair_module.RepairAdapter = Mock()
        graph_module = types.ModuleType("workflow.graph")
        workflow_factory = Mock(return_value=MagicMock())
        graph_module.BuildFailureWorkflow = workflow_factory
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", arguments), \
                patch.dict(sys.modules, {
                    "workflow.evidence": evidence_module,
                    "workflow.repair": repair_module,
                    "workflow.graph": graph_module,
                }):
            self.assertEqual(main(), 0)
        workflow_factory.assert_called_once()

    def test_worker_requires_event_state_paths(self):
        arguments = [
            "worker.py", "--queue-db", str(self.queue.database), "--workspace", str(self.workspace),
        ]
        with patch.object(sys, "argv", arguments), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(), 1)
        self.assertIn("--checkpoints and --worktree-root", output.getvalue())

    def test_worker_no_longer_accepts_legacy_runner_or_events_switch(self):
        base_arguments = [
            "worker.py", "--queue-db", str(self.queue.database), "--workspace", str(self.workspace),
        ]
        for option in ("--runner", "--events"):
            with self.subTest(option=option), patch.object(sys, "argv", base_arguments + [option, "value"]):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main()


if __name__ == "__main__":
    unittest.main()
