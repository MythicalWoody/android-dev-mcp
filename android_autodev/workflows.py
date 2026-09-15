"""Durable workflow state and cross-process resource leases.

Every integration run gets an explicit identity.  State that used to be shared
through module globals can therefore be resumed safely and cannot leak between
projects, devices, or concurrent MCP clients.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
from datetime import datetime, timezone
from time import time
from typing import Any

from .security import file_lock, load_json, private_makedirs, store_json


WORKFLOW_TTL_SECONDS = 24 * 60 * 60
ENVIRONMENT_TOKEN_TTL_SECONDS = 30 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workflow_dir(log_dir: str) -> str:
    return os.path.join(log_dir, "workflows")


def workflow_path(log_dir: str, workflow_id: str) -> str:
    """Resolve a workflow record without allowing IDs to become path input."""
    key = hashlib.sha256((workflow_id or "").encode()).hexdigest()
    return os.path.join(_workflow_dir(log_dir), f"{key}.json")


def create_workflow(log_dir: str, project_path: str, purpose: str) -> tuple[str, dict[str, Any]]:
    """Create a private, resumable Android development workflow."""
    workflow_id = secrets.token_urlsafe(24)
    now = time()
    record: dict[str, Any] = {
        "workflow_id": workflow_id,
        "project_path": project_path,
        "purpose": purpose,
        "state": "active",
        "api_mode": None,
        "build_variant": "UatDebug",
        "device_serial": None,
        "appium_port": None,
        "retry_counters": {},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "expires_at_epoch": now + WORKFLOW_TTL_SECONDS,
    }
    path = workflow_path(log_dir, workflow_id)
    private_makedirs(os.path.dirname(path))
    store_json(path, record)
    return workflow_id, record


def load_workflow(
    log_dir: str,
    workflow_id: str,
    project_path: str | None = None,
    *,
    allow_closed: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Load and validate an active workflow, optionally binding it to a project."""
    if not workflow_id:
        raise ValueError("workflow_id is required; call start_workflow first.")
    path = workflow_path(log_dir, workflow_id)
    record = load_json(path)
    if not record or not secrets.compare_digest(str(record.get("workflow_id", "")), workflow_id):
        raise ValueError("Unknown workflow_id; call start_workflow first.")
    if project_path and record.get("project_path") != project_path:
        raise ValueError("The workflow belongs to a different Android project.")
    if time() > float(record.get("expires_at_epoch", 0)):
        raise ValueError("The workflow expired; start a new workflow.")
    if not allow_closed and record.get("state") != "active":
        raise ValueError(f"The workflow is {record.get('state', 'closed')}.")
    return path, record


def update_workflow(path: str, record: dict[str, Any], **updates: Any) -> dict[str, Any]:
    """Apply a locked, atomic update while preserving newer concurrent fields."""
    with file_lock(path):
        current = load_json(path)
        if not current:
            raise ValueError("Workflow state disappeared during update.")
        current.update(updates)
        current["updated_at"] = _now_iso()
        store_json(path, current)
        record.clear()
        record.update(current)
    return record


def issue_environment_authorization(
    log_dir: str,
    workflow_id: str,
    project_path: str,
    variant: str,
) -> str:
    """Bind a short-lived exception token to one workflow and exact variant."""
    path, record = load_workflow(log_dir, workflow_id, project_path)
    token = secrets.token_urlsafe(32)
    update_workflow(
        path,
        record,
        environment_authorization={
            "variant": variant,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at_epoch": time() + ENVIRONMENT_TOKEN_TTL_SECONDS,
            "issued_at": _now_iso(),
        },
    )
    return token


def consume_environment_authorization(
    log_dir: str,
    workflow_id: str,
    project_path: str,
    variant: str,
    token: str,
) -> None:
    """Atomically consume the exact non-UAT variant authorization once."""
    path, _record = load_workflow(log_dir, workflow_id, project_path)
    with file_lock(path):
        record = load_json(path)
        if not record:
            raise ValueError("Workflow state disappeared.")
        authorization = record.get("environment_authorization") or {}
        expected = str(authorization.get("token_sha256", ""))
        actual = hashlib.sha256((token or "").encode()).hexdigest()
        if not expected or not secrets.compare_digest(expected, actual):
            raise ValueError("Invalid environment authorization token.")
        if authorization.get("variant") != variant:
            raise ValueError("The authorization token belongs to a different build variant.")
        if time() > float(authorization.get("expires_at_epoch", 0)):
            raise ValueError("The environment authorization token expired.")
        record["environment_authorization"] = None
        record["build_variant"] = variant
        record["updated_at"] = _now_iso()
        store_json(path, record)


def _lease_path(log_dir: str, resource_type: str, resource_id: str) -> str:
    key = hashlib.sha256(f"{resource_type}:{resource_id}".encode()).hexdigest()
    return os.path.join(log_dir, "leases", f"{key}.json")


def acquire_lease(
    log_dir: str,
    workflow_id: str,
    resource_type: str,
    resource_id: str,
    ttl_seconds: int = 15 * 60,
) -> None:
    """Acquire a renewable project/device/port lease for one workflow."""
    path = _lease_path(log_dir, resource_type, resource_id)
    with file_lock(path):
        current = load_json(path)
        if current and time() <= float(current.get("expires_at_epoch", 0)):
            if current.get("workflow_id") != workflow_id:
                raise ValueError(
                    f"{resource_type} '{resource_id}' is in use by another workflow."
                )
        store_json(
            path,
            {
                "workflow_id": workflow_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "expires_at_epoch": time() + ttl_seconds,
                "updated_at": _now_iso(),
            },
        )


def release_lease(log_dir: str, workflow_id: str, resource_type: str, resource_id: str) -> None:
    """Release a resource only when the caller owns its lease."""
    path = _lease_path(log_dir, resource_type, resource_id)
    with file_lock(path):
        current = load_json(path)
        if current and current.get("workflow_id") == workflow_id:
            os.remove(path)


def increment_retry(log_dir: str, workflow_id: str, gate: str, maximum: int) -> int:
    """Increment a workflow-local retry counter and fail after its limit."""
    path, _record = load_workflow(log_dir, workflow_id)
    with file_lock(path):
        record = load_json(path) or {}
        counters = dict(record.get("retry_counters") or {})
        count = int(counters.get(gate, 0)) + 1
        if count > maximum:
            raise ValueError(f"{gate} exceeded its {maximum}-attempt workflow limit.")
        counters[gate] = count
        record["retry_counters"] = counters
        record["updated_at"] = _now_iso()
        store_json(path, record)
        return count


def reset_retry(log_dir: str, workflow_id: str, gate: str) -> None:
    """Reset only the completed workflow's retry counter."""
    path, record = load_workflow(log_dir, workflow_id)
    counters = dict(record.get("retry_counters") or {})
    counters[gate] = 0
    update_workflow(path, record, retry_counters=counters)


def allocate_loopback_port() -> int:
    """Ask the OS for an unused loopback TCP port for an owned Appium server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])
