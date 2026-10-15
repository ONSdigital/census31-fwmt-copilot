FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    GITHUB_TOKEN_FILE=/run/secrets/github-token

WORKDIR /app
COPY workspace.json /app/workspace.json
COPY lib/common/workspace_manifest.py /app/lib/common/workspace_manifest.py
COPY fwmt-agent-integration/poll_failures.py /app/poll_failures.py

USER 65532:65532
ENTRYPOINT ["python", "/app/poll_failures.py", "--workspace-config", "/app/workspace.json", "--state-db", "/state/seen.sqlite3"]