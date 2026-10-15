---
name: gcloud-artifact-registry
description: Use when the user needs Google Artifact Registry help with gcloud, especially to inspect repositories, packages, and versions, verify regional repository scope, and troubleshoot Maven, npm, or container publish and fetch problems involving pkg.dev.
compatibility: gcloud CLI, Google Cloud auth, network access to Google Cloud APIs
metadata:
  references:
    - https://docs.cloud.google.com/sdk/gcloud/reference/artifacts
    - https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/repositories
    - https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/packages
    - https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/versions
  author: dev-team
---

# gcloud Artifact Registry

Use this skill when the task involves Google Artifact Registry through the `gcloud` CLI, especially for repository inspection, package/version lookup, and troubleshooting publish failures from Cloud Build or local tooling.

## When to use

- User needs to list or inspect Artifact Registry repositories
- Build logs mention `pkg.dev`
- Maven, npm, or container publish steps fail
- User needs to confirm whether a package or version exists in a repository
- User needs to validate repository location, naming, or IAM-related assumptions from CLI output

## Core model

Artifact Registry work usually follows this chain:

1. Repository
2. Package
3. Version

Relevant command groups:

- `gcloud artifacts repositories`
- `gcloud artifacts packages`
- `gcloud artifacts versions`

Always treat Artifact Registry resources as location-scoped.

## Core rules

- Always pass `--project` when the target project matters.
- Always pass `--location` for repository, package, and version queries.
- Do not assume the repository is global.
- If the error contains `*.pkg.dev`, extract the region and repository name from the URL first.

Example repository URL:

```text
https://europe-west2-maven.pkg.dev/PROJECT_ID/REPOSITORY
```

From that URL you can derive:

- location: `europe-west2`
- project: `PROJECT_ID`
- repository: `REPOSITORY`

## Common commands

### List repositories

```bash
gcloud artifacts repositories list \
  --project=PROJECT_ID \
  --location=LOCATION
```

### Describe one repository

```bash
gcloud artifacts repositories describe REPOSITORY \
  --project=PROJECT_ID \
  --location=LOCATION
```

### List packages in a repository

```bash
gcloud artifacts packages list \
  --project=PROJECT_ID \
  --location=LOCATION \
  --repository=REPOSITORY
```

### Describe one package

```bash
gcloud artifacts packages describe PACKAGE_NAME \
  --project=PROJECT_ID \
  --location=LOCATION \
  --repository=REPOSITORY
```

### List versions for a package

```bash
gcloud artifacts versions list \
  --project=PROJECT_ID \
  --location=LOCATION \
  --repository=REPOSITORY \
  --package=PACKAGE_NAME
```

### Describe one version

```bash
gcloud artifacts versions describe VERSION \
  --project=PROJECT_ID \
  --location=LOCATION \
  --repository=REPOSITORY \
  --package=PACKAGE_NAME
```

## Fast troubleshooting flow

### 1. Repository URL appears in an error

Start by extracting location, project, and repository from the URL.

Then verify the repository directly:

```bash
gcloud artifacts repositories describe REPOSITORY \
  --project=PROJECT_ID \
  --location=LOCATION
```

### 2. Publish failed from Maven or Cloud Build

Check in this order:

1. Repository exists in the expected location
2. Project is correct
3. Package name being published is the one you expect
4. Version visibility matches expectations
5. Build log’s first concrete repository error

Commands:

```bash
gcloud artifacts repositories describe REPOSITORY \
  --project=PROJECT_ID \
  --location=LOCATION
```

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

### 3. User says the artifact exists but CLI cannot find it

Treat this first as a location or repository mismatch.

Check:

- same project
- same location
- same repository
- same package identifier

### 4. Cloud Build error mentions `Could not find artifact`

This does not always mean the repository is missing.

It can indicate:

- wrong repository name
- wrong location
- wrong package coordinates
- publish process failed before the artifact became visible

Use the log plus repository/package/version queries together instead of relying on the exception text alone.

## Output tips

- Use `--format="table(...)"` for operator-readable output.
- Use `--format=json` when the user needs exact package or version payloads.
- Prefer direct repository/package/version commands over trying to infer state only from build logs.

## Recommended response patterns

- If the user provides a `pkg.dev` URL, parse it explicitly.
- If the failure started in Cloud Build, use the build log to identify the failing repository first, then pivot into `gcloud artifacts` commands.
- If the user is troubleshooting Maven publish failures, combine Artifact Registry inspection with the Cloud Build trigger/build history workflow.

## Reference map

- Main Artifact Registry group: `https://docs.cloud.google.com/sdk/gcloud/reference/artifacts`
- Repository commands: `https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/repositories`
- Package commands: `https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/packages`
- Version commands: `https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/versions`