---
name: gcloud-console-url-to-cli
description: Given a console.cloud.google.com URL, derive equivalent gcloud commands to inspect the same resource details without console access. Prioritizes exact project and region scoping and provides fallback discovery commands when the route is ambiguous.
compatibility: gcloud CLI, Google Cloud auth, network access to Google Cloud APIs
metadata:
  references:
    - https://docs.cloud.google.com/sdk/gcloud/reference
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds/describe
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds/log
    - https://docs.cloud.google.com/sdk/gcloud/reference/builds/triggers
    - https://docs.cloud.google.com/sdk/gcloud/reference/artifacts
    - https://docs.cloud.google.com/sdk/gcloud/reference/run/services
  author: dev-team
---

# gcloud Console URL to CLI

Use this skill when a user shares a Google Cloud Console URL but cannot access the console UI, and wants equivalent gcloud commands to retrieve the same details.

## When to use

- User provides a console.cloud.google.com link and asks for CLI equivalents
- User lacks IAM permissions for the console UI but can run gcloud
- Need exact resource inspection based on URL path and query values
- Need project or region-safe commands with minimal guesswork

## Core rules

- Always parse and carry project from query parameter project when present
- If the project parameter is a numeric project number, resolve it to a project ID before using `gcloud builds log`, which rejects project numbers:
  `gcloud projects list --filter="projectNumber=PROJECT_NUMBER" --format="value(projectId)"`.
  If no ID is returned, check the active account and project access; do not guess.
- Always parse and carry region or location from route or matrix segment when present
- Ignore console-only query parameters such as supportedpurview
- Prefer describe commands first, then list or log commands for verification
- If route parsing is ambiguous, provide a short discovery sequence instead of guessing

## URL parsing model

Extract:

1. Product route from path
2. Resource identifier from final path segment
3. Region or location from:
   - matrix segment such as ;region=europe-west2
   - route segment if embedded in path
4. Project from query parameter project

Console URL parts that are often relevant:

- Host: console.cloud.google.com
- Path and matrix parameters: identify product and region
- Query parameters: project is usually required

## Route to command mapping

### Cloud Build build details

Route pattern:
- /cloud-build/builds;region=REGION/BUILD_ID?project=PROJECT_ID

Primary command:
- gcloud builds describe BUILD_ID --project=PROJECT_ID --region=REGION

Useful follow-ups:
- gcloud builds log BUILD_ID --project=PROJECT_ID --region=REGION
- gcloud builds list --project=PROJECT_ID --region=REGION --filter="id=BUILD_ID"

### Cloud Build trigger details

Route pattern:
- /cloud-build/triggers;region=REGION/TRIGGER_ID?project=PROJECT_ID

Primary command:
- gcloud builds triggers describe TRIGGER_ID --project=PROJECT_ID --region=REGION

Useful follow-ups:
- gcloud builds list --project=PROJECT_ID --region=REGION --filter="buildTriggerId=TRIGGER_ID" --format="table(id,status,createTime,finishTime)"

### Artifact Registry repository view

Typical route pattern:
- /artifacts?project=PROJECT_ID (with repo/location visible in path fragments or page state)

Primary discovery:
- gcloud artifacts repositories list --project=PROJECT_ID --location=LOCATION
- gcloud artifacts repositories describe REPOSITORY --project=PROJECT_ID --location=LOCATION

### Cloud Run service details

Typical route pattern:
- /run/detail/REGION/SERVICE?project=PROJECT_ID

Primary command:
- gcloud run services describe SERVICE --project=PROJECT_ID --region=REGION

Useful follow-ups:
- gcloud run revisions list --project=PROJECT_ID --region=REGION --service=SERVICE
- gcloud run services describe SERVICE --project=PROJECT_ID --region=REGION --format=json

## Required response format

When given a URL, return:

1. Parsed fields:
   - product
   - project
   - region or location
   - resource id or name
2. Primary gcloud command
3. One to three verification or troubleshooting commands
4. If parsing confidence is low, state assumptions and provide discovery commands

## Example from user

Input URL:
https://console.cloud.google.com/cloud-build/builds;region=europe-west2/1bee2978-a4e0-4d12-aab9-47f18f6d1e79?project=c31-fwmtg-ci-prod&supportedpurview=project

Parsed:
- product: Cloud Build build details
- project: c31-fwmtg-ci-prod
- region: europe-west2
- build id: 1bee2978-a4e0-4d12-aab9-47f18f6d1e79

Equivalent commands:
- gcloud builds describe 1bee2978-a4e0-4d12-aab9-47f18f6d1e79 --project=c31-fwmtg-ci-prod --region=europe-west2
- gcloud builds log 1bee2978-a4e0-4d12-aab9-47f18f6d1e79 --project=c31-fwmtg-ci-prod --region=europe-west2
- gcloud builds list --project=c31-fwmtg-ci-prod --region=europe-west2 --filter="id=1bee2978-a4e0-4d12-aab9-47f18f6d1e79" --format="table(id,status,createTime,finishTime)"

Notes:
- supportedpurview is console metadata and not needed for gcloud
- region is mandatory here to avoid empty or mismatched results

## Fast troubleshooting rules

- If describe returns not found, verify project and region first
- If list is empty but console URL implies a resource exists, re-check region parsing
- If command works without region in one environment but not another, keep explicit region in recommended commands
- If path does not map cleanly to a known product route, provide product-specific list and describe discovery commands with project scoping
