---
name: gcloud-cli-reference
description: Use when the user needs general Google Cloud CLI help with gcloud command discovery, auth and project scoping, regions, filters, output formatting, and reference-driven troubleshooting. Covers how to navigate the main gcloud reference and product subcommand groups such as builds, artifacts, storage, run, and iam.
compatibility: gcloud CLI, Google Cloud auth, network access to Google Cloud APIs
metadata:
  references:
    - https://docs.cloud.google.com/sdk/gcloud/reference
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds
  author: dev-team
---

# gcloud CLI Reference

Use this skill when the task is about the Google Cloud CLI in general, not just one Cloud Build trigger workflow.

## When to use

- User asks for the correct `gcloud` command for a Google Cloud task
- User needs help navigating product command groups under `gcloud`
- User needs help with project, account, region, or location scoping
- User needs filters, table output, JSON output, or machine-readable CLI results
- User sees a console-versus-CLI mismatch and needs a systematic CLI troubleshooting flow

## Core model

Think of the CLI in three layers:

1. Top-level entry point: `gcloud`
2. Product group: `builds`, `run`, `artifacts`, `storage`, `iam`, `projects`
3. Action: `list`, `describe`, `create`, `update`, `delete`, `log`, `run`

Pattern:

```bash
gcloud GROUP SUBGROUP ACTION [RESOURCE] [FLAGS]
```

Examples:

```bash
gcloud builds list
gcloud builds triggers list
gcloud run services list
gcloud artifacts repositories list
gcloud projects describe PROJECT_ID
```

## First checks

Before diagnosing command behavior, verify the active CLI context.

### Active account

```bash
gcloud auth list
```

### Active project

```bash
gcloud config get-value project
```

### Current configuration

```bash
gcloud config list
```

### Optional product-specific defaults

```bash
gcloud config get-value builds/region
```

## Command discovery flow

### 1. Start with the main reference

Use the main command tree to identify the correct product group:

- `https://docs.cloud.google.com/sdk/gcloud/reference`

If the product is Cloud Build, the CLI group is `builds`.

### 2. Move into the product reference

Example:

- `https://docs.cloud.google.com/sdk/gcloud/reference/builds`

This shows available operations like `list`, `describe`, `log`, `submit`, and nested groups such as `triggers`.

### 3. Use the exact subcommand reference when needed

Example:

- `https://docs.cloud.google.com/sdk/gcloud/reference/builds/triggers/list`

Prefer exact subcommand references when the user needs flag syntax or command shape.

## Scoping rules

Most incorrect CLI results come from one of these scope mismatches:

- Wrong account
- Wrong project
- Missing region or location
- Wrong product subgroup

### Project scoping

Prefer explicit project targeting:

```bash
gcloud ... --project=PROJECT_ID
```

### Region and location scoping

Many resources are regional. If the console shows resources but CLI shows none, try the regional form:

```bash
gcloud ... --region=REGION
```

or:

```bash
gcloud ... --location=LOCATION
```

Do not assume global scope unless the product reference says so.

## Output shaping

### Human-friendly tables

```bash
gcloud ... --format="table(name,id,createTime)"
```

### Machine-readable output

```bash
gcloud ... --format=json
```

### Focus results with filters

```bash
gcloud ... --filter="name~my-service"
```

Prefer built-in `--filter` and `--format` over post-processing where possible.

## Troubleshooting flow

### Command returns no resources

Check in this order:

1. `gcloud auth list`
2. `gcloud config get-value project`
3. required `--project`
4. required `--region` or `--location`
5. correct product group from the reference tree

### Console and CLI disagree

Assume a scope mismatch first, not missing resources.

Most common causes:

- Console is on a different project
- Console is showing a regional resource
- CLI is using a different authenticated account
- CLI command is using the wrong subgroup

### Need exact syntax

Use the main reference to find the group, then the exact subcommand reference to confirm flags and examples.

## Recommended operator patterns

### Discover the right group

```bash
gcloud help
```

### Inspect product help

```bash
gcloud builds --help
```

### Inspect exact command help

```bash
gcloud builds triggers list --help
```

### Confirm CLI version

```bash
gcloud version
```

## Good response patterns

- Give the exact command first when the user asks for a command.
- If the result can vary by project or region, include those flags explicitly.
- If the user reports unexpected empty output, pivot immediately to auth, project, and region checks.
- If the task concerns Cloud Build specifically, prefer the companion `gcloud-build-triggers` skill for trigger workflows and build-log inspection.

## Reference map

- Main `gcloud` reference: `https://docs.cloud.google.com/sdk/gcloud/reference`
- Cloud Build group: `https://docs.cloud.google.com/sdk/gcloud/reference/builds`
- Use the product group pages to discover the correct nested subcommands before suggesting flags from memory.