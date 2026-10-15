import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "task-queue"))
from task_queue import TaskQueue

from worker import TaskWorker, main
from worker import EventWorker


class TaskWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.queue = TaskQueue(self.root / "tasks.sqlite3")
        self.runner_log = self.root / "runner.log"
        self.runner = self.root / "agent-runner"
        self.runner.write_text(
            "#!{}\n".format(sys.executable)
            + "import json, os, sys\n"
            + "from pathlib import Path\n"
            + "prompt = sys.stdin.read()\n"
            + "with Path(os.environ['WORKER_TEST_LOG']).open('a') as log:\n"
            + "    log.write(json.dumps({'prompt': prompt, 'cwd': os.getcwd()}) + '\\n')\n"
            + "sys.exit(2 if prompt == 'fail' else 0)\n",
            encoding="utf-8",
        )
        self.runner.chmod(0o700)
        environment = patch.dict(os.environ, {"WORKER_TEST_LOG": str(self.runner_log)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_processes_fifo_until_empty_without_interpreting_prompt(self):
        first = "Review $(touch /tmp/never-execute)\nAnd report back"
        self.queue.enqueue(first)
        self.queue.enqueue("Second task")

        TaskWorker(self.queue, self.runner, self.workspace).run(until_empty=True)

        records = [json.loads(line) for line in self.runner_log.read_text().splitlines()]
        self.assertEqual(records, [
            {"prompt": first, "cwd": str(self.workspace.resolve())},
            {"prompt": "Second task", "cwd": str(self.workspace.resolve())},
        ])
        self.assertEqual([task["status"] for task in self.queue.list_tasks()], ["done", "done"])

    def test_marks_failure_then_processes_next_task(self):
        self.queue.enqueue("fail")
        self.queue.enqueue("succeed")

        TaskWorker(self.queue, self.runner, self.workspace).run(until_empty=True)

        failed, succeeded = self.queue.list_tasks()
        self.assertEqual((failed["status"], failed["attempts"]), ("failed", 1))
        self.assertIn("2", failed["last_error"])
        self.assertEqual(succeeded["status"], "done")

    def test_refuses_stale_running_task_without_replaying(self):
        self.queue.enqueue("possibly already completed")
        self.queue.claim()
        self.queue.enqueue("pending")

        with self.assertRaises(ValueError):
            TaskWorker(self.queue, self.runner, self.workspace).run(until_empty=True)

        self.assertFalse(self.runner_log.exists())
        self.assertEqual([task["status"] for task in self.queue.list_tasks()], ["running", "pending"])

    def test_checks_runner_and_workspace_before_claiming(self):
        self.queue.enqueue("keep pending")
        for runner, workspace in ((self.root / "missing-runner", self.workspace),
                                  (self.runner, self.root / "missing-workspace")):
            with self.assertRaises(ValueError):
                TaskWorker(self.queue, runner, workspace)

        self.assertEqual(self.queue.list_tasks()[0]["status"], "pending")

        with self.assertRaises(ValueError):
            TaskWorker(self.queue, self.runner, self.workspace, poll_interval=0)

    def test_waits_on_empty_queue_and_stops_when_signalled(self):
        worker = TaskWorker(self.queue, self.runner, self.workspace)

        def stop_during_wait(poll_interval):
            self.assertEqual(poll_interval, 2.0)
            worker._stop(None, None)

        with patch.object(worker.stopping, "wait", side_effect=stop_during_wait):
            worker.run()

        self.assertTrue(worker.stopping.is_set())

    def test_failed_runner_launch_marks_task_failed(self):
        task_id = self.queue.enqueue("No executable anymore")
        worker = TaskWorker(self.queue, self.runner, self.workspace)
        self.runner.unlink()

        worker.run(until_empty=True)

        task = self.queue.list_tasks()[0]
        self.assertEqual((task["id"], task["status"]), (task_id, "failed"))
        self.assertIn("Runner launch failed", task["last_error"])

    def test_cli_processes_tasks_and_reports_config_errors(self):
        self.queue.enqueue("From the CLI")
        arguments = ["worker.py", "--queue-db", str(self.queue.database),
                     "--workspace", str(self.workspace), "--runner", str(self.runner), "--until-empty"]

        with patch.object(sys, "argv", arguments):
            self.assertEqual(main(), 0)
        self.assertEqual(self.queue.list_tasks()[0]["status"], "done")

        with patch.object(sys, "argv", arguments[:-3] + ["--runner", str(self.root / "missing"), "--until-empty"]):
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(main(), 1)
        self.assertIn("Task worker error", output.getvalue())

    def test_event_worker_dispatches_event_and_leaves_legacy_prompts_unexecuted(self):
        from unittest.mock import Mock
        self.queue.enqueue("Old prompt")
        event_id = self.queue.enqueue_event("triage-pr-build-failure", "github",
                                            {"source": "github-actions"}, "event:1")[0]
        workflow = Mock()
        workflow.run.return_value = {"status": "unsupported", "reason": "CI provider not supported"}
        EventWorker(self.queue, workflow).run(until_empty=True)
        self.assertEqual(workflow.run.call_args.args[:3], (event_id, "triage-pr-build-failure", "github"))
        self.assertEqual(self.queue.get_event(event_id)["status"], "unsupported")
        self.assertEqual(self.queue.list_tasks()[0]["status"], "pending")

    def test_event_worker_failure_does_not_claim_success_or_replay(self):
        from unittest.mock import Mock
        event_id = self.queue.enqueue_event("triage-pr-build-failure", "github", {"source": "other"}, "event:2")[0]
        workflow = Mock()
        workflow.run.side_effect = RuntimeError("build log unavailable")
        EventWorker(self.queue, workflow).run(until_empty=True)
        self.assertEqual(self.queue.get_event(event_id)["status"], "failed")
        self.assertEqual(self.queue.get_event(event_id)["report"]["reason"], "build log unavailable")


if __name__ == "__main__":
    unittest.main()