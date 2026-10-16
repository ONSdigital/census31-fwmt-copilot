import unittest

from evidence import EvidenceClient


SHA = "a" * 40
CI_SHA = "b" * 40
BUILD = "123e4567-e89b-12d3-a456-426614174000"
TRIGGER = "323e4567-e89b-12d3-a456-426614174000"
COMMAND = ["mvn", "-B", "spotless:check"]
URL = f"https://console.cloud.google.com/cloud-build/builds/{BUILD}?project=ci-project&region=europe-west2"
LOG = (
    "Starting Step #0\n"
    "[ERROR] Failed to execute goal com.diffplug.spotless:spotless-maven-plugin:2.43.0:check "
    "(default-cli) on project app: The following files had format violations:\n"
    "[ERROR] src/main/java/App.java\n"
    "[ERROR] BUILD FAILURE\n"
    "Finished Step #0\n"
    "ERROR: build step 0 failed: step exited with non-zero status: 1\n"
)


class Github:
    def __init__(self):
        self.pr = {"state": "open", "head": {"sha": SHA, "ref": "fix-format",
                                           "repo": {"full_name": "acme/fork"}}}
        self.check = {"id": 9, "head_sha": SHA, "status": "completed",
                      "conclusion": "failure", "name": "ci",
                      "app": {"slug": "google-cloud-build"},
                      "details_url": URL, "external_id": BUILD}
        self.pages = [[self.check], []]
        self.config = "steps:\n- name: maven:3\n  args: ['-B', 'spotless:check']\n"
        self.reads = []

    def pull(self, repo, number):
        return self.pr

    def check_run(self, repo, check_id):
        return self.check

    def check_runs(self, repo, sha, page):
        return self.pages[page - 1]

    def config_file(self, repo, path, ref):
        self.reads.append((repo, path, ref))
        return self.config


class Cloud:
    def __init__(self):
        self.result = {
            "id": BUILD, "projectId": "ci-project", "status": "FAILURE",
            "substitutions": {"COMMIT_SHA": SHA},
            "sourceProvenance": {"resolvedRepoSource": {"commitSha": CI_SHA,
                                                        "repoName": "acme/ci"}},
            "steps": [{"name": "maven:3", "args": COMMAND[1:], "entrypoint": "mvn",
                       "status": "FAILURE", "id": "format"}],
        }
        self.log = LOG
        self.trigger_result = {"id": TRIGGER, "filename": "cloudbuild.yaml",
                               "github": {"owner": "acme", "name": "ci"}}
        self.project_result = {"projectId": "ci-project", "projectNumber": "840167115713"}

    def build(self, build_id, project, region):
        return self.result

    def logs(self, build_id, project, region):
        return self.log

    def trigger(self, trigger_id, project, region):
        return self.trigger_result

    def project(self, project_number):
        return self.project_result


def event():
    return {"repository": "acme/app", "prNumber": 42, "headSha": SHA,
            "checkRunId": 9, "runUrl": URL, "source": "google-cloud-build",
            "conclusion": "failure", "configRepository": "acme/ci",
            "configSha": CI_SHA, "configPath": "cloudbuild.yaml"}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.github, self.cloud = Github(), Cloud()
        self.client = EvidenceClient(self.github, self.cloud)

    def test_confirms_sole_spotless_failure_and_exact_command(self):
        result = self.client.inspect(event())
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["command"], COMMAND)
        self.assertEqual((result["head_repo"], result["head_ref"], result["head_sha"]),
                         ("acme/fork", "fix-format", SHA))
        self.assertEqual(result["build_id"], BUILD)
        self.assertEqual(result["other_failures"], [])
        self.assertEqual(result["violatingFiles"], ["src/main/java/App.java"])
        self.assertEqual(self.github.reads, [("acme/ci", "cloudbuild.yaml", CI_SHA)])
        self.assertEqual(self.client.recheck(event(), result), {
            "headSha": SHA, "headRef": "fix-format", "complete": True,
            "otherFailures": [],
        })

    def test_recheck_fails_on_new_head_or_other_failed_check(self):
        evidence = self.client.inspect(event())
        self.github.pr["head"]["sha"] = "c" * 40
        self.assertNotEqual(self.client.recheck(event(), evidence).get("complete"), True)
        self.github.pr["head"]["sha"] = SHA
        self.github.pages = [[self.github.check, {"id": 10, "conclusion": "failure"}], []]
        self.assertNotEqual(self.client.recheck(event(), evidence).get("complete"), True)

    def test_recheck_requires_successful_inspection_and_same_branch(self):
        self.assertNotEqual(self.client.recheck(event()).get("complete"), True)
        evidence = self.client.inspect(event())
        self.assertTrue(evidence["eligible"])
        self.github.pr["head"]["ref"] = "new-branch"
        self.assertNotEqual(self.client.recheck(event(), evidence).get("complete"), True)
        self.github.pr["head"]["ref"] = "fix-format"
        self.github.pr["head"]["repo"]["full_name"] = "acme/other-fork"
        self.assertNotEqual(self.client.recheck(event(), evidence).get("complete"), True)

    def test_recheck_uses_persisted_verified_evidence_after_restart(self):
        evidence = self.client.inspect(event())
        restarted = EvidenceClient(self.github, self.cloud)
        self.assertTrue(restarted.recheck(event(), evidence)["complete"])
        for value in ({**evidence, "head_repo": "acme/other-fork"},
                      {**evidence, "head_sha": "c" * 40},
                      {**evidence, "build_id": "0" * 36},
                      {**evidence, "eligible": False}):
            self.assertNotEqual(restarted.recheck(event(), value).get("complete"), True)
        self.assertNotEqual(restarted.recheck(
            {**event(), "head_ref": "fix-format", "head_repo": "acme/fork"}).get("complete"), True)

    def test_missing_or_unsafe_violation_paths_never_authorize_repair(self):
        for path in ("../outside.java", "/tmp/Outside.java", "src/../Outside.java",
                     "src/Else.kt", "src/.hidden.java", "src/Bad Name.java", "a\\b.java"):
            self.cloud.log = LOG.replace("src/main/java/App.java", path)
            with self.subTest(path=path):
                result = self.client.inspect(event())
                self.assertFalse(result["eligible"])
                self.assertEqual(result["violatingFiles"], [])
        self.cloud.log = LOG.replace("[ERROR] src/main/java/App.java\n", "")
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.assertEqual(self.client.inspect(event())["violatingFiles"], [])

    def test_multiple_paths_only_from_spotless_violation_block(self):
        self.cloud.log = LOG.replace(
            "[ERROR] src/main/java/App.java\n",
            "[ERROR]     src/main/java/App.java\n[ERROR]   src/test/java/AppTest.java\n"
            "[ERROR] Run 'mvn spotless:apply'\n[ERROR] unrelated/Other.java\n")
        self.assertEqual(self.client.inspect(event())["violatingFiles"],
                         ["src/main/java/App.java", "src/test/java/AppTest.java"])
        self.cloud.log = LOG.replace(
            "[ERROR] src/main/java/App.java\n",
            "[ERROR] src/main/java/App.java\n[ERROR] src/main/java/App.java\n")
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_spotless_diff_between_sample_java_paths_is_not_a_boundary(self):
        self.cloud.log = LOG.replace(
            "[ERROR] src/main/java/App.java\n",
            "[ERROR]     src/main/java/App.java\n"
            "[ERROR]         @@ -1,3 +1,3 @@\n"
            "[ERROR]         -old line\n"
            "[ERROR]         +new line\n"
            "[ERROR]     src/test/java/AppTest.java\n"
            "[ERROR]     48 needs changes\n"
            "[ERROR] Run 'mvn spotless:apply'\n"
            "[ERROR] src/test/java/Unrelated.java\n",
        )
        result = self.client.inspect(event())
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["violatingFiles"],
                         ["src/main/java/App.java", "src/test/java/AppTest.java"])
        self.assertIs(result["violatingFilesExhaustive"], False)

    def test_prefixed_spotless_diff_keeps_later_sample_path(self):
        self.cloud.log = (
            'Starting Step #0 - "Test"\n'
            'Step #0 - "Test": [ERROR] Failed to execute goal '
            'com.diffplug.spotless:spotless-maven-plugin:2.44.0:check '
            'on project app: The following files had format violations:\n'
            'Step #0 - "Test": [ERROR]     src/main/java/App.java\n'
            'Step #0 - "Test": [ERROR]         @@ -1,3 +1,3 @@\n'
            'Step #0 - "Test": [ERROR]         +formatting\n'
            'Step #0 - "Test": [ERROR]     src/test/java/AppTest.java\n'
            'Step #0 - "Test": [ERROR] Run spotless:apply to fix these violations\n'
            'Finished Step #0 - "Test"\n'
            'ERROR: build step 0 failed: exit status 1\n'
        )
        result = self.client.inspect(event())
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["violatingFiles"],
                         ["src/main/java/App.java", "src/test/java/AppTest.java"])

    def test_sample_scan_stops_at_maven_failure_footer(self):
        self.cloud.log = LOG.replace(
            "[ERROR] src/main/java/App.java\n",
            "[ERROR] src/main/java/App.java\n"
            "[ERROR]       @@ -2,2 +2,2 @@\n"
            "[ERROR] src/test/java/AppTest.java\n"
            "[INFO] BUILD FAILURE\n"
            "[ERROR] src/main/java/OutsideSpotless.java\n",
        )
        result = self.client.inspect(event())
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["violatingFiles"],
                         ["src/main/java/App.java", "src/test/java/AppTest.java"])

    def test_real_cloud_console_matrix_url_numeric_project_and_trigger_config(self):
        link = (f"https://console.cloud.google.com/cloud-build/builds;region=europe-west2/"
                f"{BUILD}?project=840167115713")
        payload = {key: value for key, value in event().items()
                   if key not in ("configRepository", "configSha", "configPath")}
        payload["runUrl"] = link
        self.github.check["details_url"] = link
        self.cloud.result["buildTriggerId"] = TRIGGER
        result = self.client.inspect(payload)
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(self.github.reads, [("acme/ci", "cloudbuild.yaml", CI_SHA)])
        self.assertTrue(EvidenceClient(self.github, self.cloud).recheck(payload, result)["complete"])

    def test_missing_or_inconsistent_trigger_config_fails_closed(self):
        payload = {key: value for key, value in event().items()
                   if key not in ("configRepository", "configSha", "configPath")}
        self.cloud.result["buildTriggerId"] = TRIGGER
        self.cloud.trigger_result["github"]["owner"] = "other"
        self.assertFalse(self.client.inspect(payload)["eligible"])
        self.cloud.trigger_result["github"]["owner"] = "acme"
        self.cloud.trigger_result["filename"] = "../evil.yaml"
        self.assertFalse(self.client.inspect(payload)["eligible"])
        self.cloud.result.pop("buildTriggerId")
        self.assertFalse(self.client.inspect(payload)["eligible"])

    def test_lifecycle_goals_preserve_exact_build_invocation(self):
        args = ["-B", "clean", "test", "package", "spotless:check"]
        self.cloud.result["steps"][0]["args"] = args
        self.github.config = "steps:\n- name: maven:3\n  args: " + repr(args) + "\n"
        result = self.client.inspect(event())
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["command"], ["mvn", *args])
        self.cloud.result["steps"][0]["args"] = ["-B", "clean", "spotless:check", "package"]
        self.github.config = ("steps:\n- name: maven:3\n  args: "
                              + repr(self.cloud.result["steps"][0]["args"]) + "\n")
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_real_git_provenance_script_and_prefixed_step_log(self):
        payload = {key: value for key, value in event().items()
                   if key not in ("configRepository", "configSha", "configPath")}
        payload["runUrl"] = (f"https://console.cloud.google.com/cloud-build/builds;"
                             f"region=europe-west2/{BUILD}?project=840167115713")
        self.github.check["details_url"] = payload["runUrl"]
        self.github.check["external_id"] = "YWJjLW9wYXF1ZQ=="
        self.cloud.result["sourceProvenance"] = {
            "resolvedGitSource": {"revision": SHA, "url": "https://github.com/acme/app.git"}}
        self.cloud.result["substitutions"] = {
            "TRIGGER_BUILD_CONFIG_PATH": "cloudbuild-snapshot.yaml",
            "COMMIT_SHA": SHA,
        }
        script = ('EFFECTIVE_VERSION="$(cat /workspace/effective-version.txt)"\n'
                  'mvn -B -ntp -s /workspace/settings.xml '
                  '-Drevision="${EFFECTIVE_VERSION}" clean verify spotless:check\n')
        self.cloud.result["steps"] = [
            {"id": "Prepare Maven Auth", "name": "ubuntu", "status": "SUCCESS"},
            {"id": "Compute Snapshot Version", "name": "ubuntu", "status": "SUCCESS"},
            {"id": "Test", "name": "maven:3.9.16-eclipse-temurin-25", "status": "FAILURE",
             "entrypoint": "bash", "args": ["-ceu", script]},
            {"id": "Publish Snapshot", "name": "ubuntu", "status": "QUEUED"},
        ]
        self.github.config = (
            "steps:\n"
            "  - name: ubuntu\n    id: Prepare Maven Auth\n"
            "  - name: ubuntu\n    id: Compute Snapshot Version\n"
            "  - name: maven:3.9.16-eclipse-temurin-25\n    id: Test\n"
            "    entrypoint: bash\n    args:\n      - -ceu\n      - |\n"
            "        EFFECTIVE_VERSION=\"$(cat /workspace/effective-version.txt)\"\n"
            "        mvn -B -ntp -s /workspace/settings.xml "
            "-Drevision=\"$${EFFECTIVE_VERSION}\" clean verify spotless:check\n"
            "  - name: ubuntu\n    id: Publish Snapshot\n"
        )
        self.cloud.log = (
            'Starting Step #2 - "Test"\n'
            'Step #2 - "Test": [ERROR] Failed to execute goal '
            'com.diffplug.spotless:spotless-maven-plugin:2.44.0:check (default-cli) '
            'on project app: The following files had format violations:\n'
            'Step #2 - "Test": [ERROR]     src/main/java/GatewayException.java\n'
            'Step #2 - "Test": [INFO] BUILD FAILURE\n'
            'Finished Step #2 - "Test"\n'
            'ERROR: build step 2 "ubuntu" failed: step exited with non-zero status: 1\n'
        )
        result = self.client.inspect(payload)
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["violatingFiles"], ["src/main/java/GatewayException.java"])
        self.assertEqual(result["command"], ["mvn", "-B", "-ntp", "clean", "verify", "spotless:check"])
        self.assertEqual(self.github.reads, [("acme/app", "cloudbuild-snapshot.yaml", SHA)])

    def test_real_shape_script_or_provenance_mismatch_is_rejected(self):
        payload = {key: value for key, value in event().items()
                   if key not in ("configRepository", "configSha", "configPath")}
        self.cloud.result["sourceProvenance"] = {
            "resolvedGitSource": {"revision": SHA, "url": "https://github.com/acme/app.git"}}
        self.cloud.result["substitutions"]["TRIGGER_BUILD_CONFIG_PATH"] = "cloudbuild-snapshot.yaml"
        self.cloud.result["steps"][0]["entrypoint"] = "bash"
        self.cloud.result["steps"][0]["args"] = ["-ceu", "mvn spotless:check; curl evil.invalid"]
        self.github.config = ("steps:\n- name: maven:3\n  entrypoint: bash\n"
                              "  args: ['-ceu', 'mvn spotless:check; curl evil.invalid']\n")
        self.assertFalse(self.client.inspect(payload)["eligible"])
        self.cloud.result["sourceProvenance"]["resolvedGitSource"]["revision"] = "c" * 40
        self.assertFalse(self.client.inspect(payload)["eligible"])

    def test_config_step_id_mismatch_and_numeric_project_spoof_fail_closed(self):
        self.github.config = (
            "steps:\n- name: maven:3\n  id: unrelated\n"
            "  args: ['-B', 'spotless:check']\n")
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.github.config = "steps:\n- name: maven:3\n  args: ['-B', 'spotless:check']\n"
        numeric = {**event(), "runUrl": (
            f"https://console.cloud.google.com/cloud-build/builds;region=europe-west2/"
            f"{BUILD}?project=840167115713")}
        self.github.check["details_url"] = numeric["runUrl"]
        self.cloud.project_result["projectNumber"] = "111111111111"
        self.assertFalse(self.client.inspect(numeric)["eligible"])

    def test_unknown_cloud_build_app_slug_does_not_pass_identity(self):
        self.github.check["app"]["slug"] = "cloud-build"
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_other_failed_check_is_reported(self):
        self.github.pages = [[self.github.check, {"id": 10, "name": "lint",
                                                   "conclusion": "failure"}], []]
        result = self.client.inspect(event())
        self.assertFalse(result["eligible"])
        self.assertIn("lint", result["other_failures"][0])

    def test_other_failed_step_and_goal_are_not_spotless_only(self):
        self.cloud.result["steps"].append({"id": "test", "status": "FAILURE",
                                            "entrypoint": "mvn", "args": ["test"]})
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.cloud.result["steps"].pop()
        self.cloud.log += "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.0:test\n"
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_spotless_mentioned_but_failure_elsewhere_is_not_eligible(self):
        self.cloud.log = "Starting Step #0\nSpotless.Java checked\n" \
                         "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:compile\n" \
                         "Finished Step #0\nERROR: build step 0 failed\n"
        result = self.client.inspect(event())
        self.assertFalse(result["eligible"])
        self.assertIn("failed Maven goal", result["reason"])

    def test_incomplete_logs_pagination_and_provider_errors_fail_closed(self):
        self.cloud.log = LOG.replace("Finished Step #0\n", "")
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.cloud.log = LOG
        self.github.pages = [None]
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.github.pages = [[self.github.check], []]
        self.github.check["app"]["slug"] = "other-ci"
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_missing_target_run_and_duplicate_check_ids_fail_closed(self):
        self.github.pages = [[{"id": 10, "conclusion": "success"}], []]
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.github.pages = [[self.github.check, self.github.check], []]
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_stale_identity_and_missing_revision_fail_closed(self):
        self.github.check["head_sha"] = "c" * 40
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.github.check["head_sha"] = SHA
        self.cloud.result["substitutions"] = {}
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_refuses_unsafe_git_repo_and_branch_names(self):
        self.github.pr["head"]["repo"]["full_name"] = "../app"
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.github.pr["head"]["repo"]["full_name"] = "acme/fork"
        for ref in ("-push", "feature/../main", "feature//main", "main.lock", "feature/"):
            self.github.pr["head"]["ref"] = ref
            with self.subTest(ref=ref):
                self.assertFalse(self.client.inspect(event())["eligible"])

    def test_config_must_match_immutable_ci_revision_and_command(self):
        self.cloud.result["sourceProvenance"]["resolvedRepoSource"]["commitSha"] = "c" * 40
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.cloud.result["sourceProvenance"]["resolvedRepoSource"]["commitSha"] = CI_SHA
        self.github.config = "steps:\n- name: maven:3\n  args: ['test']\n"
        self.assertFalse(self.client.inspect(event())["eligible"])

    def test_scripts_shell_tokens_and_non_maven_commands_are_rejected(self):
        self.cloud.result["steps"][0]["args"] = ["-B", "spotless:check;rm -rf ."]
        self.assertFalse(self.client.inspect(event())["eligible"])
        self.cloud.result["steps"][0]["args"] = COMMAND[1:]
        self.cloud.result["steps"][0]["entrypoint"] = "bash"
        self.assertFalse(self.client.inspect(event())["eligible"])


if __name__ == "__main__":
    unittest.main()
