"""Parse workspace manifests for local checkout and GitHub polling."""

import json
import re
import urllib.parse
from pathlib import Path


def repository_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("invalid repository directory name: {!r}".format(name))
    if name == "workspace.json":
        raise ValueError("repository directory name conflicts with workspace.json")
    return name


def repository_entry(entry, name):
    if not isinstance(entry, dict):
        raise ValueError("repository entry must be an object")
    url = entry.get("url")
    ref = entry.get("ref")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("{} requires a nonempty url".format(name))
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
        raise ValueError("{} requires a nonempty ref".format(name))
    return name, url, ref


def load_workspace_manifest(config):
    with Path(config).open(encoding="utf-8") as source:
        manifest = json.load(source)
    if not isinstance(manifest, dict):
        raise ValueError("workspace manifest must be an object")
    documents = manifest.get("documents", {})
    repos = manifest.get("repos", [])
    if not isinstance(documents, dict) or not isinstance(repos, list):
        raise ValueError("documents must be an object and repos must be a list")

    document = None
    if documents:
        url = documents.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("documents requires a nonempty url")
        filename = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        name = filename[:-4] if filename.endswith(".git") else filename
        document = repository_entry(documents, repository_name(name))
    repositories = []
    for repo in repos:
        if not isinstance(repo, dict):
            raise ValueError("repository entry must be an object")
        repositories.append(repository_entry(repo, repository_name(repo.get("name"))))

    names = [name for name, _, _ in ([document] if document else []) + repositories]
    if len(names) != len(set(names)):
        raise ValueError("repository directory names must be unique")
    return document, repositories


def github_repository(url):
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.removesuffix(".git").split("/")
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.params or parsed.query or parsed.fragment or len(parts) != 3
            or parts[0] or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
                                  for part in parts[1:])):
        raise ValueError("workspace contains a non-GitHub repository URL")
    return "/".join(parts[1:])