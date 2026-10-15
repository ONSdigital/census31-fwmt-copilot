"""Bounded, isolated Spotless repair and guarded publication of a PR head."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


_SHA = re.compile(r"[0-9a-fA-F]{40}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_TASK = re.compile(r"[^A-Za-z0-9_-]+")
_MODULES = re.compile(r"[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*")
_GOALS = {"clean", "test", "verify", "package", "spotless:check"}
_FLAGS = {"-B", "--batch-mode", "-ntp", "--no-transfer-progress", "-q", "--quiet",
          "-am", "--also-make"}
_TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
_PUSH_MARKER = ".repair-push-attempt"


class RepairAdapter:
    """Run deterministic Maven formatting only; never grant a general-purpose shell.

    ``runner_path`` is reserved for a future explicitly sandboxed Copilot runner;
    this adapter deliberately does not execute it. The proven Spotless formatter is
    the sole editor.
    """

    def __init__(self, workspace_root, worktree_root, runner_path):
        self.workspace_root = Path(workspace_root).resolve()
        self.worktree_root = Path(worktree_root).resolve()
        self.runner_path = Path(runner_path) if runner_path is not None else None
        if not self.workspace_root.is_dir():
            raise ValueError("workspace root must be an existing directory")
        if (self.worktree_root == self.workspace_root
                or self.workspace_root in self.worktree_root.parents
                or self.worktree_root in self.workspace_root.parents):
            raise ValueError("worktrees must be outside the source repository")

    @staticmethod
    def _run(args, cwd, *, check=False, timeout=300):
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                    check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Command unavailable or timed out") from error
        if check and result.returncode:
            raise RuntimeError("Required command failed: {}".format(args[0]))
        return result

    def _git(self, cwd, *args, check=True):
        return self._run(["git", "-C", str(cwd), *args], cwd=cwd, check=check, timeout=60)

    @staticmethod
    def _head(payload):
        sha = payload.get("headSha") if isinstance(payload, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ValueError("A full PR head SHA is required")
        return sha.lower()

    @staticmethod
    def _ref(payload, evidence):
        ref = evidence.get("head_ref") or payload.get("headRef")
        if payload.get("headRef") and payload["headRef"] != ref:
            raise ValueError("Payload and verified head branch disagree")
        if (not isinstance(ref, str) or not _REF.fullmatch(ref)
                or ref.endswith(".") or ref.endswith("/") or ref.endswith(".lock")
                or ".." in ref or "//" in ref or "/." in ref or "@{" in ref):
            raise ValueError("An unambiguous PR head branch is required")
        return ref

    @staticmethod
    def _repository(payload, evidence):
        repository = evidence.get("head_repo") or payload.get("headRepository")
        if payload.get("headRepository") and payload["headRepository"] != repository:
            raise ValueError("Payload and verified head repository disagree")
        if (not isinstance(repository, str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                    repository)):
            raise ValueError("Verified PR head repository owner/name is required")
        return repository

    @staticmethod
    def _task(task_id):
        if isinstance(task_id, int) and not isinstance(task_id, bool) and task_id > 0:
            return str(task_id), "task-" + str(task_id)
        if isinstance(task_id, str) and task_id and len(task_id) <= 128:
            slug = _TASK.sub("-", task_id).strip("-")[:36] or "task"
            return task_id, "task-{}-{}".format(
                slug, hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12])
        raise ValueError("A positive queue task ID or bounded string is required")

    @staticmethod
    def _marker(worktree):
        return worktree / _PUSH_MARKER

    def _worktree(self, task_id, sha):
        _, slug = self._task(task_id)
        return self.worktree_root / "{}-{}".format(slug, sha[:12])

    @staticmethod
    def _github_url(url, repository):
        if not isinstance(url, str):
            return False
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
                and parsed.path.lower() == "/" + repository.lower() + ".git"
                and not parsed.query and not parsed.fragment
                and not parsed.username and not parsed.password)

    def _source(self, payload, evidence):
        repository = self._repository(payload, evidence)
        if (self.workspace_root / ".git").exists():
            source = self.workspace_root
        else:
            manifest_file = self.workspace_root / "workspace.json"
            if manifest_file.is_symlink() or not manifest_file.is_file():
                raise ValueError("Workspace manifest is missing or a symlink")
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                entries = manifest["repos"]
                if not isinstance(entries, list):
                    raise ValueError("Workspace repos must be a list")
            except (ValueError, KeyError, TypeError, OSError) as error:
                raise ValueError("Invalid workspace manifest") from error
            matching = [entry for entry in entries if isinstance(entry, dict)
                        and self._github_url(entry.get("url"), repository)]
            if len(matching) != 1:
                raise ValueError("Verified PR head repository missing or ambiguous in manifest")
            name = matching[0].get("name")
            if (not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                    or name in (".", "..")):
                raise ValueError("Unsafe repository checkout name in manifest")
            source = self.workspace_root / name
            if source.is_symlink() or not source.is_dir() or source.resolve().parent != self.workspace_root:
                raise ValueError("Manifest checkout is missing or symlinked")
        if not (source / ".git").exists() or (source / ".git").is_symlink():
            raise ValueError("Verified PR head checkout is not a Git repository")
        origin = self._git(source, "remote", "get-url", "origin").stdout.strip()
        if not self._github_url(origin, repository):
            raise ValueError("Checkout origin differs from verified PR head repository")
        return source

    def _check_worktree(self, source, worktree):
        common = self._git(worktree, "rev-parse", "--git-common-dir").stdout.strip()
        source_common = self._git(
            source, "rev-parse", "--git-common-dir").stdout.strip()
        if (worktree.is_symlink()
                or self._git(worktree, "rev-parse", "--show-toplevel").stdout.strip()
                != str(worktree)
                or (worktree / common).resolve()
                    != (source / source_common).resolve()):
            raise RuntimeError("Existing worktree does not belong to the source repository")

    def _committed_result(self, task_id, sha, worktree, allowed, command):
        current = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
        if self._git(worktree, "rev-parse", "HEAD^").stdout.strip().lower() != sha:
            raise RuntimeError("Existing worktree has an unexpected parent")
        marker = self._marker(worktree)
        if marker.is_symlink():
            raise RuntimeError("Push marker cannot be a symlink")
        status = self._git(worktree, "status", "--porcelain=v1", "-z",
                           "--untracked-files=all").stdout
        if status not in ("", "?? " + _PUSH_MARKER + "\x00"):
            raise RuntimeError("Existing committed worktree has unexpected changes")
        task, _ = self._task(task_id)
        message = self._git(worktree, "log", "-1", "--format=%B").stdout
        if ("Repair-Task: " + task + "\n") not in message or _TRAILER not in message:
            raise RuntimeError("Existing commit is not this task's repair")
        changed = self._git(worktree, "diff", "--name-only", "--diff-filter=ACDMRT",
                            sha, current).stdout.splitlines()
        if (not changed or len(changed) != len(set(changed))
                or not allowed <= set(changed)
                or any(not self._java_scope(name, allowed) for name in changed)):
            raise RuntimeError("Existing repair commit changed unproven paths")
        for name in changed:
            self._safe_java_file(worktree, name)
        if self._git(worktree, "diff", "--name-status", sha, current).stdout.splitlines() != [
                "M\t" + name for name in sorted(changed)]:
            raise RuntimeError("Existing repair changed file modes or paths")
        if self._git(worktree, "diff", "--summary", sha, current).stdout:
            raise RuntimeError("Existing repair changed file metadata")
        self._git(worktree, "diff", "--check", sha, current)
        return {"status": "validated", "validated": True, "reason": None,
                "commit": current, "worktree": str(worktree), "headSha": sha,
                "taskId": task_id, "changedFiles": sorted(changed), "command": command}

    @staticmethod
    def _files(evidence):
        files = evidence.get("violatingFiles") if isinstance(evidence, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError("Evidence must identify the violating Java files")
        for name in files:
            if (not isinstance(name, str) or not name or "\\" in name
                    or PurePosixPath(name).is_absolute()
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or PurePosixPath(name).suffix != ".java"):
                raise ValueError("Only relative Java file paths are permitted")
        if len(files) != len(set(files)):
            raise ValueError("Duplicate violating file paths")
        return set(files)

    @staticmethod
    def _java_scope(name, allowed):
        path = PurePosixPath(name)
        return (name in allowed or (
            path.suffix == ".java"
            and any(path.parts[index:index + 3] in
                    (("src", "main", "java"), ("src", "test", "java"))
                    for index in range(len(path.parts) - 2))))

    @staticmethod
    def _safe_java_file(worktree, name):
        path = worktree
        for component in PurePosixPath(name).parts:
            path = path / component
            if path.is_symlink():
                raise RuntimeError("Java source path contains symlink")
        if not path.is_file():
            raise RuntimeError("Java source file is missing")

    @staticmethod
    def _command(evidence):
        command = evidence.get("command") if isinstance(evidence, dict) else None
        if not isinstance(command, list) or len(command) < 2 or len(command) > 16:
            raise ValueError("A bounded Maven command list is required")
        if command[0] not in ("./mvnw", "mvn") or any(
                not isinstance(token, str) or not token or "\x00" in token
                for token in command):
            raise ValueError("Only Maven or the repository wrapper is permitted")
        modules = []
        goals = []
        index = 1
        while index < len(command):
            token = command[index]
            if token in ("-pl", "--projects"):
                if (modules or index + 1 >= len(command)
                        or not _MODULES.fullmatch(command[index + 1])
                        or any(part in (".", "..") for part in command[index + 1].split(","))):
                    raise ValueError("Invalid Maven module selection")
                modules = [token, command[index + 1]]
                index += 2
            elif token in _FLAGS:
                index += 1
            elif token in _GOALS:
                goals.append(token)
                index += 1
            else:
                raise ValueError("Maven token is not allowlisted")
        if goals.count("spotless:check") != 1:
            raise ValueError("Exactly one Spotless check goal is required")
        return command, modules

    @staticmethod
    def _spotless_violation(result):
        output = result.stdout + "\n" + result.stderr
        if result.returncode == 0:
            return False
        if not re.search(r"spotless(?:-maven-plugin)?", output, re.I):
            return False
        if not re.search(r"(?:format(?:ting)? violations?|not properly formatted)", output, re.I):
            return False
        failed_goals = re.findall(r"Failed to execute goal ([^\s]+)", output, re.I)
        if not failed_goals or any(
                "spotless" not in goal.lower() or ":check" not in goal.lower()
                for goal in failed_goals):
            return False
        if re.search(r"Tests run:\s*\d+,\s*Failures:\s*[1-9]", output, re.I):
            return False
        return True

    def _changed_files(self, worktree, allowed):
        status = self._git(worktree, "status", "--porcelain=v1", "-z",
                           "--untracked-files=all").stdout
        entries = [entry for entry in status.split("\x00") if entry]
        if not entries:
            raise RuntimeError("Formatter made no changes")
        changed = []
        for entry in entries:
            code, name = entry[:2], entry[3:]
            if code != " M" or not self._java_scope(name, allowed):
                raise RuntimeError("Formatter changed an unproven or non-Java path: "
                                   + repr((code, name)))
            self._safe_java_file(worktree, name)
            changed.append(name)
        if len(changed) != len(set(changed)):
            raise RuntimeError("Ambiguous formatter diff")
        if not allowed <= set(changed):
            raise RuntimeError("Not all logged Spotless violations changed")
        self._git(worktree, "diff", "--check")
        return sorted(changed)

    def reproduce_and_fix(self, task_id, payload, evidence):
        """Return validated/commit/worktree/reason; never commit or push here."""
        report = {"validated": False, "commit": None, "worktree": None}
        try:
            return self._reproduce_and_fix(task_id, payload, evidence, report)
        except (ValueError, RuntimeError) as error:
            return {**report, "reason": str(error)}

    def _reproduce_and_fix(self, task_id, payload, evidence, report):
        sha = self._head(payload)
        if not isinstance(evidence, dict):
            raise ValueError("Verified CI evidence is required")
        evidence_sha = evidence.get("head_sha")
        if evidence_sha is not None and (
                not isinstance(evidence_sha, str) or evidence_sha.lower() != sha):
            raise ValueError("Evidence and event head revisions disagree")
        command, modules = self._command(evidence)
        allowed = self._files(evidence)
        self._task(task_id)
        source = self._source(payload, evidence)
        if self._git(source, "cat-file", "-e", sha + "^{commit}", check=False).returncode:
            ref = self._ref(payload, evidence)
            self._git(source, "fetch", "--no-tags", "origin", "refs/heads/" + ref)
            if self._git(source, "rev-parse", "FETCH_HEAD").stdout.strip().lower() != sha:
                raise RuntimeError("Fetched PR branch does not match verified head SHA")
        if self._git(source, "rev-parse", sha).stdout.strip().lower() != sha:
            raise RuntimeError("Requested PR head could not be resolved")
        if command[0] == "mvn" and not shutil.which("mvn"):
            raise RuntimeError("Maven is unavailable")
        if command[0] == "./mvnw" and (
                self._git(source, "ls-tree", sha, "mvnw").stdout.strip() == ""):
            raise RuntimeError("Tracked Maven wrapper is unavailable")

        self.worktree_root.mkdir(parents=True, exist_ok=True)
        worktree = self._worktree(task_id, sha)
        report["worktree"] = str(worktree)
        if worktree.exists() or worktree.is_symlink():
            self._check_worktree(source, worktree)
            current = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            if current.lower() != sha:
                return self._committed_result(task_id, sha, worktree, allowed, command)
            raise RuntimeError("Existing uncommitted worktree needs operator inspection")
        self._git(source, "worktree", "add", "--detach", str(worktree), sha)
        if command[0] == "./mvnw" and (
                not (worktree / "mvnw").is_file() or (worktree / "mvnw").is_symlink()):
            raise RuntimeError("Maven wrapper is unavailable in worktree")
        for name in allowed:
            self._safe_java_file(worktree, name)
        reproduction = self._run(command, worktree, timeout=900)
        if not self._spotless_violation(reproduction):
            raise RuntimeError("Spotless formatting failure was not exclusively reproduced")
        if self._git(worktree, "status", "--porcelain").stdout:
            raise RuntimeError("Reproduction changed the worktree")

        prefix = [command[0], "-B", "-ntp", *modules]
        if "-am" in command or "--also-make" in command:
            prefix.append("-am")
        self._run([*prefix, "spotless:apply"], worktree, check=True, timeout=900)
        changed = self._changed_files(worktree, allowed)
        self._run(command, worktree, check=True, timeout=900)
        self._run([*prefix, "test"], worktree, check=True, timeout=900)
        if self._changed_files(worktree, allowed) != changed:
            raise RuntimeError("Verification changed formatter output")
        return {"status": "validated", "validated": True, "reason": None,
                "commit": None, "worktree": str(worktree), "headSha": sha,
                "taskId": task_id, "changedFiles": changed, "command": command}

    def publish(self, task_id, payload, evidence, result, recheck_callable):
        """Commit then recheck head/checks/remote before a non-force push.

        Failures return status=reported, retaining worktree/commit when available.
        ``recheck_callable()`` must return a complete authoritative PR snapshot.
        A push error is ambiguous and must never be blindly retried.
        """
        report = {"status": "reported", "worktree": (
            result.get("worktree") if isinstance(result, dict) else None),
            "commit": None}
        try:
            return self._publish(task_id, payload, evidence, result, recheck_callable, report)
        except (ValueError, RuntimeError) as error:
            return {**report, "reason": str(error)}
        except Exception as error:
            return {**report, "reason": "Publication recheck failed ({})".format(
                type(error).__name__)}

    def _publish(self, task_id, payload, evidence, result, recheck_callable, report):
        sha = self._head(payload)
        ref = self._ref(payload, evidence)
        repository = self._repository(payload, evidence)
        source = self._source(payload, evidence)
        allowed = self._files(evidence)
        command, _ = self._command(evidence)
        if (not isinstance(result, dict) or result.get("validated") is not True
                or result.get("taskId") != task_id or result.get("headSha") != sha
                or result.get("command") != command):
            raise ValueError("Matching validated repair result is required")
        worktree = Path(result.get("worktree", "")).resolve()
        if worktree != self._worktree(task_id, sha) or not worktree.is_dir():
            raise ValueError("Validated worktree is outside the allocated root")
        self._check_worktree(source, worktree)
        current = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
        if current.lower() == sha:
            changed = self._changed_files(worktree, allowed)
            if changed != result.get("changedFiles"):
                raise RuntimeError("Validated diff has changed")
            self._git(worktree, "add", "--", *changed)
            self._git(worktree, "diff", "--cached", "--check")
            task, _ = self._task(task_id)
            self._run(["git", "-c", "user.name=Copilot",
                       "-c", "user.email=223556219+Copilot@users.noreply.github.com",
                       "-C", str(worktree), "commit", "-m",
                       "style: apply Spotless formatting", "-m", _TRAILER,
                       "-m", "Repair-Task: " + task],
                      worktree, check=True, timeout=60)
        committed = self._committed_result(task_id, sha, worktree, allowed, command)
        commit_sha = committed["commit"]
        report["commit"] = commit_sha
        if committed["changedFiles"] != result.get("changedFiles"):
            raise RuntimeError("Committed repair differs from validated diff")
        if self._marker(worktree).exists():
            raise RuntimeError("Previous push attempt has uncertain outcome; do not retry")

        snapshot = recheck_callable()
        if (not isinstance(snapshot, dict) or snapshot.get("headSha") != sha
                or snapshot.get("headRef") != ref or snapshot.get("complete") is not True
                or snapshot.get("otherFailures") != []
                or (snapshot.get("headRepository") is not None
                    and snapshot["headRepository"] != repository)):
            raise RuntimeError("PR head or checks changed; commit retained without push")
        origin = self._git(worktree, "remote", "get-url", "origin").stdout.strip()
        if not re.fullmatch(
                r"(?:https://github\.com/|git@github\.com:)"
                + re.escape(repository) + r"\.git/?", origin, re.I):
            raise RuntimeError("Origin is not the verified PR head repository")
        remote = self._git(worktree, "ls-remote", "origin", "refs/heads/" + ref)
        if remote.stdout.strip() != "{}\trefs/heads/{}".format(sha, ref):
            raise RuntimeError("Remote branch changed; commit retained without push")
        try:
            with self._marker(worktree).open("x", encoding="ascii") as marker:
                marker.write(commit_sha + "\n")
                marker.flush()
                os.fsync(marker.fileno())
        except FileExistsError as error:
            raise RuntimeError("Previous push attempt has uncertain outcome") from error
        self._git(worktree, "push", "--no-force", "origin", "HEAD:refs/heads/" + ref)
        return {"status": "pushed-awaiting-ci", "reason": None,
                "worktree": str(worktree), "headSha": sha,
                "commit": commit_sha, "commitSha": commit_sha}
