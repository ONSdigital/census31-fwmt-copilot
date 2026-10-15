# GitHub Copilot workspace image

This image extends the [agent workspace image](../agent-workspace/README.md) with GitHub Copilot CLI. It does not clone repositories: create the workspace first using the development image. The Copilot CLI release is pinned in the Dockerfile and verified against its published SHA-256 checksum during the build. An active Copilot subscription and permission to use Copilot CLI are required.

## Build

Run all three commands from the project root (the directory containing `workspace.json`):

```sh
docker build -f dev-workspace/Dockerfile -t dev-workspace:base .
docker build -f agent-workspace/Dockerfile -t agent-workspace:base .
docker build -f github-copilot-workspace/Dockerfile -t dev-workspace:copilot .
```

The Copilot image builds for Linux amd64 or arm64. Mise is inherited from the agent image; no tools from `mise.toml` are installed at build time. No Git or Copilot token is needed at build time.

## Run

Replace `/path/to/writable-parent` with the host directory mounted as `/work` when creating the development workspace. This example assumes that the resulting checkout is `/work/fwmt` and that its docs repository contains the `.github/skills` or `.github/agents` you want Copilot to load. Supply a separate local `mise.toml` and a writable directory for cached mise installations; these host paths must exist:

```sh
docker run --rm -it \
  --mount type=bind,src=/path/to/writable-parent,dst=/work \
  --mount type=bind,src=/path/to/mise.toml,dst=/work/fwmt/mise.toml,readonly \
  --mount type=bind,src=/path/to/mise-data,dst=/mise \
  --mount type=bind,src=/path/to/git-token-file,dst=/run/secrets/git_token,readonly \
  -e GITHUB_TOKEN_FILE=/run/secrets/git_token \
  -e GIT_CONFIG_COUNT=2 \
  -e GIT_CONFIG_KEY_0=user.name \
  -e GIT_CONFIG_VALUE_0="Your Name" \
  -e GIT_CONFIG_KEY_1=user.email \
  -e GIT_CONFIG_VALUE_1="you@example.com" \
  dev-workspace:copilot \
  sh -c 'cd /work/fwmt && mise install && exec mise exec -- copilot -C /work/fwmt --add-dir=/work/fwmt/census31-fwmt-docs'
```

`mise install` installs the tools specified in the mounted file when the container starts. For example, the file could contain `[tools]` entries for `java = "25.0.2"`, `maven = "3.9.16"`, and `terraform = "1.15.6"`. Review the file before running it: mise configurations can contain executable hooks. The `/mise` mount keeps downloaded tools and caches between runs; the container user must be able to write there. The read-only file mount overlays an existing `/work/fwmt/mise.toml` for this run.

Copilot CLI will prompt for `/login` on first use; follow its device-code flow. With `--rm`, that login is not retained between runs. For public repositories or if you will not push, omit the Git token mount and `GITHUB_TOKEN_FILE` option. For additional repositories whose `.github/skills` or `.github/agents` you trust, repeat `--add-dir=/work/fwmt/REPOSITORY` in the final command. Use `/env`, `/skills list`, and `/agent` in Copilot CLI to inspect what loaded. `/work/fwmt` itself is not a Git repository; Git commands must target an individual child repository.

## Shared task queue and API

The inherited SQLite queue, polling worker, and FastAPI ingress are documented in the [agent workspace README](../agent-workspace/README.md). Run the API using `agent-workspace:base` with only its own token mount; it shares `/work/task-queue.sqlite3` with the worker below.

## Process queued tasks

Start this container separately from the API, using the same writable `/work` mount. The CLI-specific `/usr/local/bin/copilot-runner` is configured as `AGENT_RUNNER`; the shared worker passes each prompt to it on stdin. This example uses a separate runtime Copilot token so the unattended worker will not pause for `/login`:

```sh
docker run --rm -it \
  --mount type=bind,src=/path/to/writable-parent,dst=/work \
  --mount type=bind,src=/path/to/mise.toml,dst=/work/fwmt/mise.toml,readonly \
  --mount type=bind,src=/path/to/mise-data,dst=/mise \
  --mount type=bind,src=/path/to/git-token-file,dst=/run/secrets/git_token,readonly \
  --mount type=bind,src=/path/to/copilot-token-file,dst=/run/secrets/copilot_token,readonly \
  -e GITHUB_TOKEN_FILE=/run/secrets/git_token \
  -e GIT_CONFIG_COUNT=2 \
  -e GIT_CONFIG_KEY_0=user.name \
  -e GIT_CONFIG_VALUE_0="Your Name" \
  -e GIT_CONFIG_KEY_1=user.email \
  -e GIT_CONFIG_VALUE_1="you@example.com" \
  -e AGENT_ARTIFACTS_DIR=/work/fwmt/census31-fwmt-docs \
  dev-workspace:copilot \
  sh -c 'cd /work/fwmt && mise install && COPILOT_GITHUB_TOKEN="$(cat /run/secrets/copilot_token)" exec mise exec -- python3 /app/worker/worker.py --queue-db /work/task-queue.sqlite3 --workspace /work/fwmt'
```

No Copilot tools are approved automatically by default; noninteractive tasks needing approval will fail. Only after reviewing the prompts and trusted agent directories, add a scoped setting such as `-e COPILOT_ALLOWED_TOOLS='write,shell(git:*)'` to the `docker run` options. This permits writes and Git commands, including push, throughout the trusted workspace. Avoid broad allow-all permissions. The worker marks failed tasks for inspection and continues with the next pending task; it never retries them automatically. If a task remains `running` after an interrupted container, confirm it is stopped before using the shared queue's `recover` and `retry` commands.

For structured `triage-pr-build-failure` events, start an **event** worker rather than the legacy prompt worker above. Add `--events --checkpoints /work/workflow-checkpoints.sqlite3 --worktree-root /work/triage-worktrees` to the worker command. Create the worktree root in the writable `/work` mount before startup. The checkpoint database and task queue must persist across restarts. The worker needs read access to PR check runs and Cloud Build logs as well as Git credentials allowed to push to the PR head; provide those through runtime credentials and confirm the required CI tools are available. The workflow will not wait for empty check suites, repair other failure types or push on incomplete evidence. Inspect results with `python3 /app/task-queue/task_queue.py --queue-db /work/task-queue.sqlite3 list-events`. For a stopped worker with an interrupted event, run `recover-events` and review the remote and worktree before `retry-event ID`; the workflow must reconcile external side effects before resuming. The existing legacy prompt worker mode is not used by this event path.

## Copilot token alternative

Instead of interactive login, [create a fine-grained personal access token](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli#authenticating-with-a-personal-access-token) owned by your **personal account** with the **Copilot Requests** account permission. This is separate from the Git token, which needs repository Contents permission for pushes. Mount the Copilot token file read-only and replace the final command in the example above with:

```sh
  --mount type=bind,src=/path/to/copilot-token-file,dst=/run/secrets/copilot_token,readonly \
  dev-workspace:copilot \
  sh -c 'cd /work/fwmt && mise install && COPILOT_GITHUB_TOKEN="$(cat /run/secrets/copilot_token)" exec mise exec -- copilot -C /work/fwmt --add-dir=/work/fwmt/census31-fwmt-docs --secret-env-vars=COPILOT_GITHUB_TOKEN'
```

Place the extra `--mount` among the other `docker run` options, before the image name. Keep token files outside the build context and workspace, restrict their host permissions, and never put token values in the Dockerfile, image, remote URL, or `docker run -e` arguments. The token file remains readable to processes in the container while mounted; use only trusted code and skills in this session.

The [Copilot workspace example](templates/copilot-workspace.example.json) illustrates an `agent-artifacts` list, but the image does not read that file yet. The explicit `--add-dir` arguments above are the source of truth for this first version.