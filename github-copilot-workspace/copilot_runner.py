#!/usr/bin/env python3
"""Run one queued prompt through Copilot CLI without putting it in process arguments."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    workspace = Path.cwd().resolve()
    command = ["copilot", "-C", str(workspace), "--no-ask-user"]
    artifacts = os.environ.get("AGENT_ARTIFACTS_DIR")
    if artifacts:
        if not Path(artifacts).is_dir():
            print("Copilot agent artifacts directory does not exist", file=sys.stderr)
            return 1
        command.append("--add-dir={}".format(artifacts))
    allowed_tools = os.environ.get("COPILOT_ALLOWED_TOOLS")
    if allowed_tools:
        command.append("--allow-tool={}".format(allowed_tools))
    if os.environ.get("COPILOT_GITHUB_TOKEN"):
        command.append("--secret-env-vars=COPILOT_GITHUB_TOKEN")
    try:
        return subprocess.run(command, input=sys.stdin.read(), text=True, check=False).returncode
    except OSError as error:
        print("Could not start Copilot CLI: {}".format(type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())