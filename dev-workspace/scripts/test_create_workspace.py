import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("create_workspace.py")
ASKPASS = Path(__file__).with_name("git_askpass.py")


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


class CreateWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def make_repo(self, name):
        source = self.root / f"{name}-source"
        git("init", source)
        git("-C", source, "checkout", "-b", "main")
        git("-C", source, "config", "user.email", "test@example.com")
        git("-C", source, "config", "user.name", "Workspace Test")
        (source / "content.txt").write_text("main", encoding="utf-8")
        git("-C", source, "add", "content.txt")
        git("-C", source, "commit", "-m", "main")
        git("-C", source, "checkout", "-b", "feature")
        (source / "content.txt").write_text("feature", encoding="utf-8")
        git("-C", source, "commit", "-am", "feature")
        bare = self.root / f"{name}.git"
        git("clone", "--bare", source, bare)
        return str(bare)

    def run_script(self, config, workspace):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--workspace", str(workspace), "--config", str(config)],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(SCRIPT.parents[2])),
        )

    def test_copies_config_and_checks_out_documents_and_repos(self):
        docs_url = self.make_repo("census31-fwmt-docs")
        repo_url = self.make_repo("census31-fwmt-common")
        config = self.root / "source.json"
        data = {
            "workspace_root": ".",
            "documents": {"url": docs_url, "ref": "main"},
            "repos": [{"name": "census31-fwmt-common", "url": repo_url, "ref": "feature"}],
        }
        config.write_text(json.dumps(data), encoding="utf-8")
        workspace = self.root / "new" / "workspace"

        result = self.run_script(config, workspace)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((workspace / "workspace.json").read_text()), data)
        self.assertEqual((workspace / "census31-fwmt-docs" / "content.txt").read_text(), "main")
        self.assertEqual((workspace / "census31-fwmt-common" / "content.txt").read_text(), "feature")
        self.assertEqual(git("-C", workspace / "census31-fwmt-common", "branch", "--show-current").stdout.strip(), "feature")

    def test_checks_out_a_commit_ref(self):
        repo_url = self.make_repo("pinned-repo")
        commit = git("-C", repo_url, "rev-parse", "refs/heads/main").stdout.strip()
        config = self.root / "source.json"
        config.write_text(json.dumps({
            "documents": {},
            "repos": [{"name": "pinned-repo", "url": repo_url, "ref": commit}],
        }))
        workspace = self.root / "workspace"

        result = self.run_script(config, workspace)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((workspace / "pinned-repo" / "content.txt").read_text(), "main")
        self.assertEqual(git("-C", workspace / "pinned-repo", "rev-parse", "HEAD").stdout.strip(), commit)

    def test_rejects_unsafe_repo_name_before_creating_workspace(self):
        config = self.root / "source.json"
        config.write_text(json.dumps({"documents": {}, "repos": [{"name": "../outside", "url": "unused", "ref": "main"}]}))
        workspace = self.root / "workspace"

        result = self.run_script(config, workspace)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(workspace.exists())
        self.assertFalse((self.root / "outside").exists())

    def test_does_not_overwrite_existing_workspace(self):
        config = self.root / "source.json"
        config.write_text(json.dumps({"documents": {}, "repos": []}))
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "keep.txt").write_text("unchanged", encoding="utf-8")

        result = self.run_script(config, workspace)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((workspace / "keep.txt").read_text(), "unchanged")
        self.assertFalse((workspace / "workspace.json").exists())

    def test_rejects_duplicate_checkout_names_before_creating_workspace(self):
        config = self.root / "source.json"
        config.write_text(json.dumps({
            "documents": {"url": "https://example.org/docs.git", "ref": "main"},
            "repos": [{"name": "docs", "url": "https://example.org/docs.git", "ref": "main"}],
        }))
        workspace = self.root / "workspace"

        result = self.run_script(config, workspace)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(workspace.exists())

    def test_reports_git_failure(self):
        config = self.root / "source.json"
        config.write_text(json.dumps({
            "documents": {},
            "repos": [{"name": "missing", "url": str(self.root / "missing.git"), "ref": "main"}],
        }))
        workspace = self.root / "workspace"

        result = self.run_script(config, workspace)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Workspace creation failed", result.stderr)
        self.assertFalse(workspace.exists())

    def test_checks_git_before_creating_workspace(self):
        config = self.root / "source.json"
        config.write_text(json.dumps({"documents": {}, "repos": []}))
        workspace = self.root / "workspace"
        environment = dict(os.environ, PATH=str(self.root / "no-git"), PYTHONPATH=str(SCRIPT.parents[2]))

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--workspace", str(workspace), "--config", str(config)],
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git", result.stderr)
        self.assertFalse(workspace.exists())

    def test_token_file_answers_git_credential_prompts(self):
        token_file = self.root / "token"
        token_file.write_text("example-token\n", encoding="utf-8")
        environment = dict(os.environ, GIT_ASKPASS=str(ASKPASS),
                           GIT_TERMINAL_PROMPT="0", GITHUB_TOKEN_FILE=str(token_file))

        result = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("username=x-access-token", result.stdout)
        self.assertIn("password=example-token", result.stdout)

        result = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            input="protocol=https\nhost=example.com\n\n",
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("example-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()