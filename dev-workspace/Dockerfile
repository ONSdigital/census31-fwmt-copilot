FROM registry.access.redhat.com/ubi10/ubi-minimal:latest

RUN microdnf install -y python3 git ca-certificates \
    && microdnf clean all

WORKDIR /app
COPY dev-workspace/scripts/create_workspace.py scripts/create_workspace.py
COPY --chmod=755 dev-workspace/scripts/git_askpass.py scripts/git_askpass.py
COPY workspace.json workspace.json

ENV GIT_ASKPASS=/app/scripts/git_askpass.py \
    GIT_TERMINAL_PROMPT=0