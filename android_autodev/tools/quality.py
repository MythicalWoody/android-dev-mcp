"""High-level quality gate and diagnostic artifact collection tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from .. import project as project_service
from .. import runtime, workflows
from ..security import atomic_write_text, safe_join, validate_package_name
from ._registration import register_tools


async def run_quality_gate(
    project_path: str,
    workflow_id: str,
    include_connected_tests: bool = False,
) -> dict:
    """Run the standard UAT Debug lint, unit-test, build, and optional device gates."""
    try:
        project = runtime.validate_path(project_path, "project_path")
        profile = await asyncio.to_thread(project_service.inspect_project, project)
        module = str(profile.get("recommended_module") or "").replace("/", ":")
        prefix = f":{module}:" if module else ""
    except (OSError, ValueError) as exc:
        return {"status": "FAILURE", "error_code": "QUALITY_GATE_SETUP_FAILED", "error_output": str(exc)}
    tasks = [f"{prefix}lintUatDebug", f"{prefix}testUatDebugUnitTest", f"{prefix}assembleUatDebug"]
    if include_connected_tests:
        tasks.append(f"{prefix}connectedUatDebugAndroidTest")
    results = []
    for task in tasks:
        result = await runtime.run_gradle(task, project_path, workflow_id=workflow_id)
        results.append({"task": task, "result": result})
        if result.get("status") != "SUCCESS":
            return {
                "status": "FAILURE",
                "error_code": "QUALITY_GATE_FAILED",
                "failed_task": task,
                "results": results,
            }
    return {"status": "SUCCESS", "workflow_id": workflow_id, "results": results}


async def _capture_command(path: str, *command: str) -> dict:
    """Capture a bounded diagnostic command without invoking a shell."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        atomic_write_text(path, output.decode(errors="replace")[-2_000_000:], mode=0o600)
        return {"command": list(command), "exit_code": process.returncode, "path": path}
    except (OSError, asyncio.TimeoutError) as exc:
        atomic_write_text(path, f"Diagnostic command failed: {exc}\n", mode=0o600)
        return {"command": list(command), "exit_code": None, "path": path, "error": str(exc)}


async def collect_failure_bundle(
    project_path: str,
    workflow_id: str,
    package_name: str,
    device_serial: str = "",
) -> dict:
    """Collect logcat, device, package, and workflow state into one private bundle."""
    try:
        project = runtime.validate_path(project_path, "project_path")
        package = validate_package_name(package_name)
        _workflow_path, workflow = workflows.load_workflow(
            runtime.LOG_DIR, workflow_id, project
        )
        serial = await runtime._resolve_adb_device(
            device_serial or str(workflow.get("device_serial") or "")
        )
        workflows.acquire_lease(runtime.LOG_DIR, workflow_id, "device", serial)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"status": "FAILURE", "error_code": "DIAGNOSTIC_SETUP_FAILED", "error_output": str(exc)}

    key = hashlib.sha256(workflow_id.encode()).hexdigest()[:16]
    bundle_dir = safe_join(
        project,
        "test-artifacts",
        "android-autodev",
        key,
        "failure-bundle",
        label="failure bundle",
    )
    os.makedirs(bundle_dir, mode=0o700, exist_ok=True)
    captures = await asyncio.gather(
        _capture_command(safe_join(bundle_dir, "logcat.txt"), "adb", "-s", serial, "logcat", "-d", "-t", "3000"),
        _capture_command(safe_join(bundle_dir, "device.txt"), "adb", "-s", serial, "shell", "getprop"),
        _capture_command(safe_join(bundle_dir, "package.txt"), "adb", "-s", serial, "shell", "dumpsys", "package", package),
    )
    summary_path = safe_join(bundle_dir, "summary.json")
    atomic_write_text(
        summary_path,
        json.dumps({"workflow": workflow, "captures": captures}, indent=2),
        mode=0o600,
    )
    return {
        "status": "SUCCESS",
        "workflow_id": workflow_id,
        "device_serial": serial,
        "bundle_dir": bundle_dir,
        "summary_path": summary_path,
        "captures": captures,
    }


TOOLS = (run_quality_gate, collect_failure_bundle)


def register(mcp) -> None:
    """Register high-level developer-experience tools."""
    register_tools(mcp, TOOLS)
