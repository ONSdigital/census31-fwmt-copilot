import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-workspace" / "task-queue"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-workspace" / "worker"))
from task_queue import TaskQueue
from worker import TaskWorker
from copilot_runner import main as runner_main


RUNNER = Path(__file__).with_name("copilot_runner.py")


class CopilotRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.artifacts = self.workspace / "docs"
        self.artifacts.mkdir()
        self.log = self.root / "copilot.json"
        copilot = self.root / "copilot"
        copilot.write_text(
            "#!{}\n".format(sys.executable)
            + "import json, os, sys\n"
            + "from pathlib import Path\n"
            + "Path(os.environ['COPILOT_TEST_LOG']).write_text(json.dumps("
            + "{'args': sys.argv[1:], 'prompt': sys.stdin.read()}))\n"
            + "sys.exit(int(os.environ.get('COPILOT_TEST_EXIT', '0')))\n",
            encoding="utf-8",
        )
        copilot.chmod(0o700)

    def invoke(self, prompt, **overrides):
        environment = dict(os.environ, PATH="{}:{}".format(self.root, os.environ.get("PATH", "")),
                           COPILOT_TEST_LOG=str(self.log), AGENT_ARTIFACTS_DIR=str(self.artifacts))
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, str(RUNNER)], input=prompt, capture_output=True, text=True,
            env=environment, cwd=self.workspace,
        )

    def test_passes_prompt_only_on_stdin_with_scoped_options(self):
        prompt = "Change $(touch /tmp/never-execute)\nThen run tests"
        result = self.invoke(prompt, COPILOT_ALLOWED_TOOLS="write,shell(git:*)")

        self.assertEqual(result.returncode, 0, result.stderr)
        logged = json.loads(self.log.read_text())
        self.assertEqual(logged["prompt"], prompt)
        self.assertIn("-C", logged["args"])
        self.assertIn(str(self.workspace.resolve()), logged["args"])
        self.assertIn("--add-dir={}".format(self.artifacts), logged["args"])
        self.assertIn("--allow-tool=write,shell(git:*)", logged["args"])
        self.assertIn("--no-ask-user", logged["args"])
        self.assertNotIn(prompt, logged["args"])

    def test_propagates_failure_without_broad_permissions(self):
        result = self.invoke("Review only", COPILOT_TEST_EXIT="4", AGENT_ARTIFACTS_DIR="")

        self.assertEqual(result.returncode, 4)
        logged = json.loads(self.log.read_text())
        self.assertFalse(any(argument.startswith("--allow-tool") for argument in logged["args"]))
        self.assertFalse(any(argument.startswith("--add-dir") for argument in logged["args"]))

    def test_rejects_missing_trusted_directory(self):
        result = self.invoke("Do not run", AGENT_ARTIFACTS_DIR=str(self.root / "missing"))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_worker_and_adapter_process_task_from_queue(self):
        runner = self.root / "copilot-runner"
        shutil.copyfile(RUNNER, runner)
        runner.chmod(0o700)
        queue = TaskQueue(self.root / "tasks.sqlite3")
        queue.enqueue("Review the docs")
        environment = dict(os.environ, PATH="{}:{}".format(self.root, os.environ.get("PATH", "")),
                           COPILOT_TEST_LOG=str(self.log), AGENT_ARTIFACTS_DIR=str(self.artifacts))

        from unittest.mock import patch
        with patch.dict(os.environ, environment):
            TaskWorker(queue, runner, self.workspace).run(until_empty=True)

        self.assertEqual(queue.list_tasks()[0]["status"], "done")
        self.assertEqual(json.loads(self.log.read_text())["prompt"], "Review the docs")

    def test_main_redacts_token_and_reports_missing_cli(self):
        environment = dict(os.environ, PATH="{}:{}".format(self.root, os.environ.get("PATH", "")),
                           COPILOT_TEST_LOG=str(self.log), AGENT_ARTIFACTS_DIR=str(self.artifacts),
                           COPILOT_GITHUB_TOKEN="example-copilot-token")
        with patch.dict(os.environ, environment), patch.object(sys, "stdin", io.StringIO("Review docs")):
            self.assertEqual(runner_main(), 0)
        self.assertIn("--secret-env-vars=COPILOT_GITHUB_TOKEN", json.loads(self.log.read_text())["args"])
        self.assertNotIn("example-copilot-token", self.log.read_text())

        with patch.dict(os.environ, {"PATH": "", "AGENT_ARTIFACTS_DIR": "", "COPILOT_GITHUB_TOKEN": ""}):
            with patch.object(sys, "stdin", io.StringIO("Review docs")), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(runner_main(), 1)
        self.assertIn("Could not start Copilot CLI", output.getvalue())


if __name__ == "__main__":
    unittest.main()