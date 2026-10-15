#!/usr/bin/env python3
"""Create a workspace from a JSON manifest and check out its repositories."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
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


def load_repositories(config):
    with config.open(encoding="utf-8") as source:
        manifest = json.load(source)
    if not isinstance(manifest, dict):
        raise ValueError("workspace manifest must be an object")
    documents = manifest.get("documents", {})
    repos = manifest.get("repos", [])
    if not isinstance(documents, dict) or not isinstance(repos, list):
        raise ValueError("documents must be an object and repos must be a list")

    entries = []
    if documents:
        url = documents.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("documents requires a nonempty url")
        filename = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        name = filename[:-4] if filename.endswith(".git") else filename
        entries.append(repository_entry(documents, repository_name(name)))
    for repo in repos:
        if not isinstance(repo, dict):
            raise ValueError("repository entry must be an object")
        entries.append(repository_entry(repo, repository_name(repo.get("name"))))

    names = [name for name, _, _ in entries]
    if len(names) != len(set(names)):
        raise ValueError("repository directory names must be unique")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="new workspace directory")
    parser.add_argument("--config", required=True, type=Path, help="source workspace.json")
    args = parser.parse_args()

    try:
        entries = load_repositories(args.config)
        if args.workspace.exists():
            raise ValueError("workspace already exists: {}".format(args.workspace))
        if shutil.which("git") is None:
            raise ValueError("Git is required; install it and ensure it is on PATH")
        token_file = os.environ.get("GITHUB_TOKEN_FILE")
        if token_file and (not Path(token_file).is_file() or not os.access(token_file, os.R_OK)):
            raise ValueError("GITHUB_TOKEN_FILE must point to a readable file")
        for name, url, _ in entries:
            print("Checking access to {}".format(name), flush=True)
            subprocess.run(["git", "ls-remote", "--", url], check=True, stdout=subprocess.DEVNULL)
        args.workspace.mkdir(parents=True)
        shutil.copyfile(args.config, args.workspace / "workspace.json")
        for name, url, ref in entries:
            print("Checking out {} at {}".format(name, ref), flush=True)
            destination = args.workspace / name
            subprocess.run(
                ["git", "clone", "--", url, str(destination)],
                check=True,
            )
            subprocess.run(["git", "-C", str(destination), "checkout", ref], check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Workspace creation failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())