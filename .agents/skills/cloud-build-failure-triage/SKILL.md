---
name: cloud-build-failure-triage
description: 'Use when diagnosing a failed Google Cloud Build or asking whether Spotless caused a build failure. Classify the failing step as Maven or Terraform from build metadata and logs, then identify the failing goal or command with evidence.'
compatibility: gcloud CLI, Google Cloud auth, network access to Google Cloud APIs
---

# Cloud Build Failure Triage

Use this skill to establish what *failed*, not just what ran. A build may have Maven and Terraform steps; classify the **failing step**, not the repository or the entire build.

## 1. Obtain the build and logs

For a Console URL, extract the build ID, region, and project. Follow the project-number resolution guidance in the companion `gcloud-build-triggers` skill if the URL contains a numeric project. Use the project ID with both commands:

```bash
gcloud builds describe BUILD_ID --project=PROJECT_ID --region=REGION \
  --format="yaml(status,steps,logUrl,failureInfo)"
gcloud builds log BUILD_ID --project=PROJECT_ID --region=REGION
```

If the logs are too long to display, capture them to a local file and inspect bounded matches with context, then read the surrounding failing step. Do not infer success or failure from truncated output. If either command is denied, fails, or returns no logs, report that limitation explicitly; do not guess.

## 2. Locate the failing step

Check the build status and step statuses/exit codes in the build description. Correlate the failed step's ID/index and image or command with `Starting Step #N`, `Step #N`, `Finished Step #N`, and the terminal `ERROR: build step N ... failed` lines in the log. Read the error *within* that step, not only its final exit-code line. If a step runs multiple commands, identify which command failed. Do not classify from an earlier successful step or a later skipped step.

If the build has not failed, state its actual status. If the log is incomplete, the failed step cannot be identified, or several independent failures are present, state what is known and mark the cause unconfirmed.

## 3. Classify the failed step

Use the step's command, image, and logs together:

| Type | Positive evidence |
| --- | --- |
| Maven | `mvn`/`mvnw` invocation, Maven image, `pom.xml` in context, Maven reactor output or `--- plugin:version:goal ... ---` lines |
| Terraform | `terraform` invocation, Terraform image, `terraform init/validate/plan/apply/fmt` output or Terraform diagnostic lines |

If both occur, use the failed command within the failed step and report mixed context if relevant. If neither can be confirmed, use **unknown**; do not infer a build type from the repository name or a stray keyword.

## 4. Determine the actual cause

**Maven:** Locate `BUILD FAILURE` and the earliest actionable `[ERROR] Failed to execute goal ...` line for the failed module. Report **Spotless check failed** only if the failed goal is `com.diffplug.spotless:spotless-maven-plugin:...:check` and the log attributes format violations to it. Quote the relevant violation summary, affected module, and example files when present. A `spotless:check` invocation or `Spotless.Java ...` line *alone* does not prove it failed; another Maven goal may have failed later. Distinguish `spotless:apply` failures from `spotless:check`.

**Terraform:** Locate the failing Terraform command and its first concrete error (for example `terraform fmt -check`, `validate`, or `plan`). Report the actual diagnostic; never label it Spotless because a different step mentioned Spotless.

**Unknown or other cause:** Report the failed step and concrete error without forcing a Maven/Terraform or Spotless diagnosis.

## Response

Give the build ID and status, failed step, classified type (`Maven`, `Terraform`, or `unknown`), verdict (`Spotless check failed`, `other failure`, or `insufficient evidence`), and the log lines that justify it. Include a corrective command only when it fits the observed cause (for a confirmed Spotless format violation, `mvn spotless:apply` in the affected Maven project, followed by review of the diff). If the verdict is uncertain, say what evidence is missing and how to obtain it.
