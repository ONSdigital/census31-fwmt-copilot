"""Fail-closed, read-only evidence gate for a PR-head Cloud Build failure.

``inspect`` returns the command to reproduce only after checking the PR, the
check run, every page of PR-head checks, build metadata, source config and logs.
``recheck(payload, inspected_evidence)`` is a fresh PR/check snapshot and must
be called immediately before publication; it does not authorize a push by itself.
"""

import ast
import base64
import binascii
import json
import re
import shlex
import subprocess
from urllib.parse import parse_qs, quote, urlsplit


SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PROJECT = re.compile(r"(?:[a-z][a-z0-9-]{4,62}|[0-9]{6,20})\Z")
REGION = re.compile(r"[a-z]+(?:-[a-z0-9]+)+\Z")
BUILD_ID = re.compile(r"[0-9a-fA-F-]{36}\Z")
PATH = re.compile(r"[A-Za-z0-9_./-]+\Z")
GOAL = re.compile(r"Failed to execute goal\s+([^\s(]+)", re.I)
SPOTLESS_GOAL = re.compile(r"com\.diffplug\.spotless:spotless-maven-plugin:[^:\s]+:check\Z")
BUILD_FAILURE = re.compile(r"^ERROR: build step (\d+) .*failed", re.M | re.I)
SHELL = re.compile(r"[;&|`$<>\\\n\r]")
VIOLATION_MARKER = re.compile(r"format violations:\s*$", re.I)
VIOLATION_FILE = re.compile(r"^\[ERROR\][ \t]+([A-Za-z0-9_./-]+\.java)[ \t]*$")
VIOLATION_END = re.compile(
    r"^\[(?:ERROR|INFO)\]\s+(?:Run\b|BUILD FAILURE\b|->\s*\[Help|For more information\b)",
    re.I,
)
MAX_LOG = 2_000_000
MAX_PAGES = 50
MAX_VIOLATIONS = 100


def _repository(value):
    return (isinstance(value, str) and REPO.fullmatch(value) is not None
            and all(part not in (".", "..") and not part.startswith("-")
                    for part in value.split("/")))


def _branch(value):
    return (isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", value) is not None
            and not value.startswith(("-", "/", "."))
            and not value.endswith(("/", ".", ".lock"))
            and all(part and not part.startswith(".") for part in value.split("/")))


def _run_json(args):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


class GithubCLI:
    """Use gh's configured identity; arguments never pass through a shell."""

    def pull(self, repo, number):
        return _run_json(["gh", "api", f"repos/{repo}/pulls/{number}"])

    def check_run(self, repo, check_id):
        return _run_json(["gh", "api", f"repos/{repo}/check-runs/{check_id}"])

    def check_runs(self, repo, sha, page):
        data = _run_json(["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}"])
        if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
            raise ValueError("Missing check runs")
        return data["check_runs"]

    def config_file(self, repo, path, ref):
        encoded = quote(path, safe="/")
        data = _run_json(["gh", "api", f"repos/{repo}/contents/{encoded}?ref={ref}"])
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise ValueError("Missing immutable build config")
        return base64.b64decode("".join(data["content"].split()), validate=True).decode("utf-8")


class CloudBuildCLI:
    """Read a specific project, region and build; never execute build commands."""

    def build(self, build_id, project, region):
        return _run_json(["gcloud", "builds", "describe", build_id, f"--project={project}",
                          f"--region={region}", "--format=json"])

    def trigger(self, trigger_id, project, region):
        return _run_json(["gcloud", "builds", "triggers", "describe", trigger_id,
                          f"--project={project}", f"--region={region}", "--format=json"])

    def project(self, project_number):
        return _run_json(["gcloud", "projects", "describe", project_number, "--format=json"])

    def logs(self, build_id, project, region):
        proc = subprocess.run(["gcloud", "builds", "log", build_id,
                               f"--project={project}", f"--region={region}"],
                              capture_output=True, text=True, timeout=60, check=True)
        return proc.stdout


def _build_link(url):
    if not isinstance(url, str):
        raise ValueError("Missing Cloud Build URL")
    parsed = urlsplit(url)
    segments = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)
    matrix_region = None
    if len(segments) == 3 and segments[1].startswith("builds;region="):
        segments[1], matrix_region = "builds", segments[1][len("builds;region="):]
    if (parsed.scheme != "https" or parsed.netloc != "console.cloud.google.com"
            or len(segments) != 3 or segments[:2] != ["cloud-build", "builds"]
            or not BUILD_ID.fullmatch(segments[2])
            or len(query.get("project", [])) != 1
            or (matrix_region is None and len(query.get("region", [])) != 1)
            or (matrix_region is not None and query.get("region"))):
        raise ValueError("Build link lacks an unambiguous project, region or build ID")
    project, region = query["project"][0], matrix_region or query["region"][0]
    if not PROJECT.fullmatch(project) or not REGION.fullmatch(region):
        raise ValueError("Invalid Cloud Build project or region")
    return segments[2], project, region


def _config_steps(source):
    """Read only plain YAML/JSON literal step fields; reject templates/complex YAML."""
    if not isinstance(source, str) or len(source) > 200_000:
        raise ValueError("Missing or oversized build configuration")
    try:
        decoded = json.loads(source)
        if isinstance(decoded, dict) and isinstance(decoded.get("steps"), list):
            return decoded["steps"]
    except ValueError:
        pass
    steps = []
    inside = False
    lines = source.splitlines()
    index = 0
    step_indent = None
    while index < len(lines):
        line = lines[index]
        index += 1
        if line == "steps:":
            inside = True
            continue
        if not inside:
            continue
        if line and not line.startswith((" ", "-")):
            break
        match = re.fullmatch(r"(\s*)-\s+(name|id):\s*['\"]?([A-Za-z0-9._:/ -]+)['\"]?\s*", line)
        if match and step_indent is None:
            step_indent = len(match.group(1))
        if match and len(match.group(1)) != step_indent:
            match = None
        if match:
            steps.append({match.group(2): match.group(3)})
            continue
        if not steps:
            continue
        match = re.fullmatch(
            r" {" + str(step_indent + 2) + r"}(name|id|args|entrypoint|script):\s*(.*?)\s*", line)
        if match:
            key, raw = match.groups()
            if key == "args":
                if raw:
                    try:
                        steps[-1][key] = ast.literal_eval(raw)
                    except (SyntaxError, ValueError, RecursionError):
                        raise ValueError("Unsupported build config args") from None
                else:
                    args = []
                    item_prefix = " " * (step_indent + 4) + "- "
                    while index < len(lines) and lines[index].startswith(item_prefix):
                        item = lines[index][len(item_prefix):]
                        index += 1
                        if item == "|":
                            block = []
                            while (index < len(lines)
                                   and (not lines[index].strip()
                                        or len(lines[index]) - len(lines[index].lstrip(" "))
                                        > step_indent + 4)):
                                block.append(lines[index])
                                index += 1
                            nonempty = [len(value) - len(value.lstrip(" ")) for value in block
                                        if value.strip()]
                            if not nonempty:
                                raise ValueError("Empty build script")
                            margin = min(nonempty)
                            args.append("\n".join(value[margin:] if value.strip() else ""
                                                  for value in block).rstrip("\n") + "\n")
                        else:
                            args.append(item)
                    steps[-1][key] = args
            else:
                steps[-1][key] = raw.strip("'\"")
    if not steps:
        raise ValueError("No verifiable steps in build config")
    return steps


def _command(build_step, config_step):
    if not isinstance(build_step, dict) or not isinstance(config_step, dict):
        raise ValueError("Malformed build step")
    if build_step.get("name") != config_step.get("name"):
        raise ValueError("Build step differs from immutable config")
    configured_args = config_step.get("args")
    if (config_step.get("entrypoint") == "bash" and isinstance(configured_args, list)
            and len(configured_args) == 2 and isinstance(configured_args[1], str)):
        configured_args = [configured_args[0],
                           configured_args[1].replace("$${EFFECTIVE_VERSION}",
                                                       "${EFFECTIVE_VERSION}")]
    if build_step.get("args") != configured_args:
        raise ValueError("Build args differ from immutable config")
    if config_step.get("script") or build_step.get("script"):
        raise ValueError("Script-based build steps cannot be safely reproduced")
    if config_step.get("entrypoint") and config_step["entrypoint"] != build_step.get("entrypoint"):
        raise ValueError("Build entrypoint differs from config")
    if build_step.get("entrypoint") == "bash":
        script_args = build_step.get("args")
        if (not isinstance(script_args, list) or len(script_args) != 2
                or script_args[0] != "-ceu" or not isinstance(script_args[1], str)):
            raise ValueError("Unsupported build script")
        script = script_args[1].strip().splitlines()
        if (len(script) != 2
                or script[0] != 'EFFECTIVE_VERSION="$(cat /workspace/effective-version.txt)"'):
            raise ValueError("Build script cannot be safely reproduced")
        ci_setup = '-s /workspace/settings.xml -Drevision="${EFFECTIVE_VERSION}"'
        if script[1].count(ci_setup) != 1:
            raise ValueError("Unknown CI Maven setup")
        local = script[1].replace(ci_setup, "", 1)
        if any(char in local for char in ";&|`$<>\\\n\r"):
            raise ValueError("Shell metacharacters in Maven command")
        tokens = shlex.split(local)
        if not tokens or tokens.pop(0) != "mvn":
            raise ValueError("Build script does not run Maven directly")
        args = tokens
    elif build_step.get("entrypoint") == "mvn":
        args = build_step.get("args")
    else:
        raise ValueError("Failed step is not a verifiable Maven invocation")
    if (not isinstance(args, list) or not args or len(args) > 20
            or any(not isinstance(arg, str) or len(arg) > 200 or SHELL.search(arg) for arg in args)):
        raise ValueError("Unsafe Maven arguments")
    allowed_flags = {"-B", "--batch-mode", "-ntp", "--no-transfer-progress", "-q"}
    goals = [arg for arg in args if arg not in allowed_flags]
    lifecycle = {"clean": 0, "test": 1, "package": 2, "verify": 3}
    if (not goals or goals[-1] != "spotless:check" or goals.count("spotless:check") != 1
            or any(goal not in lifecycle for goal in goals[:-1])
            or len(goals[:-1]) != len(set(goals[:-1]))
            or [lifecycle[goal] for goal in goals[:-1]]
            != sorted(lifecycle[goal] for goal in goals[:-1])):
        raise ValueError("Failed command contains other or unsupported goals")
    return ["mvn", *args]


def _logs_confirm(log, failed_index):
    if (not isinstance(log, str) or not log or len(log) > MAX_LOG
            or "truncated" in log.lower()):
        raise ValueError("Missing or truncated build logs")
    marker = f"Starting Step #{failed_index}"
    end = f"Finished Step #{failed_index}"
    if log.count(marker) != 1 or log.count(end) != 1:
        raise ValueError("Failed step log is incomplete")
    section = log.split(marker, 1)[1].split(end, 1)[0]
    section = re.sub(r"(?m)^Step #\d+ - \"[^\n\"]{1,100}\": ", "", section)
    goals = GOAL.findall(section)
    if len(goals) != 1 or not SPOTLESS_GOAL.fullmatch(goals[0]):
        raise ValueError("Spotless check was not the proven failed Maven goal")
    if len(GOAL.findall(log)) != 1 or len(BUILD_FAILURE.findall(log)) != 1:
        raise ValueError("Additional or missing goal/build failures")
    if int(BUILD_FAILURE.findall(log)[0]) != failed_index:
        raise ValueError("Build log failure disagrees with step status")
    lines = section.splitlines()
    markers = [i for i, line in enumerate(lines) if VIOLATION_MARKER.search(line)]
    if len(markers) != 1 or "Failed to execute goal" not in lines[markers[0]]:
        raise ValueError("Missing unique Spotless format-violation marker")
    paths = []
    for line in lines[markers[0] + 1:]:
        if VIOLATION_END.match(line):
            break
        match = VIOLATION_FILE.fullmatch(line)
        if not match:
            continue
        path = match.group(1)
        if (len(path) > 240 or path.startswith(("/", "."))
                or any(not part or part.startswith(".") for part in path.split("/"))
                or path in paths or len(paths) >= MAX_VIOLATIONS):
            raise ValueError("Unsafe or duplicate Spotless violation path")
        paths.append(path)
    if not paths:
        raise ValueError("Spotless log does not identify safe Java violation paths")
    return paths


def _config_identity(build, cloud, payload, project, region):
    provenance = build.get("sourceProvenance") or {}
    source = provenance.get("resolvedRepoSource") or {}
    git_source = provenance.get("resolvedGitSource") or {}
    config_repo, config_sha, config_path = (payload.get(key) for key in
                                            ("configRepository", "configSha", "configPath"))
    trigger_id = build.get("buildTriggerId")
    if (not any(value is not None for value in (config_repo, config_sha, config_path))
            and git_source):
        git_url = git_source.get("url")
        git_revision = git_source.get("revision")
        config_path = (build.get("substitutions") or {}).get("TRIGGER_BUILD_CONFIG_PATH")
        if (not isinstance(git_url, str)
                or not git_url.startswith("https://github.com/")
                or not git_url.endswith(".git")
                or git_revision != payload.get("headSha")
                or git_url != f"https://github.com/{payload['repository']}.git"):
            raise ValueError("Build Git source does not match PR head")
        config_repo, config_sha = payload["repository"], git_revision
        return _validated_config(config_repo, config_sha, config_path)
    if trigger_id is not None:
        if not isinstance(trigger_id, str) or not BUILD_ID.fullmatch(trigger_id):
            raise ValueError("Invalid Cloud Build trigger ID")
        trigger = cloud.trigger(trigger_id, project, region)
        github = trigger.get("github") if isinstance(trigger, dict) else None
        if (trigger.get("id") != trigger_id or not isinstance(github, dict)
                or not isinstance(github.get("owner"), str)
                or not isinstance(github.get("name"), str)):
            raise ValueError("Cannot resolve trusted GitHub build trigger")
        resolved_repo = github["owner"] + "/" + github["name"]
        resolved_path = trigger.get("filename")
        if config_repo is not None and config_repo != resolved_repo:
            raise ValueError("Build config repo differs from trigger")
        if config_path is not None and config_path != resolved_path:
            raise ValueError("Build config path differs from trigger")
        config_repo, config_path = resolved_repo, resolved_path
        if config_sha is None:
            config_sha = source.get("commitSha")
    if source.get("repoName") != config_repo or source.get("commitSha") != config_sha:
        raise ValueError("Cannot verify immutable build config revision")
    return _validated_config(config_repo, config_sha, config_path)


def _validated_config(config_repo, config_sha, config_path):
    if (not _repository(config_repo)
            or not isinstance(config_sha, str) or not SHA.fullmatch(config_sha)
            or not isinstance(config_path, str) or not PATH.fullmatch(config_path)
            or config_path.startswith("/") or ".." in config_path.split("/")):
        raise ValueError("Cannot verify immutable build config revision")
    return config_repo, config_sha, config_path


class EvidenceClient:
    def __init__(self, github=None, cloud=None):
        self.github = github if github is not None else GithubCLI()
        self.cloud = cloud if cloud is not None else CloudBuildCLI()

    def _identity(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Missing event payload")
        repo, sha, number, check_id = (payload.get(key) for key in
                                        ("repository", "headSha", "prNumber", "checkRunId"))
        if (not _repository(repo)
                or not isinstance(sha, str) or not SHA.fullmatch(sha)
                or type(number) is not int or number <= 0
                or type(check_id) is not int or check_id <= 0
                or payload.get("source") != "google-cloud-build"
                or payload.get("conclusion") != "failure"):
            raise ValueError("Invalid Cloud Build PR-head event")
        build_id, project, region = _build_link(payload.get("runUrl"))
        if project.isdigit():
            resolved = self.cloud.project(project)
            if (not isinstance(resolved, dict) or str(resolved.get("projectNumber")) != project
                    or not isinstance(resolved.get("projectId"), str)
                    or not PROJECT.fullmatch(resolved["projectId"])):
                raise ValueError("Cannot resolve Cloud Build project number")
            project = resolved["projectId"]
        pr = self.github.pull(repo, number)
        head = pr.get("head", {}) if isinstance(pr, dict) else {}
        head_repo = head.get("repo") or {}
        if (pr.get("state") != "open" or head.get("sha") != sha
                or not _repository(head_repo.get("full_name"))
                or not _branch(head.get("ref"))):
            raise ValueError("PR is closed, stale or has no safe head ref")
        check = self.github.check_run(repo, check_id)
        if (not isinstance(check, dict) or check.get("id") != check_id
                or check.get("head_sha") != sha or check.get("status") != "completed"
                or check.get("conclusion") != "failure"
                or (check.get("app") or {}).get("slug") != "google-cloud-build"
                or check.get("details_url") != payload["runUrl"]):
            raise ValueError("Failed Cloud Build check does not match PR head")
        return head_repo["full_name"], head["ref"], sha, build_id, project, region

    def _other_failures(self, repo, sha, check_id):
        others = []
        found = False
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            runs = self.github.check_runs(repo, sha, page)
            if not isinstance(runs, list) or len(runs) > 100:
                raise ValueError("Incomplete check-run pagination")
            for run in runs:
                if not isinstance(run, dict) or type(run.get("id")) is not int:
                    raise ValueError("Invalid check-run record")
                if run["id"] in seen:
                    raise ValueError("Duplicate check run in pagination")
                seen.add(run["id"])
                if run["id"] == check_id:
                    if run.get("conclusion") != "failure":
                        raise ValueError("Target check changed during pagination")
                    found = True
                elif run.get("conclusion") in ("failure", "timed_out", "action_required",
                                               "cancelled", "startup_failure"):
                    others.append(re.sub(r"[\x00-\x1f\x7f]", "", str(run.get("name") or run["id"]))[:200])
                elif run.get("status") == "completed" and not run.get("conclusion"):
                    raise ValueError("Unknown completed check conclusion")
            if not runs:
                if not found:
                    raise ValueError("Target check absent from PR-head check runs")
                return others
        raise ValueError("Check-run pagination limit reached")

    def inspect(self, payload):
        result = {"eligible": False, "reason": "Incomplete evidence", "command": [],
                  "head_repo": "", "head_ref": "", "head_sha": "", "build_id": "",
                  "other_failures": [], "violatingFiles": [],
                  "violatingFilesExhaustive": False}
        try:
            head_repo, head_ref, sha, build_id, project, region = self._identity(payload)
            result.update(head_repo=head_repo, head_ref=head_ref, head_sha=sha, build_id=build_id)
            other = self._other_failures(payload["repository"], sha, payload["checkRunId"])
            result["other_failures"] = other
            if other:
                raise ValueError("Another PR-head check has failed")
            build = self.cloud.build(build_id, project, region)
            if (not isinstance(build, dict) or build.get("id") != build_id
                    or build.get("projectId") != project or build.get("status") != "FAILURE"
                    or build.get("substitutions", {}).get("COMMIT_SHA") != sha):
                raise ValueError("Cloud Build does not prove this PR-head revision failed")
            config_repo, config_sha, config_path = _config_identity(
                build, self.cloud, payload, project, region)
            steps = build.get("steps")
            if not isinstance(steps, list) or not steps or len(steps) > 100:
                raise ValueError("Missing build step statuses")
            failed = [index for index, step in enumerate(steps)
                      if isinstance(step, dict) and step.get("status") == "FAILURE"]
            if len(failed) != 1 or any(not isinstance(step, dict) or not step.get("status")
                                       for step in steps):
                raise ValueError("Build has multiple failures or incomplete step statuses")
            index = failed[0]
            config_steps = _config_steps(self.github.config_file(config_repo, config_path, config_sha))
            if len(config_steps) != len(steps):
                raise ValueError("Build step count differs from config")
            for step, configured in zip(steps, config_steps):
                if (step.get("name") != configured.get("name")
                        or (configured.get("id") is not None
                            and step.get("id") != configured["id"])):
                    raise ValueError("Build steps differ from config")
            command = _command(steps[index], config_steps[index])
            files = _logs_confirm(self.cloud.logs(build_id, project, region), index)
            result.update(eligible=True, reason="Spotless check format violations are the sole proven failure",
                          command=command, violatingFiles=files)
        except ValueError as exc:
            result["reason"] = str(exc)
        except (KeyError, TypeError, IndexError, AttributeError, binascii.Error, OSError,
                RuntimeError, subprocess.SubprocessError):
            result["reason"] = "Incomplete or inaccessible CI evidence"
        return result

    def recheck(self, payload, evidence=None):
        """Fresh publication snapshot anchored to persisted, previously verified evidence.

        Supply the inspect result from durable graph state after a restart. Never
        accept claimed branch/repository identity from the incoming event payload.
        """
        incomplete = {"complete": False, "otherFailures": []}
        try:
            if not isinstance(evidence, dict) or evidence.get("eligible") is not True:
                return incomplete
            head_repo, head_ref, sha, build_id, _, _ = self._identity(payload)
            if (evidence.get("head_repo") != head_repo or evidence.get("head_ref") != head_ref
                    or evidence.get("head_sha") != sha or evidence.get("build_id") != build_id
                    or evidence.get("other_failures") != []
                    or not isinstance(evidence.get("violatingFiles"), list)
                    or not evidence["violatingFiles"]):
                return incomplete
            others = self._other_failures(payload["repository"], sha, payload["checkRunId"])
            return {"headSha": sha, "headRef": head_ref, "complete": not others,
                    "otherFailures": others}
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, binascii.Error,
                OSError, RuntimeError, subprocess.SubprocessError):
            return incomplete
