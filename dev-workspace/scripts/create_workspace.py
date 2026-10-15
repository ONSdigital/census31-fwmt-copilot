#!/usr/bin/env python3
"""Create a workspace from a JSON manifest and check out its repositories."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from lib.common.workspace_manifest import load_workspace_manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="new workspace directory")
    parser.add_argument("--config", required=True, type=Path, help="source workspace.json")
    args = parser.parse_args()

    try:
        document, repositories = load_workspace_manifest(args.config)
        entries = ([document] if document else []) + repositories
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