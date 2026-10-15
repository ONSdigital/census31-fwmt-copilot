#!/usr/bin/env python3
"""Log failed GitHub Actions runs and Google Cloud Build PR checks by PR author."""

import argparse
import json
import os
import signal
import sqlite3
import sys
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from lib.common.workspace_manifest import github_repository, load_workspace_manifest


def load_repositories(manifest):
    _, entries = load_workspace_manifest(manifest)
    if not entries:
        raise ValueError("workspace config must list repositories")
    return list(dict.fromkeys(github_repository(url) for _, url, _ in entries))


class GitHubClient:
    def __init__(self, token_file):
        self.token_file = Path(token_file)
        if not self._token():
            raise ValueError("GitHub token file must contain a token")

    def _token(self):
        return self.token_file.read_text(encoding="utf-8").strip()

    def _get(self, path, parameters=None):
        url = "https://api.github.com" + path
        if parameters:
            url += "?" + urllib.parse.urlencode(parameters)
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self._token(),
            "User-Agent": "fwmt-agent-integration",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def list_failed_runs(self, repository, page):
        result = self._get("/repos/{}/actions/runs".format(repository), {
            "event": "pull_request", "status": "failure", "per_page": 100, "page": page,
        })
        return result["workflow_runs"]

    def get_pull_request(self, repository, number):
        return self._get("/repos/{}/pulls/{}".format(repository, number))

    def list_open_pulls(self, repository, page):
        return self._get("/repos/{}/pulls".format(repository), {
            "state": "open", "per_page": 100, "page": page,
        })

    def list_check_runs(self, repository, sha, page):
        result = self._get("/repos/{}/commits/{}/check-runs".format(repository, sha), {
            "per_page": 100, "page": page,
        })
        return result["check_runs"]


class AgentRequestClient:
    def __init__(self, url, token_file):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" and not (parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "localhost"))):
            raise ValueError("Agent API URL must use HTTPS or local HTTP")
        if not parsed.netloc or parsed.path != "/agent-requests" or parsed.query or parsed.fragment:
            raise ValueError("Agent API URL must end in /agent-requests")
        self.url = url
        self.token_file = Path(token_file)
        if not self.token_file.read_text(encoding="utf-8").strip():
            raise ValueError("Agent API token file must contain a token")

    def send(self, record):
        if record.get("source") != "google-cloud-build":
            print(json.dumps(record), flush=True)
            return
        key = "github:{}:pr:{}:check:{}".format(
            record["repository"], record["prNumber"], record["checkRunId"])
        body = json.dumps({
            "command": "triage-pr-build-failure", "type": "github",
            "payload": record, "idempotencyKey": key,
        }).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.token_file.read_text(encoding="utf-8").strip(),
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in (200, 201):
                raise ValueError("Agent API rejected request with status {}".format(response.status))


class FailurePoller:
    def __init__(self, github, state_db, repositories, authors, emit, lookback_hours=48, max_pages=10, now=None):
        if not repositories or not authors or lookback_hours <= 0 or max_pages < 1:
            raise ValueError("repositories, PR authors, and positive polling bounds are required")
        self.github = github
        self.state_db = Path(state_db)
        self.repositories = repositories
        self.authors = {author.casefold() for author in authors}
        self.emit = emit
        self.lookback_hours = lookback_hours
        self.max_pages = max_pages
        self.now = now or (lambda: datetime.now(timezone.utc))
        with closing(sqlite3.connect(self.state_db)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS seen_failures ("
                "repository TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL, "
                "pr_number INTEGER NOT NULL, PRIMARY KEY (repository, run_id, attempt, pr_number))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS seen_cloud_build_checks ("
                "repository TEXT NOT NULL, check_id INTEGER NOT NULL, pr_number INTEGER NOT NULL, "
                "PRIMARY KEY (repository, check_id, pr_number))"
            )

    def poll_once(self):
        cutoff = self.now() - timedelta(hours=self.lookback_hours)
        with closing(sqlite3.connect(self.state_db)) as connection:
            for repository in self.repositories:
                for page in range(1, self.max_pages + 1):
                    runs = self.github.list_failed_runs(repository, page)
                    for run in runs:
                        if (run.get("event") != "pull_request" or run.get("status") != "completed"
                                or run.get("conclusion") != "failure" or not run.get("updated_at")):
                            continue
                        updated_at = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                        if updated_at < cutoff:
                            continue
                        for pull in run.get("pull_requests") or []:
                            number = pull.get("number")
                            if not isinstance(number, int):
                                continue
                            identity = (repository, run["id"], run.get("run_attempt", 1), number)
                            if connection.execute(
                                "SELECT 1 FROM seen_failures WHERE repository = ? AND run_id = ? "
                                "AND attempt = ? AND pr_number = ?", identity,
                            ).fetchone():
                                continue
                            details = self.github.get_pull_request(repository, number)
                            author = (details.get("user") or {}).get("login")
                            if not isinstance(author, str) or author.casefold() not in self.authors:
                                continue
                            record = {
                                "repository": repository, "prNumber": number, "prAuthor": author,
                                "prUrl": details["html_url"], "runId": run["id"],
                                "attempt": identity[2], "runUrl": run["html_url"],
                                "workflow": run.get("name"), "conclusion": "failure",
                            }
                            self.emit(record)
                            with connection:
                                connection.execute(
                                    "INSERT OR IGNORE INTO seen_failures "
                                    "(repository, run_id, attempt, pr_number) VALUES (?, ?, ?, ?)", identity,
                                )
                    if len(runs) < 100:
                        break
                self._poll_cloud_build_checks(connection, repository, cutoff)

    def _poll_cloud_build_checks(self, connection, repository, cutoff):
        for page in range(1, self.max_pages + 1):
            pulls = self.github.list_open_pulls(repository, page)
            for pull in pulls:
                author = (pull.get("user") or {}).get("login")
                if not isinstance(author, str) or author.casefold() not in self.authors:
                    continue
                for check_page in range(1, self.max_pages + 1):
                    checks = self.github.list_check_runs(repository, pull["head"]["sha"], check_page)
                    for check in checks:
                        if ((check.get("app") or {}).get("slug") != "google-cloud-build"
                                or check.get("status") != "completed" or check.get("conclusion") != "failure"
                                or not check.get("completed_at")):
                            continue
                        completed_at = datetime.fromisoformat(check["completed_at"].replace("Z", "+00:00"))
                        if completed_at < cutoff:
                            continue
                        identity = (repository, check["id"], pull["number"])
                        if connection.execute(
                            "SELECT 1 FROM seen_cloud_build_checks WHERE repository = ? AND check_id = ? "
                            "AND pr_number = ?", identity,
                        ).fetchone():
                            continue
                        record = {
                            "repository": repository, "prNumber": pull["number"], "prAuthor": author,
                            "prUrl": pull["html_url"], "headSha": pull["head"]["sha"],
                            "checkRunId": check["id"],
                            "runUrl": check["details_url"], "workflow": check["name"],
                            "source": "google-cloud-build", "conclusion": "failure",
                        }
                        self.emit(record)
                        with connection:
                            connection.execute(
                                "INSERT OR IGNORE INTO seen_cloud_build_checks "
                                "(repository, check_id, pr_number) VALUES (?, ?, ?)", identity,
                            )
                    if len(checks) < 100:
                        break
            if len(pulls) < 100:
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-config", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, default=os.environ.get("GITHUB_TOKEN_FILE"))
    parser.add_argument("--agent-api-url", default=os.environ.get("AGENT_API_URL"))
    parser.add_argument("--agent-token-file", type=Path, default=os.environ.get("AGENT_API_TOKEN_FILE"))
    parser.add_argument("--author", action="append", required=True, help="PR author login; repeat for more users")
    parser.add_argument("--interval", type=int, default=60, help="poll interval in seconds")
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--once", action="store_true", help="run one poll and exit")
    arguments = parser.parse_args()
    if arguments.interval < 1 or not arguments.token_file or not arguments.agent_api_url or not arguments.agent_token_file:
        parser.error("a positive interval, GitHub token, agent API URL, and agent API token are required")
    try:
        sender = AgentRequestClient(arguments.agent_api_url, arguments.agent_token_file)
        poller = FailurePoller(
            GitHubClient(arguments.token_file), arguments.state_db,
            load_repositories(arguments.workspace_config), arguments.author,
            sender.send,
            lookback_hours=arguments.lookback_hours, max_pages=arguments.max_pages,
        )
        stopping = Event()
        signal.signal(signal.SIGINT, lambda number, frame: stopping.set())
        signal.signal(signal.SIGTERM, lambda number, frame: stopping.set())
        while not stopping.is_set():
            try:
                poller.poll_once()
            except (OSError, ValueError, KeyError, sqlite3.Error) as error:
                print("Polling failed: {}".format(error), file=sys.stderr)
                if arguments.once:
                    return 1
            if arguments.once:
                break
            stopping.wait(arguments.interval)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print("Polling failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())