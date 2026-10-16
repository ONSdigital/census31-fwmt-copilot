"""Isolated integration tests for the bounded Spotless repair adapter."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch


REPAIR_FILE = Path(__file__).with_name("repair.py")
spec = importlib.util.spec_from_file_location("workflow_repair", REPAIR_FILE)
repair = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = repair
spec.loader.exec_module(repair)
RepairAdapter = repair.RepairAdapter


class RepairAdapterTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / (".repair-test-" + uuid.uuid4().hex)
        root.mkdir()
        self.addCleanup(shutil.rmtree, root)
        self.source = root / "source"
        self.source.mkdir()
        self.worktrees = root / "worktrees"
        self.run_git("init", "-q", "-b", "feature", cwd=self.source)
        self.run_git("config", "user.name", "Tester", cwd=self.source)
        self.run_git("config", "user.email", "tester@example.test", cwd=self.source)
        (self.source / "Example.java").write_text("class Example {  }\n")
        (self.source / "mvnw").write_text(
            "#!" + sys.executable + "\n"
            "import pathlib, sys\n"
            "p = pathlib.Path('Example.java')\n"
            "if 'test' in sys.argv and pathlib.Path('FAIL_TEST').exists():\n"
            "    print('[ERROR] Tests run: 1, Failures: 1')\n"
            "    sys.exit(1)\n"
            "if 'spotless:apply' in sys.argv:\n"
            "    p.write_text('class Example {}\\n')\n"
            "elif 'spotless:check' in sys.argv and p.read_text() != 'class Example {}\\n':\n"
            "    print('[ERROR] Failed to execute goal com.diffplug.spotless:spotless-maven-plugin:check')\n"
            "    print('The following files had format violations: Example.java')\n"
            "    sys.exit(1)\n"
        )
        (self.source / "mvnw").chmod(0o755)
        self.run_git("add", ".", cwd=self.source)
        self.run_git("commit", "-qm", "baseline", cwd=self.source)
        self.run_git("remote", "add", "origin", "https://github.com/test/repair.git",
                     cwd=self.source)
        self.head = self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        self.adapter = RepairAdapter(self.source, self.worktrees)
        self.payload = {"headSha": self.head, "headRef": "feature",
                        "headRepository": "test/repair"}
        self.evidence = {"command": ["./mvnw", "-B", "spotless:check"],
                         "violatingFiles": ["Example.java"]}

    @staticmethod
    def run_git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              check=True)

    def test_reproduces_fixes_and_validates_without_touching_source(self):
        result = self.adapter.reproduce_and_fix("task/123", self.payload, self.evidence)
        self.assertEqual(result["status"], "validated")
        self.assertTrue(result["validated"])
        self.assertIsNone(result["commit"])
        self.assertEqual(result["changedFiles"], ["Example.java"])
        self.assertEqual((self.source / "Example.java").read_text(), "class Example {  }\n")
        self.assertEqual((Path(result["worktree"]) / "Example.java").read_text(),
                         "class Example {}\n")
        self.assertEqual(self.run_git("status", "--porcelain", cwd=self.source).stdout, "")

    def test_rejects_non_spotless_command_without_creating_worktree(self):
        result = self.adapter.reproduce_and_fix(
            "bad", self.payload, {**self.evidence, "command": ["./mvnw", "test"]})
        self.assertFalse(result["validated"])
        self.assertIsNone(result["worktree"])
        self.assertFalse(self.worktrees.exists())

    def test_rejects_untrusted_maven_options_and_goals(self):
        for command in (["./mvnw", "spotless:check", "-Dexec.args=rm"],
                        ["./mvnw", "spotless:check", "help:effective-settings"],
                        ["sh", "-c", "./mvnw spotless:check"]):
            with self.subTest(command=command):
                result = self.adapter.reproduce_and_fix(
                    "bad", self.payload, {**self.evidence, "command": command})
                self.assertFalse(result["validated"])
                self.assertTrue(result["reason"])

    def test_non_spotless_failure_preserves_worktree_and_does_not_edit(self):
        (self.source / "FAIL_TEST").write_text("1")
        self.run_git("add", "FAIL_TEST", cwd=self.source)
        self.run_git("commit", "-qm", "failure fixture", cwd=self.source)
        self.payload["headSha"] = self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        self.evidence["command"] = ["./mvnw", "test", "spotless:check"]
        # A failed test after formatting is a distinct failure, not a formatting-only failure.
        result = self.adapter.reproduce_and_fix("other-failure", self.payload, self.evidence)
        self.assertFalse(result["validated"])
        self.assertEqual(len(list(self.worktrees.iterdir())), 1)
        self.assertEqual((next(self.worktrees.iterdir()) / "Example.java").read_text(),
                         "class Example {  }\n")

    def test_rejects_changes_outside_proven_java_files(self):
        original = (self.source / "mvnw").read_text()
        (self.source / "mvnw").write_text(original.replace(
            "p.write_text('class Example {}\\n')",
            "p.write_text('class Example {}\\n'); pathlib.Path('notes.txt').write_text('oops')"))
        self.run_git("add", "mvnw", cwd=self.source)
        self.run_git("commit", "-qm", "malicious formatter fixture", cwd=self.source)
        self.payload["headSha"] = self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        result = self.adapter.reproduce_and_fix("scope", self.payload, self.evidence)
        self.assertFalse(result["validated"])
        self.assertTrue((next(self.worktrees.iterdir()) / "notes.txt").exists())

    def test_failed_post_fix_tests_retain_worktree_without_validation(self):
        (self.source / "FAIL_TEST").write_text("1")
        self.run_git("add", "FAIL_TEST", cwd=self.source)
        self.run_git("commit", "-qm", "failing tests fixture", cwd=self.source)
        self.payload["headSha"] = self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        result = self.adapter.reproduce_and_fix("test-failure", self.payload, self.evidence)
        self.assertFalse(result["validated"])
        self.assertIn("Required command failed", result["reason"])
        self.assertTrue(Path(result["worktree"]).is_dir())

    def test_publish_commits_but_never_pushes_on_stale_head(self):
        result = self.adapter.reproduce_and_fix("task", self.payload, self.evidence)
        published = self.adapter.publish(
            "task", self.payload, self.evidence, result,
            lambda: {"headSha": "0" * 40, "headRef": "feature",
                     "otherFailures": [], "complete": True},
        )
        self.assertEqual(published["status"], "reported")
        self.assertTrue(published["commit"])
        self.assertEqual(self.run_git("status", "--porcelain", cwd=self.source).stdout, "")
        self.assertTrue(result["worktree"])

    def test_publish_requires_complete_clean_recheck(self):
        result = self.adapter.reproduce_and_fix("task", self.payload, self.evidence)
        for snapshot in (
            {"headSha": self.head, "otherFailures": ["lint"], "complete": True},
            {"headSha": self.head, "otherFailures": [], "complete": False},
        ):
            with self.subTest(snapshot=snapshot):
                published = self.adapter.publish("task", self.payload, self.evidence, result,
                                                 lambda: snapshot)
                self.assertEqual(published["status"], "reported")

    def test_publish_commits_with_trailer_and_uses_non_force_push(self):
        result = self.adapter.reproduce_and_fix("task", self.payload, self.evidence)
        original = repair.subprocess.run
        observed = []

        def intercept(command, **kwargs):
            if command[:2] == ["git", "-C"] and "ls-remote" in command:
                return subprocess.CompletedProcess(command, 0,
                                                   self.head + "\trefs/heads/feature\n", "")
            if command[:2] == ["git", "-C"] and "push" in command:
                observed.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(command, **kwargs)

        with patch.object(repair.subprocess, "run", side_effect=intercept):
            published = self.adapter.publish(
                "task", self.payload, self.evidence, result,
                lambda: {"headSha": self.head, "headRef": "feature",
                         "otherFailures": [], "complete": True},
            )
        self.assertEqual(published["status"], "pushed-awaiting-ci")
        self.assertEqual(len(observed), 1)
        self.assertIn("HEAD:refs/heads/feature", observed[0])
        self.assertNotIn("--force", observed[0])
        message = self.run_git("log", "-1", "--format=%B",
                               cwd=Path(result["worktree"])).stdout
        self.assertIn("Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
                      message)

    def test_remote_mismatch_blocks_push(self):
        result = self.adapter.reproduce_and_fix("task", self.payload, self.evidence)
        original = repair.subprocess.run

        def intercept(command, **kwargs):
            if command[:2] == ["git", "-C"] and "ls-remote" in command:
                return subprocess.CompletedProcess(command, 0, "0" * 40 +
                                                   "\trefs/heads/feature\n", "")
            if command[:2] == ["git", "-C"] and "push" in command:
                self.fail("unexpected push")
            return original(command, **kwargs)

        with patch.object(repair.subprocess, "run", side_effect=intercept):
            published = self.adapter.publish(
                "task", self.payload, self.evidence, result,
                lambda: {"headSha": self.head, "headRef": "feature",
                         "otherFailures": [], "complete": True},
            )
            self.assertEqual(published["status"], "reported")

    def test_mismatched_repository_and_task_cannot_publish(self):
        result = self.adapter.reproduce_and_fix("first", self.payload, self.evidence)
        snapshot = lambda: {"headSha": self.head, "headRef": "feature",
                            "otherFailures": [], "complete": True}
        self.assertEqual(self.adapter.publish(
            "second", self.payload, self.evidence, result, snapshot)["status"], "reported")
        self.assertEqual(self.adapter.publish(
            "first", {**self.payload, "headRepository": "someone/else"},
            self.evidence, result, snapshot)["status"], "reported")

    def test_untrusted_origin_cannot_receive_push(self):
        result = self.adapter.reproduce_and_fix("first", self.payload, self.evidence)
        self.run_git("remote", "set-url", "origin", "https://github.com/other/project.git",
                     cwd=self.source)
        published = self.adapter.publish(
            "first", self.payload, self.evidence, result,
            lambda: {"headSha": self.head, "headRef": "feature",
                     "otherFailures": [], "complete": True},
        )
        self.assertEqual(published["status"], "reported")

    def test_recheck_exception_is_reported_and_commit_is_retained(self):
        result = self.adapter.reproduce_and_fix("first", self.payload, self.evidence)

        def unavailable():
            raise ConnectionError("private authentication detail")

        published = self.adapter.publish(
            "first", self.payload, self.evidence, result, unavailable)
        self.assertEqual(published["status"], "reported")
        self.assertIn("ConnectionError", published["reason"])
        self.assertNotIn("private authentication detail", published["reason"])
        self.assertTrue(published["commit"])

    def test_queue_integer_id_and_verified_evidence_identity(self):
        payload = {"headSha": self.head, "repository": "test/repair",
                   "prNumber": 8, "checkRunId": 123}
        evidence = {**self.evidence, "head_repo": "test/repair",
                    "head_ref": "feature", "head_sha": self.head}
        result = self.adapter.reproduce_and_fix(42, payload, evidence)
        self.assertTrue(result["validated"], result.get("reason"))
        self.assertEqual(Path(result["worktree"]).name, "task-42-" + self.head[:12])
        original = repair.subprocess.run

        def fake_remote(command, **kwargs):
            if "ls-remote" in command:
                return subprocess.CompletedProcess(
                    command, 0, self.head + "\trefs/heads/feature\n", "")
            if "push" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(command, **kwargs)

        with patch.object(repair.subprocess, "run", side_effect=fake_remote):
            published = self.adapter.publish(
                42, payload, evidence, result,
                lambda: {"headSha": self.head, "headRef": "feature",
                         "headRepository": "test/repair", "complete": True,
                         "otherFailures": []})
        self.assertEqual(published["status"], "pushed-awaiting-ci", published.get("reason"))

    def test_invalid_queue_ids_do_not_allocate_worktrees(self):
        for task_id in (0, -3, True, False):
            with self.subTest(task_id=task_id):
                result = self.adapter.reproduce_and_fix(
                    task_id, self.payload, self.evidence)
                self.assertFalse(result["validated"])
                self.assertIsNone(result["worktree"])
        self.assertFalse(self.worktrees.exists())

    def test_runner_is_optional_for_deterministic_spotless_repair(self):
        adapter = RepairAdapter(self.source, self.worktrees)
        result = adapter.reproduce_and_fix(64, self.payload, self.evidence)
        self.assertTrue(result["validated"], result.get("reason"))

    def test_sampled_spotless_log_allows_additional_java_in_source_roots(self):
        root = self.source / "src/main/java"
        root.mkdir(parents=True)
        (root / "First.java").write_text("class First {  }\n")
        (root / "Second.java").write_text("class Second {  }\n")
        wrapper = self.source / "mvnw"
        wrapper.write_text(wrapper.read_text().replace(
            "pathlib.Path('Example.java')",
            "pathlib.Path('src/main/java/First.java')").replace(
            "class Example {}", "class First {}") + (
            "for name in ('First.java', 'Second.java'):\n"
            "    java = pathlib.Path('src/main/java') / name\n"
            "    if 'spotless:apply' in sys.argv:\n"
            "        java.write_text('class ' + name[:-5] + ' {}\\n')\n"
            "    elif 'spotless:check' in sys.argv and '{  }' in java.read_text():\n"
            "        sys.exit(1)\n"
        ))
        self.run_git("add", ".", cwd=self.source)
        self.run_git("commit", "-qm", "formatting samples", cwd=self.source)
        self.payload["headSha"] = self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        evidence = {"command": ["./mvnw", "-B", "spotless:check"],
                    "violatingFiles": ["src/main/java/First.java"]}
        result = self.adapter.reproduce_and_fix(72, self.payload, evidence)
        self.assertTrue(result["validated"], result.get("reason"))
        self.assertEqual(result["changedFiles"], [
            "src/main/java/First.java", "src/main/java/Second.java"])
        original = repair.subprocess.run

        def remote(command, **kwargs):
            if "ls-remote" in command:
                return subprocess.CompletedProcess(
                    command, 0, self.payload["headSha"] + "\trefs/heads/feature\n", "")
            if "push" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(command, **kwargs)

        with patch.object(repair.subprocess, "run", side_effect=remote):
            published = self.adapter.publish(
                72, self.payload,
                {**evidence, "head_repo": "test/repair", "head_ref": "feature",
                 "head_sha": self.payload["headSha"]},
                result,
                lambda: {"headSha": self.payload["headSha"], "headRef": "feature",
                         "complete": True, "otherFailures": []})
        self.assertEqual(published["status"], "pushed-awaiting-ci", published.get("reason"))

    def test_formatter_rejects_java_outside_source_roots(self):
        (self.source / "Other.java").write_text("class Other {  }\n")
        wrapper = self.source / "mvnw"
        wrapper.write_text(wrapper.read_text() +
                           "\nif 'spotless:apply' in sys.argv:\n"
                           "    pathlib.Path('Other.java').write_text('class Other {}\\n')\n")
        self.run_git("add", ".", cwd=self.source)
        self.run_git("commit", "-qm", "out of scope formatter", cwd=self.source)
        self.payload["headSha"] = self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        result = self.adapter.reproduce_and_fix(73, self.payload, self.evidence)
        self.assertFalse(result["validated"])

    def test_formatter_rejects_untracked_resource_and_java_symlink(self):
        for unsafe in ("resource", "symlink"):
            with self.subTest(unsafe=unsafe):
                wrapper = self.source / "mvnw"
                baseline = wrapper.read_text()
                if unsafe == "resource":
                    wrapper.write_text(baseline +
                                       "\nif 'spotless:apply' in sys.argv:\n"
                                       "    pathlib.Path('config.yml').write_text('unsafe')\n")
                else:
                    target = self.source / "src/main/java"
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "Linked.java").symlink_to("../../../Example.java")
                    wrapper.write_text(baseline +
                                       "\nif 'spotless:apply' in sys.argv:\n"
                                       "    pathlib.Path('src/main/java/Linked.java')"
                                       ".write_text('class Linked {}\\n')\n")
                self.run_git("add", ".", cwd=self.source)
                self.run_git("commit", "-qm", unsafe + " fixture", cwd=self.source)
                self.payload["headSha"] = self.run_git(
                    "rev-parse", "HEAD", cwd=self.source).stdout.strip()
                result = self.adapter.reproduce_and_fix(
                    80 if unsafe == "resource" else 81, self.payload, self.evidence)
                self.assertFalse(result["validated"])
                wrapper.write_text(baseline)

    def test_rejects_logged_file_via_symlinked_parent(self):
        root = self.source / "src"
        root.mkdir()
        (root / "test/java").mkdir(parents=True)
        (root / "test/java/Linked.java").write_text("class Linked {  }\n")
        (root / "main").symlink_to("test")
        self.run_git("add", ".", cwd=self.source)
        self.run_git("commit", "-qm", "linked java source root", cwd=self.source)
        self.payload["headSha"] = self.run_git(
            "rev-parse", "HEAD", cwd=self.source).stdout.strip()
        evidence = {"command": ["./mvnw", "-B", "spotless:check"],
                    "violatingFiles": ["src/main/java/Linked.java"]}
        result = self.adapter.reproduce_and_fix(83, self.payload, evidence)
        self.assertFalse(result["validated"])
        self.assertIn("symlink", result["reason"])

    def test_all_logged_violations_must_be_changed_by_formatter(self):
        (self.source / "AlreadyFormatted.java").write_text("class AlreadyFormatted {}\n")
        self.run_git("add", ".", cwd=self.source)
        self.run_git("commit", "-qm", "unchanged logged sample", cwd=self.source)
        self.payload["headSha"] = self.run_git(
            "rev-parse", "HEAD", cwd=self.source).stdout.strip()
        evidence = {**self.evidence, "violatingFiles": [
            "Example.java", "AlreadyFormatted.java"]}
        result = self.adapter.reproduce_and_fix(84, self.payload, evidence)
        self.assertFalse(result["validated"])
        self.assertIn("Not all logged", result["reason"])
    def test_replay_after_commit_preserves_single_commit_and_worktree(self):
        payload = {"headSha": self.head}
        evidence = {**self.evidence, "head_repo": "test/repair",
                    "head_ref": "feature", "head_sha": self.head}
        result = self.adapter.reproduce_and_fix(9, payload, evidence)

        def interrupted_recheck():
            raise KeyboardInterrupt("simulate crash after commit")

        with self.assertRaises(KeyboardInterrupt):
            self.adapter.publish(9, payload, evidence, result, interrupted_recheck)
        worktree = Path(result["worktree"])
        commit = self.run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        replay = self.adapter.reproduce_and_fix(9, payload, evidence)
        self.assertTrue(replay["validated"], replay.get("reason"))
        self.assertEqual(replay["commit"], commit)
        self.assertEqual(replay["worktree"], result["worktree"])
        self.assertEqual(self.run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip(), commit)
        self.assertEqual(len(list(self.worktrees.iterdir())), 1)

    def test_interrupted_push_never_repushes_even_when_remote_is_old(self):
        evidence = {**self.evidence, "head_repo": "test/repair",
                    "head_ref": "feature", "head_sha": self.head}
        result = self.adapter.reproduce_and_fix(11, self.payload, evidence)
        original = repair.subprocess.run
        pushes = []

        def fake_remote(command, **kwargs):
            if "ls-remote" in command:
                return subprocess.CompletedProcess(
                    command, 0, self.head + "\trefs/heads/feature\n", "")
            if "push" in command:
                pushes.append(command)
                raise KeyboardInterrupt("push outcome unknown")
            return original(command, **kwargs)

        snapshot = lambda: {"headSha": self.head, "headRef": "feature",
                            "headRepository": "test/repair", "complete": True,
                            "otherFailures": []}
        with patch.object(repair.subprocess, "run", side_effect=fake_remote):
            with self.assertRaises(KeyboardInterrupt):
                self.adapter.publish(11, self.payload, evidence, result, snapshot)
            resumed = self.adapter.reproduce_and_fix(11, self.payload, evidence)
            self.assertEqual(resumed["commit"], self.run_git(
                "rev-parse", "HEAD", cwd=Path(result["worktree"])).stdout.strip())
            replay = self.adapter.publish(11, self.payload, evidence, result, snapshot)
        self.assertEqual(replay["status"], "reported")
        self.assertIn("uncertain", replay["reason"].lower())
        self.assertEqual(len(pushes), 1)
        self.assertEqual(len(list(self.worktrees.iterdir())), 1)

    def test_multi_repo_workspace_resolves_verified_manifest_url(self):
        root = self.source.parent / "collection"
        root.mkdir()
        (root / "sample").symlink_to(self.source)
        (root / "workspace.json").write_text(json.dumps({
            "repos": [{"name": "sample", "url": "https://github.com/test/repair.git",
                       "ref": "feature"}],
            "workspace_root": ".",
        }))
        adapter = RepairAdapter(root, self.worktrees)
        evidence = {**self.evidence, "head_repo": "test/repair",
                    "head_ref": "feature", "head_sha": self.head}
        result = adapter.reproduce_and_fix(99, {"headSha": self.head}, evidence)
        self.assertFalse(result["validated"])
        self.assertIn("symlink", result["reason"])
        (root / "sample").unlink()
        # Real checkout at the manifest name; source checkout branch remains unchanged.
        self.source.rename(root / "sample")
        self.source = root / "sample"
        adapter = RepairAdapter(root, self.worktrees)
        result = adapter.reproduce_and_fix(99, {"headSha": self.head}, evidence)
        self.assertTrue(result["validated"], result.get("reason"))
        self.assertEqual(self.run_git("symbolic-ref", "--short", "HEAD",
                                      cwd=self.source).stdout.strip(), "feature")

    def test_multi_repo_manifest_cannot_map_wrong_or_missing_fork(self):
        root = self.source.parent / "collection"
        root.mkdir()
        (root / "workspace.json").write_text(json.dumps({
            "repos": [{"name": "sample", "url": "https://github.com/test/repair.git",
                       "ref": "feature"}],
        }))
        adapter = RepairAdapter(root, self.worktrees)
        evidence = {**self.evidence, "head_repo": "someone/fork",
                    "head_ref": "feature", "head_sha": self.head}
        result = adapter.reproduce_and_fix(99, {"headSha": self.head}, evidence)
        self.assertFalse(result["validated"])
        self.assertIn("manifest", result["reason"])
        self.assertFalse(self.worktrees.exists())

    def test_missing_sha_fetches_verified_ref_without_switching_branch(self):
        origin = self.source.parent / "bare.git"
        self.run_git("init", "--bare", "-q", str(origin), cwd=self.source.parent)
        seed = self.source.parent / "seed"
        self.run_git("clone", "-q", str(origin), str(seed), cwd=self.source.parent)
        self.run_git("config", "user.name", "Tester", cwd=seed)
        self.run_git("config", "user.email", "tester@example.test", cwd=seed)
        self.run_git("checkout", "-qb", "feature", cwd=seed)
        shutil.copy2(self.source / "mvnw", seed / "mvnw")
        shutil.copy2(self.source / "Example.java", seed / "Example.java")
        self.run_git("add", ".", cwd=seed)
        self.run_git("commit", "-qm", "remote only", cwd=seed)
        remote_head = self.run_git("rev-parse", "HEAD", cwd=seed).stdout.strip()
        self.run_git("push", "-q", "origin", "feature", cwd=seed)
        self.assertNotEqual(remote_head, self.head)
        original = repair.subprocess.run

        def local_fetch(command, **kwargs):
            if "fetch" in command and "origin" in command:
                command = [str(origin) if token == "origin" else token for token in command]
            return original(command, **kwargs)

        evidence = {**self.evidence, "head_repo": "test/repair",
                    "head_ref": "feature", "head_sha": remote_head}
        with patch.object(repair.subprocess, "run", side_effect=local_fetch):
            result = self.adapter.reproduce_and_fix(77, {"headSha": remote_head}, evidence)
        self.assertTrue(result["validated"], result.get("reason"))
        self.assertEqual(self.run_git("rev-parse", "HEAD", cwd=self.source).stdout.strip(),
                         self.head)

    def test_fetch_mismatched_ref_sha_fails_without_worktree(self):
        missing = "0" * 40
        evidence = {**self.evidence, "head_repo": "test/repair",
                    "head_ref": "feature", "head_sha": missing}
        original = repair.subprocess.run

        def fake_fetch(command, **kwargs):
            if "fetch" in command and "origin" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return original(command, **kwargs)

        with patch.object(repair.subprocess, "run", side_effect=fake_fetch):
            result = self.adapter.reproduce_and_fix(77, {"headSha": missing}, evidence)
        self.assertFalse(result["validated"])
        self.assertFalse(self.worktrees.exists())


if __name__ == "__main__":
    unittest.main()
