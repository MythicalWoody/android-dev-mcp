"""Workflow lifecycle and explicit non-UAT environment authorization tools."""

from .. import runtime
from .. import workflows
from ._registration import register_tools


async def start_workflow(project_path: str, purpose: str) -> dict:
    """Start a resumable, isolated Android integration workflow."""
    try:
        project = runtime.validate_path(project_path, "project_path")
        if not purpose or not purpose.strip():
            raise ValueError("purpose is required.")
        workflow_id, record = workflows.create_workflow(
            runtime.LOG_DIR, project, purpose.strip()
        )
        workflows.acquire_lease(runtime.LOG_DIR, workflow_id, "project", project)
    except (OSError, ValueError) as exc:
        return {"status": "FAILURE", "error_code": "WORKFLOW_START_FAILED", "error_output": str(exc)}
    return {
        "status": "SUCCESS",
        "workflow_id": workflow_id,
        "project_path": project,
        "default_variant": record["build_variant"],
        "expires_in_seconds": workflows.WORKFLOW_TTL_SECONDS,
        "message": "Use this workflow_id for API selection, builds, device work, Appium, and cleanup.",
    }


async def get_workflow_status(project_path: str, workflow_id: str) -> dict:
    """Return durable progress and ownership information for a workflow."""
    try:
        project = runtime.validate_path(project_path, "project_path")
        _path, record = workflows.load_workflow(
            runtime.LOG_DIR, workflow_id, project, allow_closed=True
        )
    except (OSError, ValueError) as exc:
        return {"status": "FAILURE", "error_code": "WORKFLOW_NOT_FOUND", "error_output": str(exc)}
    return {"status": "SUCCESS", "workflow": record}


async def authorize_environment(
    project_path: str,
    workflow_id: str,
    build_variant: str,
    user_confirmed: bool = False,
) -> dict:
    """Authorize one explicitly requested build variant other than UAT Debug."""
    if not user_confirmed:
        return {
            "status": "NEEDS_USER_CONFIRMATION",
            "message": "Ask the user to explicitly name the non-UAT variant in the current chat.",
        }
    try:
        project = runtime.validate_path(project_path, "project_path")
        variant = (build_variant or "").strip()
        if not variant or not variant[0].isupper() or not variant.isalnum():
            raise ValueError("build_variant must be a Gradle variant such as StagingDebug.")
        token = workflows.issue_environment_authorization(
            runtime.LOG_DIR, workflow_id, project, variant
        )
    except (OSError, ValueError) as exc:
        return {"status": "FAILURE", "error_code": "ENVIRONMENT_AUTH_FAILED", "error_output": str(exc)}
    return {
        "status": "AUTHORIZED",
        "build_variant": variant,
        "environment_authorization_token": token,
        "expires_in_seconds": workflows.ENVIRONMENT_TOKEN_TTL_SECONDS,
        "message": "Pass this one-time token to run_gradle for the exact authorized variant.",
    }


async def cancel_workflow(project_path: str, workflow_id: str) -> dict:
    """Close a workflow and release only resources owned by that workflow."""
    try:
        project = runtime.validate_path(project_path, "project_path")
        path, record = workflows.load_workflow(runtime.LOG_DIR, workflow_id, project)
        owned_appium = runtime._appium_servers.pop(workflow_id, None)
        if owned_appium:
            process, port = owned_appium
            if process.returncode is None:
                await runtime._terminate_process_group(process)
            workflows.release_lease(runtime.LOG_DIR, workflow_id, "appium-port", str(port))
        manifest = runtime._load_mock_manifest(project)
        if manifest and manifest.get("workflow_id") == workflow_id:
            cleanup = await runtime.deactivate_mock_environment(project, workflow_id)
            if cleanup.get("status") == "FAILURE":
                return cleanup
        device = record.get("device_serial")
        if device:
            workflows.release_lease(runtime.LOG_DIR, workflow_id, "device", str(device))
        workflows.release_lease(runtime.LOG_DIR, workflow_id, "project", project)
        workflows.update_workflow(path, record, state="cancelled")
    except (OSError, ValueError) as exc:
        return {"status": "FAILURE", "error_code": "WORKFLOW_CANCEL_FAILED", "error_output": str(exc)}
    return {"status": "CANCELLED", "workflow_id": workflow_id}


TOOLS = (start_workflow, get_workflow_status, authorize_environment, cancel_workflow)


def register(mcp) -> None:
    """Register workflow tools with the MCP application."""
    register_tools(mcp, TOOLS)
