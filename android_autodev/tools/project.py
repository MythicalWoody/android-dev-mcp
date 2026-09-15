"""Developer diagnostics and Android project discovery tools."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import shutil

from .. import project as project_service
from .. import runtime
from ._registration import register_tools


async def inspect_android_project(project_path: str, save_profile: bool = False) -> dict:
    """Inspect project conventions before generating builds, mocks, or tests."""
    try:
        project = runtime.validate_path(project_path, "project_path")
        profile = await asyncio.to_thread(project_service.inspect_project, project)
        profile_path = (
            await asyncio.to_thread(project_service.write_profile, project, profile)
            if save_profile
            else None
        )
    except (OSError, ValueError) as exc:
        return {"status": "FAILURE", "error_code": "PROJECT_INSPECTION_FAILED", "error_output": str(exc)}
    return {
        "status": "SUCCESS",
        "profile": profile,
        "profile_path": profile_path,
        "warnings": (
            []
            if any(module.get("has_uat_debug") for module in profile["modules"])
            else ["No UAT product flavor was detected; routine integration builds must stop for user guidance."]
        ),
    }


async def _version(command: list[str]) -> dict:
    binary = shutil.which(command[0])
    if not binary:
        return {"available": False, "path": None, "version": None}
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *command[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        return {
            "available": process.returncode == 0,
            "path": binary,
            "version": output.decode(errors="replace").strip()[:500],
        }
    except (OSError, asyncio.TimeoutError) as exc:
        return {"available": False, "path": binary, "version": None, "error": str(exc)}


def _installed_python_versions() -> dict[str, str]:
    """Report server dependency versions without failing on optional metadata names."""
    versions = {}
    for package in ("mcp", "httpx", "Pillow", "Appium-Python-Client", "pytest"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "missing"
    return versions


async def doctor(project_path: str = "") -> dict:
    """Check the complete local toolchain and optional Android project setup."""
    checks = {}
    for name, command in (
        ("java", ["java", "-version"]),
        ("adb", ["adb", "version"]),
        ("appium", ["appium", "--version"]),
        ("node", ["node", "--version"]),
    ):
        checks[name] = await _version(command)

    checks["android_sdk"] = {
        "available": bool(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")),
        "path": os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT"),
    }
    driver_check = await _version(["appium", "driver", "list", "--installed", "--json"])
    driver_check["available"] = bool(
        driver_check.get("available") and "uiautomator2" in str(driver_check.get("version", "")).lower()
    )
    checks["appium_uiautomator2_driver"] = driver_check
    checks["python_dependencies"] = {
        "available": all(value != "missing" for value in _installed_python_versions().values()),
        "versions": _installed_python_versions(),
    }
    disk = shutil.disk_usage(runtime.LOG_DIR)
    checks["state_disk_space"] = {
        "available": disk.free >= 1024 * 1024 * 1024,
        "free_bytes": disk.free,
        "path": runtime.LOG_DIR,
    }
    checks["figma_token"] = {
        "available": bool(os.environ.get("FIGMA_ACCESS_TOKEN")),
        "required": False,
        "message": "Optional when Figma references are supplied through another MCP connector.",
    }

    project_profile = None
    if project_path:
        inspected = await inspect_android_project(project_path)
        if inspected["status"] != "SUCCESS":
            return inspected
        project_profile = inspected["profile"]
        project = project_profile["project_path"]
        checks["gradle_wrapper"] = {
            "available": os.path.isfile(os.path.join(project, "gradlew")),
            "path": os.path.join(project, "gradlew"),
        }

    missing = [
        name
        for name, check in checks.items()
        if check.get("required", True) and not check.get("available")
    ]
    return {
        "status": "READY" if not missing else "NEEDS_ATTENTION",
        "checks": checks,
        "missing_or_unconfigured": missing,
        "project_profile": project_profile,
    }


TOOLS = (inspect_android_project, doctor)


def register(mcp) -> None:
    """Register project-inspection tools with the MCP application."""
    register_tools(mcp, TOOLS)
