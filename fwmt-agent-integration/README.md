# FWMT agent integration: failed PR checks

Polls failed `pull_request` GitHub Actions runs and failed Google Cloud Build check runs on open PR heads for the repositories in the root `workspace.json`. For each associated PR, it checks the PR author against configured `--author` logins (case-insensitive). Completed, failed Cloud Build checks are sent to the agent API as structured events; the agent workspace owns validation and prompt rendering. GitHub Actions failures continue to be logged as JSON. The poller does not wait for other checks or queued check suites and does not download build logs. A separate SQLite file records seen Actions runs and Cloud Build check IDs only after successful output or delivery, preventing repeated requests across restarts. The agent API idempotency key also protects against ambiguous HTTP retries.

## Requirements

- Python 3.10+ for local execution, or Podman/Docker for the container.
- Outbound access to `api.github.com`; no inbound port or webhook endpoint.
- A GitHub token with read access to the monitored repositories and Actions, pull request, and check-run metadata (for private repos, a fine-grained token with Actions: read, Pull requests: read, and Checks: read). Put it in a local file readable only by the process user; never put it in command arguments, the image or logs.
- A separate agent API bearer token file and an HTTPS `--agent-api-url` ending in `/agent-requests` (local HTTP on `127.0.0.1` or `localhost` is allowed). The API and poller must be able to reach each other; `localhost` inside a container refers to that container.
- A persistent writable state directory. Losing the database means recent failures may be logged again.

Run the following from the **repository root**. Create the token file yourself and restrict it with `chmod 600`; the examples assume it is at `$HOME/.config/fwmt/github-token`.

## Local

```sh
mkdir -p fwmt-agent-integration/state
PYTHONPATH="$PWD" python3 fwmt-agent-integration/poll_failures.py \
  --workspace-config workspace.json \
  --state-db fwmt-agent-integration/state/seen.sqlite3 \
  --token-file "$HOME/.config/fwmt/github-token" \
  --agent-api-url http://127.0.0.1:8000/agent-requests \
  --agent-token-file "$HOME/.config/fwmt/agent-api-token" \
  --author YOUR_GITHUB_LOGIN
```

Repeat `--author` for additional PR authors. Add `--once` to poll once and exit, useful for a smoke check. Tests require no token or network:

```sh
python3 -m unittest discover -s fwmt-agent-integration -p 'test_poll_failures.py' -v
```

On a managed Mac where system Python cannot install certificates, use a user-owned Conda environment instead. Run from the repository root; the absolute interpreter path avoids a shell or `conda run` selecting a different Python:

```sh
conda create -n fwmt-poller python=3.12 ca-certificates certifi openssl
PYTHONPATH="$PWD" "$HOME/.conda/envs/fwmt-poller/bin/python" fwmt-agent-integration/poll_failures.py \
  --workspace-config workspace.json \
  --state-db fwmt-agent-integration/state/seen.sqlite3 \
  --token-file "$HOME/.config/fwmt/github-token" \
  --author YOUR_GITHUB_LOGIN --once
```

A successful `--once` with no JSON output does not rule out a Cloud Build failure: successful Cloud Build requests are queued silently. Both the local script and container import the shared manifest loader from `lib/common`. The supplied `--workspace-config` is parsed for repository URLs; the separate `documents` URL is not polled. Bootstrap checks Git remote access, while the poller queries GitHub Actions and PRs with the mounted token. A valid Git URL does not imply that the Actions API is accessible.

## Podman or Docker

Build from the repository root (the image copies the manifest, shared loader and poller):

```sh
podman build -f fwmt-agent-integration/Dockerfile -t fwmt-agent-integration .
mkdir -p fwmt-agent-integration/state
podman run --rm --read-only --user "$(id -u):$(id -g)" \
  -v "$PWD/fwmt-agent-integration/state:/state:rw" \
  -v "$HOME/.config/fwmt/github-token:/run/secrets/github-token:ro" \
  -v "$HOME/.config/fwmt/agent-api-token:/run/secrets/agent-api-token:ro" \
  -e AGENT_API_URL=https://YOUR_AGENT_HOST/agent-requests \
  -e AGENT_API_TOKEN_FILE=/run/secrets/agent-api-token \
  fwmt-agent-integration --author YOUR_GITHUB_LOGIN
```

Replace `podman` with `docker` for Docker. The explicit user ID gives the container access to the owner-readable token and a host-owned state directory; without these mounts, it cannot read credentials or persist its cursor. Supply additional `--author` flags as needed. Stop with Ctrl-C or `podman stop`/`docker stop`.

The default interval is 60 seconds, lookback window is 48 hours (Actions `updated_at`; Cloud Build `completed_at`), and each repo is limited to 10 pages of 100 failed Actions runs, open PRs, and check runs per PR per poll. Configure `--interval`, `--lookback-hours` and `--max-pages` if needed. The first poll includes matching failures already within the window. GitHub returns only currently failed Actions runs and the latest check runs on each PR head: a failure replaced by a successful rerun before the next poll will not be observed, nor will runs beyond the lookback or page cap. Actions runs without an associated PR or a resolvable PR author are skipped; Cloud Build checks on closed PRs are not polled. API/state errors go to stderr and are retried next interval in continuous mode; `--once` exits nonzero. No historical backfill is implemented.

## Troubleshooting

- `CERTIFICATE_VERIFY_FAILED`: inspect the interpreter and its trust paths with `python -c 'import sys, ssl; print(sys.executable); print(ssl.get_default_verify_paths())'` using the same interpreter as the poller. Use a Python installation with a trusted CA bundle, such as the Conda example above. If a corporate TLS proxy is in use, obtain its CA bundle and point `SSL_CERT_FILE` to it; do not disable TLS verification.
- `HTTP Error 404`: the poller stops at the first failed repository, PR, or check-run request in `--once` mode. Check that the URL still exists and that the token's resource owner covers that repository, with Actions: read, Pull requests: read, and Checks: read. GitHub may return 404 for an inaccessible private or internal repo. If repository metadata is accessible but `/actions/runs` or `/check-runs` is not, check the corresponding permission. Do not remove repositories from the root manifest just to make polling pass; the workspace bootstrap also uses that manifest.