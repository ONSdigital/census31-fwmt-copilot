import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from lib.common.workspace_manifest import github_repository, load_workspace_manifest
from poll_failures import AgentRequestClient, GitHubClient, FailurePoller, load_repositories, main


class FakeGitHub:
    def __init__(self, pages, authors, pull_pages=None, checks=None):
        self.pages = pages
        self.authors = authors
        self.pull_pages = pull_pages or {}
        self.checks = checks or {}
        self.requests = []

    def list_failed_runs(self, repository, page):
        self.requests.append((repository, page))
        return self.pages.get((repository, page), [])

    def get_pull_request(self, repository, number):
        return {"user": {"login": self.authors[(repository, number)]},
                "html_url": "https://github.com/{}/pull/{}".format(repository, number)}

    def list_open_pulls(self, repository, page):
        return self.pull_pages.get((repository, page), [])

    def list_check_runs(self, repository, sha, page):
        self.requests.append((repository, sha, page))
        return self.checks.get((repository, sha, page), [])


class PollFailuresTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "seen.sqlite3"
        self.repo = "ONSdigital/census31-fwmt-job-service"
        self.now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)

    def run_record(self, number=17, attempt=1, conclusion="failure", pulls=None, updated_at=None):
        return {"id": number, "run_attempt": attempt, "event": "pull_request", "status": "completed",
                "conclusion": conclusion, "name": "CI", "html_url": "https://github.com/{}/actions/runs/{}".format(self.repo, number),
                "updated_at": updated_at or self.now.isoformat(),
                "pull_requests": [{"number": 42}] if pulls is None else pulls}

    def test_filters_pr_author_not_workflow_actor_and_deduplicates_reruns(self):
        first = self.run_record()
        first["actor"] = {"login": "wrong-person"}
        other = self.run_record(number=18, pulls=[{"number": 43}])
        github = FakeGitHub({(self.repo, 1): [first, other]},
                            {(self.repo, 42): "SiMoN", (self.repo, 43): "other"})
        records = []
        poller = FailurePoller(github, self.state, [self.repo], {"simon"}, records.append,
                               lookback_hours=24, max_pages=2, now=lambda: self.now)

        poller.poll_once()
        poller.poll_once()
        github.pages[(self.repo, 1)] = [self.run_record(attempt=2), first]
        FailurePoller(github, self.state, [self.repo], {"simon"}, records.append,
                  lookback_hours=24, max_pages=2, now=lambda: self.now).poll_once()

        self.assertEqual([(record["runId"], record["attempt"], record["prAuthor"]) for record in records],
                         [(17, 1, "SiMoN"), (17, 2, "SiMoN")])
        self.assertEqual(github.requests, [(self.repo, 1)] * 3)
        with closing(sqlite3.connect(self.state)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM seen_failures").fetchone()[0], 2)

    def test_ignores_unrelated_old_and_unassociated_runs(self):
        github = FakeGitHub({(self.repo, 1): [self.run_record(number=1, conclusion="success"),
                                             self.run_record(number=2, pulls=[]),
                                             self.run_record(number=3, updated_at=(self.now - timedelta(days=3)).isoformat()),
                                             self.run_record(number=4, pulls=[{"number": 43}])]},
                            {(self.repo, 43): "other"})
        records = []
        FailurePoller(github, self.state, [self.repo], {"simon"}, records.append,
                      lookback_hours=24, now=lambda: self.now).poll_once()
        self.assertEqual(records, [])

    def test_logs_only_failed_cloud_build_checks_for_matching_open_pr_and_deduplicates(self):
        pr = {"number": 14, "html_url": "https://github.com/{}/pull/14".format(self.repo),
              "user": {"login": "SiMoN"}, "head": {"sha": "head-sha"}}
        other = {"number": 15, "user": {"login": "other"}, "head": {"sha": "other-sha"}}
        checks = [
            {"id": 101, "app": {"slug": "google-cloud-build"}, "status": "completed",
             "conclusion": "failure", "completed_at": self.now.isoformat(),
             "name": "census31-fwmtg-common-build", "details_url": "https://console.cloud.google.com/build/101"},
            {"id": 102, "app": {"slug": "google-cloud-build"}, "status": "completed",
             "conclusion": "success", "completed_at": self.now.isoformat()},
            {"id": 103, "app": {"slug": "google-cloud-build"}, "status": "in_progress",
             "conclusion": None, "completed_at": None},
            {"id": 104, "app": {"slug": "other"}, "status": "completed",
             "conclusion": "failure", "completed_at": self.now.isoformat()},
            {"id": 105, "app": {"slug": "google-cloud-build"}, "status": "completed",
             "conclusion": "failure", "completed_at": (self.now - timedelta(days=3)).isoformat()},
        ]
        github = FakeGitHub({}, {}, {(self.repo, 1): [pr, other]},
                            {(self.repo, "head-sha", 1): checks})
        records = []
        poller = FailurePoller(github, self.state, [self.repo], {"simon"}, records.append,
                               lookback_hours=24, now=lambda: self.now)
        poller.poll_once()
        poller.poll_once()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["prNumber"], 14)
        self.assertEqual(records[0]["checkRunId"], 101)
        self.assertEqual(records[0]["runUrl"], "https://console.cloud.google.com/build/101")
        self.assertEqual(records[0]["workflow"], "census31-fwmtg-common-build")
        self.assertEqual(records[0]["headSha"], "head-sha")
        self.assertEqual(github.requests.count((self.repo, "head-sha", 1)), 2)
        self.assertNotIn((self.repo, "other-sha", 1), github.requests)

    def test_paginates_cloud_build_checks_and_retries_after_emit_failure(self):
        pr = {"number": 14, "html_url": "https://github.com/{}/pull/14".format(self.repo),
              "user": {"login": "simon"}, "head": {"sha": "head-sha"}}
        success = {"id": 1, "app": {"slug": "google-cloud-build"}, "status": "completed",
                   "conclusion": "success", "completed_at": self.now.isoformat()}
        failed = {"id": 2, "app": {"slug": "google-cloud-build"}, "status": "completed",
                  "conclusion": "failure", "completed_at": self.now.isoformat(),
                  "name": "Build", "details_url": "https://example.com/build"}
        github = FakeGitHub({}, {}, {(self.repo, 1): [pr]},
                            {(self.repo, "head-sha", 1): [success] * 100,
                             (self.repo, "head-sha", 2): [failed]})
        def fail_emit(record):
            raise OSError("logging unavailable")

        with self.assertRaises(OSError):
            FailurePoller(github, self.state, [self.repo], {"simon"}, fail_emit,
                          now=lambda: self.now).poll_once()
        records = []
        FailurePoller(github, self.state, [self.repo], {"simon"}, records.append,
                      now=lambda: self.now).poll_once()
        self.assertEqual([record["checkRunId"] for record in records], [2])
        self.assertIn((self.repo, "head-sha", 2), github.requests)

    def test_agent_request_sender_delivers_structured_event_with_bearer_and_key(self):
        token = self.root / "api-token"
        token.write_text("agent-token\n")
        record = {"repository": self.repo, "prNumber": 14, "prAuthor": "simon",
                  "prUrl": "https://github.com/{}/pull/14".format(self.repo),
                  "headSha": "a" * 40, "checkRunId": 101, "runUrl": "https://console.cloud.google.com/build/101",
                  "workflow": "Build", "source": "google-cloud-build", "conclusion": "failure"}
        requests = []

        class Accepted:
            status = 201
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def accept(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 15)
            return Accepted()

        with patch("urllib.request.urlopen", side_effect=accept):
            AgentRequestClient("http://127.0.0.1:8000/agent-requests", token).send(record)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer agent-token")
        body = json.loads(requests[0].data)
        self.assertEqual(body["command"], "triage-pr-build-failure")
        self.assertEqual(body["type"], "github")
        self.assertEqual(body["payload"], record)
        self.assertEqual(body["idempotencyKey"],
                         "github:{}:pr:14:check:101".format(self.repo))

    def test_failed_delivery_can_retry_without_marking_check_seen(self):
        from urllib.error import HTTPError
        pr = {"number": 14, "html_url": "https://github.com/{}/pull/14".format(self.repo),
              "user": {"login": "simon"}, "head": {"sha": "a" * 40}}
        failed = {"id": 101, "app": {"slug": "google-cloud-build"}, "status": "completed",
                  "conclusion": "failure", "completed_at": self.now.isoformat(),
                  "name": "Build", "details_url": "https://console.cloud.google.com/build/101"}
        github = FakeGitHub({}, {}, {(self.repo, 1): [pr]}, {(self.repo, "a" * 40, 1): [failed]})
        token = self.root / "api-token"
        token.write_text("agent-token\n")
        sender = AgentRequestClient("http://127.0.0.1:8000/agent-requests", token)
        poller = FailurePoller(github, self.state, [self.repo], {"simon"}, sender.send,
                               now=lambda: self.now)

        class Accepted:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch("urllib.request.urlopen", side_effect=[
                HTTPError("http://127.0.0.1:8000/agent-requests", 503, "unavailable", {}, None),
                Accepted()]):
            with self.assertRaises(HTTPError):
                poller.poll_once()
            poller.poll_once()
            poller.poll_once()
        with closing(sqlite3.connect(self.state)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM seen_cloud_build_checks").fetchone()[0], 1)

    def test_cloud_build_client_queries_open_prs_and_head_check_runs(self):
        client = object.__new__(GitHubClient)
        with patch.object(client, "_get", side_effect=[
                [{"number": 14}], {"check_runs": [{"id": 101}]}]) as get:
            self.assertEqual(client.list_open_pulls(self.repo, 1), [{"number": 14}])
            self.assertEqual(client.list_check_runs(self.repo, "sha", 2), [{"id": 101}])
        get.assert_any_call("/repos/{}/pulls".format(self.repo),
                            {"state": "open", "per_page": 100, "page": 1})
        get.assert_any_call("/repos/{}/commits/sha/check-runs".format(self.repo),
                            {"per_page": 100, "page": 2})

    def test_lists_each_page_and_retries_after_emit_failure(self):
        github = FakeGitHub({(self.repo, 1): [self.run_record(number=1)] * 100,
                             (self.repo, 2): [self.run_record(number=2)]},
                            {(self.repo, 42): "simon"})
        records = []

        def fail_emit(record):
            raise OSError("logging unavailable")

        with self.assertRaises(OSError):
            FailurePoller(github, self.state, [self.repo], {"simon"}, fail_emit,
                          max_pages=2, now=lambda: self.now).poll_once()
        FailurePoller(github, self.state, [self.repo], {"simon"}, records.append,
                      max_pages=2, now=lambda: self.now).poll_once()
        self.assertEqual([record["runId"] for record in records], [1, 2])
        self.assertIn((self.repo, 2), github.requests)

    def test_reads_only_manifest_repositories_and_validates_urls(self):
        manifest = self.root / "workspace.json"
        manifest.write_text(json.dumps({"documents": {"url": "https://github.com/ONSdigital/docs.git", "ref": "main"},
                                        "repos": [{"name": "census31-fwmt-job-service",
                                                   "url": "https://github.com/ONSdigital/census31-fwmt-job-service.git",
                                                   "ref": "main"}]}))
        self.assertEqual(load_repositories(manifest), [self.repo])
        manifest.write_text(json.dumps({"repos": [{"name": "repo", "url": "https://example.com/other/repo.git",
                                                  "ref": "main"}]}))
        with self.assertRaises(ValueError):
            load_repositories(manifest)

    def test_shared_manifest_loader_keeps_documents_separate_and_validates_github_urls(self):
        manifest = self.root / "workspace.json"
        manifest.write_text(json.dumps({
            "documents": {"url": "https://github.com/ONSdigital/docs.git", "ref": "main"},
            "repos": [{"name": "census31-fwmt-job-service",
                       "url": "https://github.com/ONSdigital/census31-fwmt-job-service.git", "ref": "main"}],
        }))
        documents, repositories = load_workspace_manifest(manifest)
        self.assertEqual(documents, ("docs", "https://github.com/ONSdigital/docs.git", "main"))
        self.assertEqual(repositories, [("census31-fwmt-job-service",
                                         "https://github.com/ONSdigital/census31-fwmt-job-service.git", "main")])
        self.assertEqual(github_repository(repositories[0][1]), self.repo)
        for url in ("https://github.com.evil.org/ONSdigital/repo.git",
                    "https://user:secret@github.com/ONSdigital/repo.git",
                    "https://github.com/ONSdigital/repo.git?token=secret"):
            with self.assertRaises(ValueError):
                github_repository(url)

    def test_client_keeps_token_out_of_url_and_loads_mounted_file(self):
        token = self.root / "github-token"
        token.write_text("test-token\n", encoding="utf-8")
        urls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return b'{"workflow_runs": []}'

        def fake_open(request, timeout):
            urls.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(timeout, 15)
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_open):
            self.assertEqual(GitHubClient(token).list_failed_runs(self.repo, 1), [])
        self.assertEqual(len(urls), 1)
        self.assertIn("event=pull_request", urls[0])
        self.assertIn("status=failure", urls[0])
        self.assertNotIn("test-token", urls[0])

    def test_continuous_mode_retries_transient_poll_errors(self):
        manifest = self.root / "workspace.json"
        manifest.write_text(json.dumps({"repos": [{"name": "example", "url": "https://github.com/ONSdigital/example.git",
                                                  "ref": "main"}]}))
        token = self.root / "token"
        token.write_text("test-token\n")
        poller = Mock()
        poller.poll_once.side_effect = [OSError("transient API error"), None]
        stopping = Mock()
        stopping.is_set.side_effect = [False, False, True]
        output = io.StringIO()
        arguments = ["poll_failures.py", "--workspace-config", str(manifest), "--state-db",
                     str(self.state), "--token-file", str(token), "--author", "simon",
                     "--agent-api-url", "http://127.0.0.1:8000/agent-requests",
                     "--agent-token-file", str(token)]
        with patch("sys.argv", arguments), patch("poll_failures.FailurePoller", return_value=poller), \
                patch("poll_failures.Event", return_value=stopping), patch("signal.signal"), \
                patch("sys.stderr", output):
            self.assertEqual(main(), 0)
        self.assertEqual(poller.poll_once.call_count, 2)
        self.assertEqual(stopping.wait.call_count, 2)
        self.assertNotIn("test-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()