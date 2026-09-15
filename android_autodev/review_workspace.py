"""Secure project snapshotting for pre-approval proposal compilation."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from .security import private_makedirs, safe_join


MAX_WORKSPACE_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "local.properties",
    "key.properties",
    "*.jks",
    "*.keystore",
    "*.p12",
    "*.pfx",
    "*credentials*.json",
    "*service-account*.json",
)
IGNORED_PARTS = {
    ".git",
    ".gradle",
    ".kotlin",
    "build",
    ".idea",
    "test-artifacts",
    ".android-auto-review",
    "android-auto-review-copy",
}


def _is_sensitive(relative_path: str) -> bool:
    name = os.path.basename(relative_path)
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_PATTERNS)


def _tracked_files(project_path: str) -> list[str] | None:
    """Return Git-tracked files, or None when the source is not a Git worktree."""
    result = subprocess.run(
        ["git", "-C", project_path, "ls-files", "-z"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    return [item.decode(errors="surrogateescape") for item in result.stdout.split(b"\0") if item]


def _fallback_files(project_path: str) -> list[str]:
    """Enumerate a safe source tree for non-Git fixtures and legacy projects."""
    root = Path(project_path)
    files = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = [
            name
            for name in names
            if name not in IGNORED_PARTS and not os.path.islink(os.path.join(directory, name))
        ]
        for name in filenames:
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if not path.is_symlink() and not _is_sensitive(relative):
                files.append(relative)
    return files


def _expand_explicit(project_path: str, entries: list[str]) -> list[str]:
    """Expand explicitly requested untracked files while enforcing containment."""
    root = Path(project_path)
    expanded = []
    for entry in entries:
        candidate = Path(safe_join(project_path, entry, label="include_untracked_paths"))
        paths = candidate.rglob("*") if candidate.is_dir() else (candidate,)
        for path in paths:
            if path.is_file() and not path.is_symlink():
                relative = str(path.relative_to(root))
                if _is_sensitive(relative):
                    raise ValueError(f"Sensitive file cannot be copied to a review workspace: {relative}")
                expanded.append(relative)
    return expanded


def copy_project_snapshot(
    project_path: str,
    workspace_path: str,
    include_untracked_paths: list[str] | None = None,
) -> dict:
    """Copy tracked and explicitly selected safe files into a private workspace."""
    tracked = _tracked_files(project_path)
    files = _fallback_files(project_path) if tracked is None else tracked
    files.extend(_expand_explicit(project_path, include_untracked_paths or []))
    files = list(dict.fromkeys(files))

    total = 0
    accepted = []
    for relative in files:
        if _is_sensitive(relative) or any(part in IGNORED_PARTS for part in Path(relative).parts):
            continue
        source = safe_join(project_path, relative, label="snapshot source")
        if os.path.islink(source) or not os.path.isfile(source):
            continue
        total += os.path.getsize(source)
        if total > MAX_WORKSPACE_BYTES:
            raise ValueError(f"Review workspace exceeds the {MAX_WORKSPACE_BYTES} byte quota.")
        accepted.append((relative, source))

    if shutil.disk_usage(os.path.dirname(workspace_path)).free - total < MIN_FREE_BYTES:
        raise ValueError("Insufficient free disk space for a safe review workspace.")

    private_makedirs(workspace_path)
    for relative, source in accepted:
        destination = safe_join(workspace_path, relative, label="snapshot destination")
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    return {
        "file_count": len(accepted),
        "total_bytes": total,
        "source_mode": "safe-tree" if tracked is None else "git-tracked-plus-explicit",
    }
