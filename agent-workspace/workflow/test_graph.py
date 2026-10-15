import tempfile
import unittest
from pathlib import Path

from workflow.graph import BuildFailureWorkflow


class FakeEvidence:
    def __init__(self, eligible=True, recheck=True):
        self.eligible = eligible
        self.safe = recheck
        self.inspections = 0

    def inspect(self, payload):
        self.inspections += 1
        return {"eligible": self.eligible, "reason": "Spotless only" if self.eligible else "Other check failed",
                "command": ["mvn", "clean", "verify", "spotless:check"],
                "head_repo": payload["repository"], "head_ref": "feature", "head_sha": payload["headSha"],
                "build_id": "build-1", "other_failures": [] if self.eligible else ["tests"]}

    def recheck(self, payload, evidence):
        if evidence["head_sha"] != payload["headSha"]:
            raise ValueError("Unmatched checkpoint")
        return {"headSha": payload["headSha"], "headRef": "feature",
                "complete": self.safe, "otherFailures": []}


class FakeRepair:
    def __init__(self):
        self.repaired = []
        self.pushed = []

    def reproduce_and_fix(self, task_id, payload, evidence):
        self.repaired.append(task_id)
        return {"commit": "b" * 40, "worktree": "/tmp/test-worktree", "validated": True}

    def publish(self, task_id, payload, evidence, result, recheck):
        if recheck()["complete"] is not True:
            return {"status": "reported", "reason": "PR head or checks changed"}
        self.pushed.append(task_id)
        return {"status": "pushed-awaiting-ci", "commit": result["commit"]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.checkpoints = Path(root.name) / "checkpoints.sqlite3"
        self.payload = {"repository": "ONSdigital/repo", "headSha": "a" * 40,
                        "source": "google-cloud-build", "prNumber": 14, "checkRunId": 101}

    def test_only_proven_spotless_failure_is_repaired_and_pushed(self):
        evidence, repair = FakeEvidence(), FakeRepair()
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            report = workflow.run(3, "triage-pr-build-failure", "github", self.payload)
        self.assertEqual(report["status"], "pushed-awaiting-ci")
        self.assertEqual(repair.repaired, [3])
        self.assertEqual(repair.pushed, [3])
        self.assertEqual(report["commit"], "b" * 40)

    def test_other_failures_and_unsupported_providers_stop_without_edits(self):
        evidence, repair = FakeEvidence(eligible=False), FakeRepair()
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            report = workflow.run(4, "triage-pr-build-failure", "github", self.payload)
            unsupported = workflow.run(5, "triage-pr-build-failure", "github",
                                       {**self.payload, "source": "github-actions"})
        self.assertEqual(report["status"], "reported")
        self.assertEqual(unsupported["status"], "unsupported")
        self.assertEqual(repair.repaired, [])
        self.assertEqual(evidence.inspections, 1)

    def test_restart_preserves_terminal_checkpoint_without_repush(self):
        evidence, repair = FakeEvidence(), FakeRepair()
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            workflow.run(6, "triage-pr-build-failure", "github", self.payload)
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            report = workflow.run(6, "triage-pr-build-failure", "github", self.payload)
        self.assertEqual(report["status"], "pushed-awaiting-ci")
        self.assertEqual(repair.pushed, [6])

    def test_changed_pr_before_push_reports_without_push(self):
        evidence, repair = FakeEvidence(recheck=False), FakeRepair()
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            report = workflow.run(7, "triage-pr-build-failure", "github", self.payload)
        self.assertEqual(report["status"], "reported")
        self.assertEqual(repair.pushed, [])

    def test_restart_after_repair_error_uses_checkpointed_evidence(self):
        evidence, repair = FakeEvidence(), FakeRepair()
        original = repair.reproduce_and_fix
        calls = []

        def interrupted(*args):
            calls.append(1)
            raise RuntimeError("tool unavailable")

        repair.reproduce_and_fix = interrupted
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            with self.assertRaises(RuntimeError):
                workflow.run(8, "triage-pr-build-failure", "github", self.payload)
        repair.reproduce_and_fix = original
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            report = workflow.run(8, "triage-pr-build-failure", "github", self.payload)
        self.assertEqual(report["status"], "pushed-awaiting-ci")
        self.assertEqual(evidence.inspections, 1)
        self.assertEqual(len(calls), 1)

    def test_restart_after_publication_error_uses_checkpointed_repair(self):
        evidence, repair = FakeEvidence(), FakeRepair()

        def uncertain(*args):
            raise RuntimeError("push outcome uncertain")

        repair.publish = uncertain
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            with self.assertRaisesRegex(RuntimeError, "push outcome uncertain"):
                workflow.run(9, "triage-pr-build-failure", "github", self.payload)
        repair.publish = lambda *args: {"status": "reported", "reason": "push outcome uncertain"}
        with BuildFailureWorkflow(self.checkpoints, evidence, repair) as workflow:
            report = workflow.run(9, "triage-pr-build-failure", "github", self.payload)
        self.assertEqual(report["status"], "reported")
        self.assertEqual(evidence.inspections, 1)
        self.assertEqual(repair.repaired, [9])
        self.assertEqual(repair.pushed, [])


if __name__ == "__main__":
    unittest.main()
