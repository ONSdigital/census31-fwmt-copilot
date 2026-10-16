# Agent workspace base image

This image extends the [development workspace image](../dev-workspace/README.md) with mise, a SQLite event queue, an authenticated FastAPI ingress, and a CLI-neutral event worker. Structured PR build-failure requests are persisted and dispatched through a checkpointed LangGraph workflow. Build from the project root, which contains `workspace.json`:

```sh
docker build -f dev-workspace/Dockerfile -t dev-workspace:base .
docker build -f agent-workspace/Dockerfile -t agent-workspace:base .
```

## Workflow event queue

Keep the database on a persistent host mount outside cloned repositories. Python's standard library provides SQLite, so the queue has no external database dependency. The queue stores structured workflow events in `workflow_events`; it does not accept or execute free-form prompt tasks.

The same database is `/work/task-queue.sqlite3` in a container with `/path/to/writable-parent` mounted at `/work`. Use `python3 task-queue/task_queue.py --queue-db /work/task-queue.sqlite3 list-events` to inspect events and reports. `recover-events` marks interrupted running events failed for operator review, and `retry-event ID` retries a reviewed failure. An event retry resumes from its persisted LangGraph checkpoint. Reconcile worktree and remote side effects before retrying, particularly if a push might already have occurred.

The old prompt queue is no longer created or accessed. Opening an existing database does not delete its `tasks` table or stored prompts; back up and review it before manually dropping that table if its data should be removed.

## Queue worker

The worker in `/app/worker/worker.py` polls for pending workflow events and processes one at a time. It inspects PR and Cloud Build evidence, stops and reports unless Spotless is the sole failure in the build and there are no other failed PR-head check runs, and only then attempts an isolated repair and guarded push. A `pushed-awaiting-ci` result records a pushed commit, not a passing PR. The worker requires GitHub/Cloud Build read credentials and push permission on the PR head branch; missing access fails closed.

Start only one worker per SQLite queue. SIGTERM or SIGINT stops the worker after its current event finishes; a force-killed worker leaves an event `running`. Confirm the old worker has stopped before using `recover-events`, review the report and remote state, and retry only when replay is safe.

Provision authenticated `gh` and `gcloud`, plus the project's compatible JDK and Maven, in the event-worker runtime. The base `agent-workspace` and `github-copilot-workspace` images do not install these CI tools; starting an event worker in those images alone cannot repair a build. Prefer a dedicated image or run the worker in a provisioned host environment. Use a persistent checkout produced by `create_workspace.py`, with `workspace.json`, as `--workspace`; keep Git credentials and Cloud Build read access in runtime mounts or managed identities, not the image. An unsupported build/configuration or unavailable tool is a reported failure, never a license to push.

This first Spotless-only path does not send repository code to Copilot: once the format-only failure is independently proven, it runs the project's own `spotless:apply` in a worktree and verifies the exact Maven goal and tests. Neither a changed PR head nor an interrupted push is retried automatically.

For a provisioned host, from this repository's root:

```sh
PYTHONPATH=agent-workspace:agent-workspace/task-queue \
  python3 agent-workspace/worker/worker.py \
    --queue-db /path/to/state/task-queue.sqlite3 \
    --checkpoints /path/to/state/workflow-checkpoints.sqlite3 \
    --workspace /path/to/created-fwmt-workspace \
    --worktree-root /path/to/state/triage-worktrees
```

Keep the queue, checkpoint file and worktree directory on persistent storage. Supply credentials and the required CI tools before starting. Stop the worker before using `recover-events`, inspect the event and remote branch, and use `retry-event ID` only after resolving any uncertain push marker.

## Agent request API

Run the API separately from an agent CLI session, sharing `/work/task-queue.sqlite3` with the queue consumer. Use a dedicated high-entropy token file outside the repository, not a Git or CLI token. For example, create it locally with `umask 077; openssl rand -hex 32 > /path/to/agent-api-token`.

```sh
docker run --rm \
  --mount type=bind,src=/path/to/writable-parent,dst=/work \
  --mount type=bind,src=/path/to/agent-api-token,dst=/run/secrets/agent_api_token,readonly \
  -e AGENT_API_TOKEN_FILE=/run/secrets/agent_api_token \
  -e TASK_QUEUE_DB=/work/task-queue.sqlite3 \
  -p 127.0.0.1:8000:8000 \
  agent-workspace:base \
  uvicorn app:app --app-dir /app/api --host 0.0.0.0 --port 8000
```

With the token file accessible locally, enqueue a failed Cloud Build triage task. Replace the example IDs and SHA with those from a real failed PR:

```sh
printf 'header = "Authorization: Bearer %s"\n' "$(cat /path/to/agent-api-token)" | \
  curl --fail-with-body --config - \
    -H 'Content-Type: application/json' \
    --data '{"command":"triage-pr-build-failure","type":"github","payload":{"repository":"ONSdigital/census31-fwmt-common","prNumber":14,"prUrl":"https://github.com/ONSdigital/census31-fwmt-common/pull/14","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","checkRunId":116430236774,"runUrl":"https://console.cloud.google.com/cloud-build/builds/example","workflow":"common-build","source":"google-cloud-build","conclusion":"failure"},"idempotencyKey":"github:ONSdigital/census31-fwmt-common:pr:14:check:116430236774"}' \
    http://127.0.0.1:8000/agent-requests
```

`POST /agent-requests` returns `201 Created` with `{"messageId": 1, "status": "pending"}` after persisting an event. It accepts a `command`, `type`, free-form JSON object `payload`, and required `idempotencyKey` (1–255 nonblank characters). The envelope and 64 KiB request size are enforced at ingress; Cloud Build events require `headSha` and valid PR/build references. Non-Cloud-Build events with the supported command are recorded and reported `unsupported` without changes. Unknown commands and invalid Cloud Build event data return 422. Repeating a key with the same event returns `200 OK` with the original message ID and current status; using it for a different event returns `409 Conflict`. Use a stable, namespaced identifier per check, including its run ID. The previous raw `prompt` request format is no longer accepted. The API refuses to start without a readable token or accessible queue. Keep it on localhost or add TLS and access controls before allowing remote callers. Event data may be sensitive; protect the database and avoid logging request bodies. The endpoint only enqueues events.

For local testing from the project root, install `agent-workspace/api/requirements-test.txt`, set `AGENT_API_TOKEN_FILE` and `TASK_QUEUE_DB`, and run `uvicorn app:app --app-dir agent-workspace/api --host 127.0.0.1 --port 8000` with `PYTHONPATH=agent-workspace/task-queue`.

## Mise

The image contains mise but does not install project tools at build time. A CLI-specific image can mount a reviewed `mise.toml` read-only at `/work/fwmt/mise.toml` and writable mise storage at `/mise`, then run `cd /work/fwmt && mise install` before starting its CLI with `mise exec`. The `/mise` mount preserves downloaded tools and caches across containers; the container user must have write access. Mise configurations can contain executable hooks, so review them before using them.