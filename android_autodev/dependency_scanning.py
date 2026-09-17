"""Gradle dependency resolution and OSV vulnerability scanning.

The scanner resolves the UAT Debug runtime graph rather than trusting declared
versions, then submits only Maven coordinates to OSV's batch API.  It fails
closed when resolution or vulnerability lookup cannot complete.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from typing import Any

import httpx

from . import project as project_service


OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_COORDINATE_RE = re.compile(
    r"(?:^|\s)([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^\s()]+)(?:\s+->\s+([^\s()]+))?"
)
_DEPENDENCY_FILENAMES = {
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "libs.versions.toml",
    "gradle.lockfile",
    "dependencies.lock",
    "gradle.properties",
    "gradle-wrapper.properties",
    "verification-metadata.xml",
    "versions.properties",
}


async def _terminate_gradle(process: asyncio.subprocess.Process) -> None:
    """Terminate the complete Gradle process group after a bounded wait."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def affects_dependencies(changed_files: list[str]) -> bool:
    """Identify review paths that can alter the resolved dependency graph."""
    for path in changed_files:
        normalized = path.replace("\\", "/").lstrip("/")
        filename = normalized.rsplit("/", 1)[-1]
        if filename in _DEPENDENCY_FILENAMES or filename.endswith(".versions.toml"):
            return True
        if normalized.startswith(("buildSrc/", "build-logic/")) and filename.endswith((".kt", ".kts")):
            return True
        if "/dependency-locks/" in f"/{normalized}" and filename.endswith(".lockfile"):
            return True
    return False


def parse_gradle_dependencies(output: str) -> list[dict[str, str]]:
    """Extract unique resolved Maven coordinates from Gradle's dependency report."""
    coordinates: dict[tuple[str, str, str], dict[str, str]] = {}
    for line in output.splitlines():
        if "project :" in line or " FAILED" in line:
            continue
        for match in _COORDINATE_RE.finditer(line):
            group, artifact, declared, resolved = match.groups()
            version = (resolved or declared).rstrip(",")
            if version in {"unspecified", "latest.release", "latest.integration"}:
                continue
            key = (group, artifact, version)
            coordinates[key] = {"group": group, "artifact": artifact, "version": version}
    return sorted(coordinates.values(), key=lambda item: (item["group"], item["artifact"], item["version"]))


async def _resolve_dependencies(project_path: str) -> tuple[list[dict[str, str]], str]:
    """Resolve the recommended module's UAT Debug runtime dependencies safely."""
    profile = await asyncio.to_thread(project_service.inspect_project, project_path)
    module = str(profile.get("recommended_module") or "").strip().replace("/", ":")
    task = f":{module}:dependencies" if module else "dependencies"
    command = ["./gradlew", task, "--configuration", "uatDebugRuntimeClasspath", "--console=plain"]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=180)
    except asyncio.TimeoutError:
        await _terminate_gradle(process)
        raise RuntimeError("Gradle dependency resolution timed out after 180 seconds.")
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_gradle(process))
        raise
    text = output.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"Gradle dependency resolution failed: {text[-4000:]}")
    dependencies = parse_gradle_dependencies(text)
    if not dependencies:
        raise RuntimeError("Gradle returned no resolved Maven dependencies for uatDebugRuntimeClasspath.")
    return dependencies, task


def _vulnerability_summary(vulnerability: dict[str, Any]) -> dict[str, Any]:
    """Keep the actionable OSV fields while bounding untrusted response data."""
    aliases = [str(value)[:100] for value in vulnerability.get("aliases", [])[:10]]
    database_specific = vulnerability.get("database_specific")
    severity = (
        database_specific.get("severity")
        if isinstance(database_specific, dict)
        else None
    )
    if not severity:
        severity_entries = vulnerability.get("severity", [])
        severity = (
            severity_entries[0].get("score")
            if severity_entries and isinstance(severity_entries[0], dict)
            else "UNKNOWN"
        )
    return {
        "id": str(vulnerability.get("id", "UNKNOWN"))[:100],
        "aliases": aliases,
        "severity": str(severity or "UNKNOWN")[:100],
        "summary": str(vulnerability.get("summary") or "No summary supplied.")[:500],
    }


async def scan_gradle_project(project_path: str) -> dict[str, Any]:
    """Resolve UAT dependencies and fail when OSV reports a vulnerability."""
    try:
        dependencies, task = await _resolve_dependencies(project_path)
        queries = [
            {
                "package": {
                    "ecosystem": "Maven",
                    "name": f"{item['group']}:{item['artifact']}",
                },
                "version": item["version"],
            }
            for item in dependencies
        ]
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(OSV_BATCH_URL, json={"queries": queries})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OSV returned a non-object response.")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(dependencies):
            raise RuntimeError("OSV returned an incomplete dependency result set.")
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("vulns", []), list):
                raise RuntimeError("OSV returned an invalid vulnerability result.")
            if any(not isinstance(item, dict) for item in result.get("vulns", [])):
                raise RuntimeError("OSV returned an invalid vulnerability record.")
    except (OSError, RuntimeError, TypeError, ValueError, httpx.HTTPError) as exc:
        return {
            "status": "FAILURE",
            "error_code": "DEPENDENCY_SCAN_INCOMPLETE",
            "error_output": str(exc),
        }

    findings = []
    for dependency, result in zip(dependencies, results):
        for vulnerability in result.get("vulns", []):
            findings.append(
                {
                    "dependency": dependency,
                    "vulnerability": _vulnerability_summary(vulnerability),
                }
            )
    if findings:
        return {
            "status": "FAILURE",
            "error_code": "VULNERABLE_DEPENDENCIES",
            "dependency_count": len(dependencies),
            "finding_count": len(findings),
            "findings": findings[:100],
            "message": "Update or remove vulnerable dependencies before review or integration.",
        }
    return {
        "status": "SUCCESS",
        "dependency_count": len(dependencies),
        "configuration": "uatDebugRuntimeClasspath",
        "gradle_task": task,
        "findings": [],
    }
