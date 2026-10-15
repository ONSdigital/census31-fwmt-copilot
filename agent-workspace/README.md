# Agent workspace base image

This image extends the [development workspace image](../dev-workspace/README.md) with mise, a SQLite task queue, an authenticated FastAPI ingress, and a CLI-neutral worker. It contains no agent-specific CLI or runner; a child image supplies the runner. Build from the project root, which contains `workspace.json`:

```sh
docker build -f dev-workspace/Dockerfile -t dev-workspace:base .
docker build -f agent-workspace/Dockerfile -t agent-workspace:base .
```

## Task queue

Keep the database on a persistent host mount outside cloned repositories. Python's standard library provides SQLite, so the queue has no external database dependency. From the project root:

```sh
python3 agent-workspace/task-queue/task_queue.py --queue-db /path/to/writable-parent/task-queue.sqlite3 enqueue "Review the job-service change"
python3 agent-workspace/task-queue/task_queue.py --queue-db /path/to/writable-parent/task-queue.sqlite3 list
```

The same database is `/work/task-queue.sqlite3` in a container with `/path/to/writable-parent` mounted at `/work`. Run `python3 task-queue/task_queue.py --queue-db /work/task-queue.sqlite3 COMMAND` in this image to manage it. Commands are `enqueue "PROMPT" [--idempotency-key KEY]`, `claim`, `done ID`, `fail ID "REASON"`, `list`, `recover`, and `retry ID`. `claim` returns the oldest pending task as JSON, or no output if none remains; only one task can be running. An existing SQLite database is migrated in place to add the nullable key column and unique index without removing queued tasks.

If a worker stops mid-task, confirm it has stopped before using `recover`. Recovery marks its task failed, not pending, because the external operation may already have completed. Inspect it with `list` and use `retry ID` only when replaying is safe.

## Queue worker

The worker in `/app/worker/worker.py` polls for pending tasks, claims one at a time, sends the prompt to a CLI-specific executable on stdin, and records completion or failure based on its exit status. Pass `--queue-db`, `--workspace`, and an absolute executable path using `--runner` or `AGENT_RUNNER`. The worker checks again after every task and polls every two seconds when the queue is empty; `--until-empty` processes current tasks and exits for testing. No automatic retries are performed.

The runner must read exactly one prompt from stdin and return exit status 0 only for success. Start only one worker per SQLite queue. SIGTERM or SIGINT stops the worker after its current runner exits; a force-killed worker leaves a task `running`. On restart it refuses to process new tasks until an operator confirms the old worker has stopped and explicitly recovers or retries the task. A CLI-specific child image provides the concrete runner.

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

With the token file accessible locally, enqueue a task:

```sh
printf 'header = "Authorization: Bearer %s"\n' "$(cat /path/to/agent-api-token)" | \
  curl --fail-with-body --config - \
    -H 'Content-Type: application/json' \
    --data '{"prompt":"Review the job-service change","idempotencyKey":"github:repo:run:1"}' \
    http://127.0.0.1:8000/agent-requests
```

`POST /agent-requests` returns `201 Created` with `{"messageId": 1, "status": "pending"}`. The optional `idempotencyKey` is a nonblank string of at most 255 characters. Repeating a key with the same prompt returns `200 OK` with the original message ID and current status; using it for a different prompt returns `409 Conflict`. Without a key, every request creates a new task. Use a stable, namespaced identifier per event (for example provider, repository, workflow run ID, and attempt), including the attempt if a rerun should become a separate task. Prompts must be nonblank and at most 4096 characters. The API refuses to start without a readable token or accessible queue. Keep it on localhost or add TLS and access controls before allowing remote callers. Prompt text may be sensitive; protect the database and avoid logging request bodies. The endpoint only enqueues tasks.

For local testing from the project root, install `agent-workspace/api/requirements-test.txt`, set `AGENT_API_TOKEN_FILE` and `TASK_QUEUE_DB`, and run `uvicorn app:app --app-dir agent-workspace/api --host 127.0.0.1 --port 8000` with `PYTHONPATH=agent-workspace/task-queue`.

## Mise

The image contains mise but does not install project tools at build time. A CLI-specific image can mount a reviewed `mise.toml` read-only at `/work/fwmt/mise.toml` and writable mise storage at `/mise`, then run `cd /work/fwmt && mise install` before starting its CLI with `mise exec`. The `/mise` mount preserves downloaded tools and caches across containers; the container user must have write access. Mise configurations can contain executable hooks, so review them before using them.