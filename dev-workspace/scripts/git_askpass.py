#!/usr/bin/env python3
"""Supply a runtime-mounted GitHub token for HTTPS Git operations."""

import os
import re
import sys
from pathlib import Path


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not re.search(r"https://(?:[^@/' ]+@)?github\.com(?=[:/']|$)", prompt):
        return 1
    token_file = os.environ.get("GITHUB_TOKEN_FILE")
    if not token_file:
        return 1
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return 1
    if not token:
        return 1
    if prompt.startswith("Username for "):
        print("x-access-token")
    elif prompt.startswith("Password for "):
        print(token)
    else:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())