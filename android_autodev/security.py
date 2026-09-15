"""Security primitives shared by every Android AutoDev domain.

The MCP accepts paths and identifiers supplied by an AI client.  Centralising
validation here keeps each tool from inventing subtly different traversal and
source-generation protections.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import fcntl


PACKAGE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
PYTHON_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FIGMA_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,200}$")
FIGMA_NODE_RE = re.compile(r"^[0-9]+:[0-9]+$")


def validate_package_name(value: str) -> str:
    """Accept only dotted Java/Kotlin package identifiers."""
    normalized = (value or "").strip()
    if not PACKAGE_NAME_RE.fullmatch(normalized):
        raise ValueError(f"Invalid Android package name: {value!r}.")
    return normalized


def validate_python_identifier(value: str, label: str = "identifier") -> str:
    """Reject filenames and class fragments that could escape or inject source."""
    normalized = (value or "").strip()
    if not PYTHON_IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"Invalid {label}: use letters, digits, and underscores only.")
    return normalized


def validate_activity_name(value: str) -> str:
    """Validate a fully-qualified or package-relative Android activity name."""
    normalized = (value or "").strip()
    candidate = normalized[1:] if normalized.startswith(".") else normalized
    if not candidate or not all(PYTHON_IDENTIFIER_RE.fullmatch(part) for part in candidate.split(".")):
        raise ValueError(f"Invalid Android activity name: {value!r}.")
    return normalized


def validate_figma_key(value: str) -> str:
    """Validate the opaque Figma file key before using it in URLs or paths."""
    normalized = (value or "").strip()
    if not FIGMA_KEY_RE.fullmatch(normalized):
        raise ValueError("Invalid Figma file key.")
    return normalized


def validate_figma_node_id(value: str) -> str:
    """Normalize a Figma node ID and reject path/control characters."""
    normalized = (value or "").strip().replace("-", ":")
    if not FIGMA_NODE_RE.fullmatch(normalized):
        raise ValueError("Invalid Figma node ID; expected a value such as '12:34'.")
    return normalized


def safe_join(root: str, *relative_parts: str, label: str = "path") -> str:
    """Join untrusted relative parts and prove the result remains below root."""
    canonical_root = os.path.realpath(root)
    for part in relative_parts:
        if not isinstance(part, str) or not part or os.path.isabs(part):
            raise ValueError(f"Rejected {label}: every component must be relative.")
    candidate = os.path.realpath(os.path.join(canonical_root, *relative_parts))
    try:
        contained = os.path.commonpath([canonical_root, candidate]) == canonical_root
    except ValueError:
        contained = False
    if not contained or candidate == canonical_root:
        raise ValueError(f"Rejected {label}: resolved path escapes its managed directory.")
    return candidate


def validate_descendant(root: str, path: str, label: str = "path") -> str:
    """Validate an already-composed path against a trusted containment root."""
    canonical_root = os.path.realpath(root)
    candidate = os.path.realpath(path)
    try:
        contained = os.path.commonpath([canonical_root, candidate]) == canonical_root
    except ValueError:
        contained = False
    if not contained or candidate == canonical_root:
        raise ValueError(f"Rejected {label}: path is outside its managed root.")
    return candidate


def kotlin_string_literal(value: str) -> str:
    """Encode arbitrary text as a safe ordinary Kotlin string literal."""
    encoded = json.dumps(str(value), ensure_ascii=False)
    return encoded.replace("$", "\\u0024")


def private_makedirs(path: str) -> None:
    """Create a process-private state directory and tighten existing modes."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write_text(path: str, content: str, mode: int = 0o600) -> None:
    """Atomically replace a text file with explicitly private permissions."""
    # File writers also serve generated project source, so never chmod an
    # existing parent directory. State owners create their private roots first.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        prefix=".android-autodev-", dir=os.path.dirname(path), text=True
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def file_lock(path: str) -> Iterator[None]:
    """Serialize cross-process mutations to one state resource."""
    lock_path = f"{path}.lock"
    private_makedirs(os.path.dirname(lock_path))
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        # fdopen owns and closes the descriptor on the normal and exceptional paths.
        pass


def load_json(path: str) -> dict[str, Any] | None:
    """Load a JSON object when present and reject non-object state."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"State file does not contain an object: {path}")
    return value


def store_json(path: str, value: dict[str, Any]) -> None:
    """Persist structured state privately and atomically."""
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True))
