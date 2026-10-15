---
name: gcloud-build-triggers
description: Use when the user asks to list, inspect, run, or troubleshoot Google Cloud Build triggers or build history with the gcloud CLI. Covers regional trigger scoping, trigger inspection, build history, log access, and common console-versus-CLI mismatches.
compatibility: gcloud CLI, Google Cloud auth, network access to Google Cloud APIs
metadata:
  references:
    - https://docs.cloud.google.com/sdk/gcloud/reference
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds/triggers/list
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds/triggers/describe
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds/log
  author: dev-team
---

# gcloud Build Triggers

Use this skill when working with Google Cloud Build via the `gcloud` CLI, especially for trigger discovery, build history inspection, and log troubleshooting.

## When to use

- User asks to list Cloud Build triggers in a project
- Console shows triggers, but `gcloud` returns `Listed 0 items.`
- User needs to inspect one trigger or run it manually
- User needs to know whether a trigger has ever run
- User needs the logs or failure reason for a Cloud Build run

## Core rules

- The product is Cloud Build, but the CLI group is `gcloud builds`, not `gcloud cloudbuild`.
- Always anchor commands with `--project` when the target project matters.
- If a Console URL supplies a numeric project number, resolve it to a project ID before using `gcloud builds log`; that command rejects project numbers.
- Triggers are often regional. If the console shows triggers but CLI shows none, check `--region` first.
- Use `gcloud builds triggers ...` for trigger objects.
- Use `gcloud builds ...` for build executions, history, and logs.

## Common commands

### List triggers

```bash
gcloud builds triggers list --project=PROJECT_ID
```

Regional trigger listing:

```bash
gcloud builds triggers list \
  --project=PROJECT_ID \
  --region=REGION
```

Useful table format:

```bash
gcloud builds triggers list \
  --project=PROJECT_ID \
  --region=REGION \
  --format="table(name,id,disabled,createTime)"
```

### Describe one trigger

```bash
gcloud builds triggers describe TRIGGER_ID \
  --project=PROJECT_ID \
  --region=REGION
```

### Run a trigger manually

```bash
gcloud builds triggers run TRIGGER_ID \
  --project=PROJECT_ID \
  --region=REGION \
  --branch=BRANCH_NAME
```

### Check whether a trigger has run

```bash
gcloud builds list \
  --project=PROJECT_ID \
  --region=REGION \
  --filter="buildTriggerId=TRIGGER_ID" \
  --format="table(id,status,createTime,finishTime)"
```

Multiple triggers:

```bash
gcloud builds list \
  --project=PROJECT_ID \
  --region=REGION \
  --filter="buildTriggerId=(TRIGGER_ID_1 OR TRIGGER_ID_2 OR TRIGGER_ID_3)" \
  --format="table(id,buildTriggerId,status,createTime,finishTime)"
```

### Inspect a build and fetch logs

```bash
gcloud builds describe BUILD_ID \
  --project=PROJECT_ID \
  --region=REGION
```

```bash
gcloud builds log BUILD_ID \
  --project=PROJECT_ID \
  --region=REGION
```

If the Console URL's `project` value is a number (or `gcloud builds log` says `--project` was set to a project number), resolve it first:

```bash
gcloud projects list \
  --filter="projectNumber=PROJECT_NUMBER" \
  --format="value(projectId)"
```

Use the returned project ID with the build commands above. If no project ID is returned, check the active account and project access rather than guessing a project.

## Fast troubleshooting flow

### 1. Console shows triggers, CLI shows zero

Check in this order:

1. Same project:

```bash
gcloud config get-value project
```

2. Same account:

```bash
gcloud auth list
```

3. Same trigger region as the console:

```bash
gcloud builds triggers list --project=PROJECT_ID --region=REGION
```

Common root cause: the trigger exists in a regional scope such as `europe-west2`, while the first command queried the default scope.

### 2. Trigger exists, but no build history appears

- Confirm the trigger ID is correct with `gcloud builds triggers describe`.
- Query build history with `buildTriggerId=TRIGGER_ID`.
- If the result is empty, the trigger may simply have never run.

### 3. Build history exists, but the build failed

- Use `gcloud builds describe BUILD_ID` to get metadata and the console log URL.
- Use `gcloud builds log BUILD_ID` to inspect the failing step.
- Focus on the first concrete application error, not only the terminal stack trace tail.

## Common failure patterns

### 1. Regional trigger mismatch

Symptoms:

- Console shows triggers
- `gcloud builds triggers list --project=PROJECT_ID` returns `Listed 0 items.`

Likely cause:

- The trigger exists in a regional scope and the CLI query omitted `--region`.

Fix:

```bash
gcloud builds triggers list \
  --project=PROJECT_ID \
  --region=REGION
```

### 2. Trigger exists but has never run

Symptoms:

- Trigger appears in `gcloud builds triggers list`
- Build history query returns no rows

Check:

```bash
gcloud builds list \
  --project=PROJECT_ID \
  --region=REGION \
  --filter="buildTriggerId=TRIGGER_ID"
```

If the result is empty, treat that as a valid operational state, not a CLI failure.

### 3. Maven snapshot publish failure to Artifact Registry

Symptoms in logs:

- Build fails in a publish or deploy step
- Errors mention `maven-deploy-plugin`, `Artifact Registry`, `Could not find artifact`, or repository URLs such as `*.pkg.dev`

Typical investigation steps:

1. Read the failing step from the build log:

```bash
gcloud builds log BUILD_ID \
  --project=PROJECT_ID \
  --region=REGION
```

2. Confirm the target repository exists:

```bash
gcloud artifacts repositories describe REPOSITORY \
  --project=PROJECT_ID \
  --location=LOCATION
```

3. Inspect package and version visibility when relevant:

```bash
gcloud artifacts packages list \
  --project=PROJECT_ID \
  --location=LOCATION \
  --repository=REPOSITORY
```

```bash
gcloud artifacts versions list \
  --project=PROJECT_ID \
  --location=LOCATION \
  --repository=REPOSITORY \
  --package=PACKAGE_NAME
```

4. Check whether the problem looks like scope, repository name, package name, or publish credentials rather than a generic build failure.

Practical note:

- In Cloud Build logs, the real cause is often one or two lines above the Java stack trace tail. Prioritize the first `Failed to ...`, `Could not ...`, or repository-specific error line.

## Recommended response patterns

- If the user asks for the right command, give the exact command first.
- If the first attempt returns zero items, propose the regional variant next.
- If the user provides trigger YAML output, extract `id`, `name`, `resourceName`, and `createTime`.
- If the user asks whether a trigger has run, pivot from `builds triggers` to `builds list` filtered by `buildTriggerId`.
- If the user asks about logs, pivot from `builds list` to `builds describe` and `builds log` using a concrete `BUILD_ID`.
- If the log mentions `pkg.dev`, `maven-deploy-plugin`, or repository publish failures, pivot to the Artifact Registry command group for repository and package inspection.

## Output tips

- Prefer `--format="table(...)"` for quick operator answers.
- Prefer `--format=json` when the user needs exact machine-readable output.
- Use `--filter` instead of post-processing whenever possible.

## Reference map

- General `gcloud` command tree: `https://docs.cloud.google.com/sdk/gcloud/reference`
- Cloud Build command group: `https://docs.cloud.google.com/sdk/gcloud/reference/builds`
- Trigger listing reference: `https://docs.cloud.google.com/sdk/gcloud/reference/builds/triggers/list`
- Trigger describe reference: `https://docs.cloud.google.com/sdk/gcloud/reference/builds/triggers/describe`
- Build logs reference: `https://docs.cloud.google.com/sdk/gcloud/reference/builds/log`