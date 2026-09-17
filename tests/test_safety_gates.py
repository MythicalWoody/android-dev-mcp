import os
import tempfile
import unittest
from unittest import mock

from android_autodev import dependency_scanning, runtime, security_scanning, static_analysis
from android_autodev.tools import quality


class SecretScanningTests(unittest.IsolatedAsyncioTestCase):
    # Split the synthetic token in this test source so repository scanners do
    # not mistake the fixture itself for a committed credential.
    SUSPECT_TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJ"
    SECRET_DIFF = (
        "diff --git a/app/src/main/Secrets.kt b/app/src/main/Secrets.kt\n"
        "--- a/app/src/main/Secrets.kt\n"
        "+++ b/app/src/main/Secrets.kt\n"
        "@@ -0,0 +1,2 @@\n"
        f"+val apiKey = \"{SUSPECT_TOKEN}\"\n"
        "+val label = \"safe\"\n"
    )

    def test_reports_redacted_location_without_secret_value(self):
        findings = security_scanning.scan_diff(self.SECRET_DIFF)

        self.assertTrue(findings)
        self.assertEqual(findings[0]["path"], "app/src/main/Secrets.kt")
        self.assertEqual(findings[0]["line"], 1)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(findings))

    def test_ignores_removed_values_and_obvious_placeholders(self):
        diff = (
            "diff --git a/local.properties b/local.properties\n"
            "--- a/local.properties\n"
            "+++ b/local.properties\n"
            "@@ -1 +1 @@\n"
            f"-apiKey=\"{self.SUSPECT_TOKEN}\"\n"
            "+apiKey=\"YOUR_API_KEY_PLACEHOLDER\"\n"
        )

        self.assertEqual(security_scanning.scan_diff(diff), [])

    async def test_review_gate_blocks_a_secret_before_persisting_it(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as state:
            with mock.patch.object(runtime, "ALLOWED_PROJECT_ROOT", project), mock.patch.object(
                runtime, "LOG_DIR", state
            ):
                result = await runtime.request_code_review(project, "Unsafe change", self.SECRET_DIFF)

        self.assertEqual(result["status"], "FAILURE")
        self.assertEqual(result["error_code"], "REVIEW_SAFETY_CHECK_FAILED")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result["error_output"])

    async def test_apply_gate_rechecks_a_preexisting_approval(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as state:
            with mock.patch.object(runtime, "ALLOWED_PROJECT_ROOT", project), mock.patch.object(
                runtime, "LOG_DIR", state
            ):
                token, _record = runtime._store_code_review(project, "Legacy approval", self.SECRET_DIFF)
                with mock.patch.object(runtime, "_git_apply", new=mock.AsyncMock()) as git_apply:
                    result = await runtime.apply_reviewed_patch(project, self.SECRET_DIFF, token)

        self.assertEqual(result["status"], "FAILURE")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result["error_output"])
        git_apply.assert_not_awaited()


class DependencyScanningTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_resolved_versions_and_deduplicates_coordinates(self):
        output = """
+--- com.squareup.okhttp3:okhttp:4.11.0 -> 4.12.0
|    \\--- com.squareup.okio:okio:3.6.0
\\--- com.squareup.okhttp3:okhttp:4.12.0
"""

        self.assertEqual(
            dependency_scanning.parse_gradle_dependencies(output),
            [
                {"group": "com.squareup.okhttp3", "artifact": "okhttp", "version": "4.12.0"},
                {"group": "com.squareup.okio", "artifact": "okio", "version": "3.6.0"},
            ],
        )

    def test_detects_dependency_configuration_paths(self):
        self.assertTrue(dependency_scanning.affects_dependencies(["gradle/libs.versions.toml"]))
        self.assertTrue(dependency_scanning.affects_dependencies(["app/build.gradle.kts"]))
        self.assertTrue(dependency_scanning.affects_dependencies(["gradle.properties"]))
        self.assertTrue(dependency_scanning.affects_dependencies(["buildSrc/src/main/kotlin/Deps.kt"]))
        self.assertFalse(dependency_scanning.affects_dependencies(["app/src/main/Main.kt"]))

    async def test_dependency_diff_requires_managed_scanned_workspace(self):
        diff = (
            "diff --git a/app/build.gradle.kts b/app/build.gradle.kts\n"
            "--- a/app/build.gradle.kts\n"
            "+++ b/app/build.gradle.kts\n"
            "@@ -1 +1 @@\n"
            "-implementation(\"a:b:1\")\n"
            "+implementation(\"a:b:2\")\n"
        )
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as state:
            with mock.patch.object(runtime, "ALLOWED_PROJECT_ROOT", project), mock.patch.object(
                runtime, "LOG_DIR", state
            ):
                result = await runtime.request_code_review(project, "Bump dependency", diff)

        self.assertEqual(result["status"], "FAILURE")
        self.assertEqual(result["error_code"], "DEPENDENCY_REVIEW_WORKSPACE_REQUIRED")

    async def test_exact_workspace_dependency_scan_is_bound_to_review(self):
        with tempfile.TemporaryDirectory() as allowed_root, tempfile.TemporaryDirectory() as state:
            project = os.path.join(allowed_root, "project")
            os.makedirs(project)
            project = os.path.realpath(project)
            with open(os.path.join(project, "build.gradle.kts"), "w") as handle:
                handle.write('dependencies { implementation("a:b:1") }\n')
            with mock.patch.object(runtime, "ALLOWED_PROJECT_ROOT", allowed_root), mock.patch.object(
                runtime, "LOG_DIR", state
            ):
                workflow_id, _record = runtime.workflows.create_workflow(state, project, "dependency bump")
                prepared = await runtime.prepare_code_review_workspace(project)
                workspace = prepared["workspace_path"]
                with open(os.path.join(workspace, "build.gradle.kts"), "w") as handle:
                    handle.write('dependencies { implementation("a:b:2") }\n')
                proposed_diff = await runtime._review_workspace_diff(workspace)
                clean_scan = {
                    "status": "SUCCESS",
                    "dependency_count": 1,
                    "findings": [],
                }
                with mock.patch.object(
                    runtime.dependency_scanning,
                    "scan_gradle_project",
                    new=mock.AsyncMock(return_value=clean_scan),
                ):
                    result = await runtime.request_code_review(
                        project,
                        "Bump dependency",
                        proposed_diff,
                        review_workspace_path=workspace,
                        workflow_id=workflow_id,
                    )

        self.assertEqual(result["status"], "AWAITING_USER_REVIEW", result)
        self.assertEqual(result["safety_checks"]["dependency_scan"]["status"], "SUCCESS")
        self.assertTrue(result["review_workspace_removed"])
        self.assertFalse(os.path.exists(workspace))


class _TaskProcess:
    def __init__(self, output: bytes):
        self.output = output
        self.returncode = 0
        self.pid = 1234

    async def communicate(self):
        return self.output, b""


class StaticAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_uat_specific_detekt_and_ktlint_tasks(self):
        output = b"detekt - Analyze Kotlin\ndetektUatDebug - Analyze UAT\nktlintCheck - Check format\n"
        with mock.patch(
            "android_autodev.static_analysis.asyncio.create_subprocess_exec",
            return_value=_TaskProcess(output),
        ):
            tasks = await static_analysis.discover_tasks("/tmp/project", "app")

        self.assertEqual(tasks["detekt"], ":app:detektUatDebug")
        self.assertEqual(tasks["ktlint"], ":app:ktlintCheck")
        self.assertTrue(runtime._is_gradle_command_allowed(tasks["detekt"]))
        self.assertTrue(runtime._is_gradle_command_allowed(tasks["ktlint"]))

    async def test_quality_gate_stops_before_gradle_when_dependency_scan_fails(self):
        profile = {"recommended_module": "app"}
        with mock.patch.object(runtime, "validate_path", return_value="/tmp/project"), mock.patch(
            "android_autodev.tools.quality.project_service.inspect_project", return_value=profile
        ), mock.patch.object(
            quality,
            "scan_dependency_vulnerabilities",
            new=mock.AsyncMock(return_value={"status": "FAILURE", "error_code": "VULNERABLE_DEPENDENCIES"}),
        ), mock.patch.object(quality, "run_static_analysis", new=mock.AsyncMock()) as static_gate:
            result = await quality.run_quality_gate("/tmp/project", "workflow")

        self.assertEqual(result["status"], "FAILURE")
        self.assertEqual(result["failed_gate"], "dependency-vulnerabilities")
        static_gate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
