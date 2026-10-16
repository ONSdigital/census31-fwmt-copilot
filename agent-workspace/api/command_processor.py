"""Validate supported event fields before durable dispatch."""

import re
from urllib.parse import urlsplit


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")


def triage_pr_build_failure(payload):
    required = ("repository", "prNumber", "prUrl", "headSha", "checkRunId",
                "runUrl", "workflow", "source", "conclusion")
    if any(field not in payload for field in required):
        raise ValueError("Cloud Build failure payload is missing required fields")
    repository, number, check_id = payload["repository"], payload["prNumber"], payload["checkRunId"]
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or type(number) is not int or number < 1 or type(check_id) is not int or check_id < 1):
        raise ValueError("Invalid repository, PR number, or check run ID")
    if not isinstance(payload["headSha"], str) or not SHA.fullmatch(payload["headSha"]):
        raise ValueError("Invalid PR head SHA")
    pr_url = urlsplit(payload["prUrl"]) if isinstance(payload["prUrl"], str) else None
    build_url = urlsplit(payload["runUrl"]) if isinstance(payload["runUrl"], str) else None
    if (pr_url is None or pr_url.scheme != "https" or pr_url.netloc != "github.com"
            or pr_url.path != "/{}/pull/{}".format(repository, number)
            or pr_url.query or pr_url.fragment
            or build_url is None or build_url.scheme != "https"
            or build_url.netloc != "console.cloud.google.com" or not build_url.path):
        raise ValueError("Invalid PR or Cloud Build URL")
    if (payload["source"] != "google-cloud-build" or payload["conclusion"] != "failure"
            or not isinstance(payload["workflow"], str) or not payload["workflow"].strip()
            or len(payload["workflow"]) > 200):
        raise ValueError("Invalid Cloud Build check")
    return None


def validate_command(command, event_type, payload):
    if command != "triage-pr-build-failure" or event_type != "github":
        raise ValueError("Unsupported command or event type")
    if payload.get("source") == "google-cloud-build":
        triage_pr_build_failure(payload)
