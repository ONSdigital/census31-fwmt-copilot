# Workspace setup audit

## 2026-10-14 - FWMT repository inventory

1. Reviewed the workspace definition and selected folders whose names start with `census31-fwmt` and whose directories are Git repository roots.
2. Populated `workspace.json` using the example schema with 14 repositories, their GitHub origin URLs, and the `main` ref.
3. Excluded `census31-fwmt-workspace` and `census31-fwmt-ai` because they are not Git repositories; excluded other workspace folders because they do not have the `census31-fwmt` prefix.

## 2026-10-14 - Documentation configuration

1. Moved `census31-fwmt-docs` from `repos` to the `documents` section, keeping its GitHub URL and `main` ref; 13 repositories remain in `repos`.

## 2026-10-14 - Workspace bootstrap script

1. Added `scripts/create_workspace.py` to take `--workspace` and `--config`, create a new workspace, and copy its manifest to `workspace.json`.
2. Configured it to check out the documents repository and each named repository at the requested branch or commit ref, with preflight name validation and no overwrite of an existing workspace.
3. Added local Git tests for successful checkouts, commit refs, invalid names, existing destinations, duplicate names, and clone failures; live GitHub authentication remains an external runtime concern.

## 2026-10-14 - Minimal container image

1. Added a Dockerfile based on Red Hat UBI 10 minimal with Python 3, Git, and CA certificates for HTTPS checkouts.
2. Copied only the workspace bootstrap script and manifest into the image; no entrypoint, compilation tools, or additional runtime executables were added.

## 2026-10-14 - Runtime Git authentication

1. Added Git availability and remote-access checks before workspace creation to fail early without a partial checkout.
2. Added a GitHub-scoped askpass helper that reads a runtime token file for HTTPS clones and later pushes; configured the container to use it without interactive prompts or image-baked credentials.
3. Documented local and container setup, token mounts, commit identity, and push permissions in `README.md`; validated the Git preflight and helper with local tests.

## 2026-10-14 - Development workspace layout

1. Moved `scripts`, `templates`, `Dockerfile`, and `README.md` into `dev-workspace`; kept the manifest and audit at the project root.
2. Updated the Docker build context paths and README commands to run from the project root; the relocated local Git tests still pass.

## 2026-10-14 - Copilot workspace image

1. Added a Copilot image extending `dev-workspace:base`, with Copilot CLI v1.0.97 pinned and verified against the release SHA-256 checksum for Linux amd64 and arm64.
2. Documented a mounted multi-repository session with the docs repository explicitly loaded via `--add-dir`, plus separate runtime Git and Copilot authentication options.
3. Kept `agent-artifacts` automation out of this stage; the Copilot workspace template remains an example and the image uses explicit directory arguments.

## 2026-10-14 - Runtime mise tools

1. Added the pinned mise binary to the Copilot image with persistent `/mise` data/cache paths and shims on `PATH`.
2. Documented a separate read-only `mise.toml` mount and `mise install` at container startup, followed by `mise exec` for the Copilot session; tool installations persist only when `/mise` is mounted to writable storage.

## 2026-10-14 - SQLite task queue

1. Added a Python standard-library SQLite CLI under `github-copilot-workspace/task-queue` for persistent string prompts, FIFO claiming, completion, failure, explicit retry, and interrupted-task recovery.
2. Kept one running task at a time and left interrupted tasks failed for review rather than automatically replaying possible Git/Copilot side effects.
3. Copied only the queue CLI into the Copilot image and documented a database path on the host-mounted `/work` directory; local database and CLI tests pass. Task execution by Copilot is a later stage.

## 2026-10-14 - Authenticated agent request API

1. Added `POST /agent-requests` in FastAPI to validate a prompt, enqueue it through the SQLite queue, and return a `messageId` with HTTP 201.
2. Required a separate read-only API bearer token file at startup, checked with constant-time comparison; the API fails closed without token or queue access and rejects missing/incorrect credentials.
3. Added FastAPI/Uvicorn dependencies to the Copilot image and documented a dedicated API container sharing `/work`, with its port published only on the host loopback interface. Local API and queue tests pass; no automatic worker has been added.

## 2026-10-14 - CLI-neutral agent image

1. Moved the SQLite queue and authenticated FastAPI API, including tests and requirements, into `agent-workspace` without changing their behavior; removed Copilot-specific module descriptions.
2. Added `agent-workspace:base` on top of `dev-workspace:base` to own mise, the API dependencies, and the queue. The Copilot image now inherits that shared layer and installs only Copilot CLI.
3. Moved shared API/queue usage to the agent workspace README, changed its API example to use the credential-isolated agent image, and updated the Copilot build order and README links. The API and queue remain separate from any task executor.

## 2026-10-14 - Sequential agent worker

1. Added a CLI-neutral single-worker loop in `agent-workspace/worker` that polls SQLite, sends one queued prompt at a time to an executable via stdin, and records success or failure without automatically retrying failed work.
2. Added a Copilot-only runner in the child image that forwards piped prompts to Copilot CLI with explicitly configured trusted directories and tool permissions; the parent agent image remains CLI-neutral.
3. Documented separate API and worker containers, their shared database mount, runtime Git/Copilot credentials, bounded permissions, and manual recovery for interrupted tasks.

## 2026-10-14 - Idempotent agent requests

1. Added optional `idempotencyKey` support to the shared API and SQLite queue for webhook retries, with atomic keyed inserts and an in-place migration for existing databases.
2. Replays of the same prompt/key reuse the message ID with HTTP 200 and current status; conflicting prompt reuse returns HTTP 409. Unkeyed requests retain their create-new behavior.
3. Documented stable namespaced keys for provider events and covered replay, conflict, concurrent submissions, CLI use, and legacy database migration in local tests.
