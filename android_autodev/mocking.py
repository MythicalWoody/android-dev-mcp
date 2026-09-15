"""Project-aware temporary mock integration planning.

This module deliberately fails when it cannot identify a safe Gradle or OkHttp
insertion point.  It never falls back to application-specific package names or
anchors from a different Android project.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .project import inspect_project
from .security import safe_join, validate_package_name


GRADLE_BEGIN = "// ANDROID_AUTODEV_MOCK_BEGIN"
GRADLE_END = "// ANDROID_AUTODEV_MOCK_END"
NETWORK_BEGIN = "// ANDROID_AUTODEV_NETWORK_BEGIN"
NETWORK_END = "// ANDROID_AUTODEV_NETWORK_END"


def _discover_network_module(project: Path, explicit_path: str) -> Path:
    """Resolve an explicit path or one unambiguous conventional network module."""
    if explicit_path:
        resolved = Path(safe_join(str(project), explicit_path, label="network_module_path"))
        if not resolved.is_file():
            raise ValueError(f"Network module does not exist: {explicit_path}")
        return resolved
    candidates = [
        path
        for path in project.rglob("*.kt")
        if path.name.lower() in {"networkmodule.kt", "networkprovidermodule.kt", "httpmodule.kt"}
        and "build" not in path.parts
    ]
    if len(candidates) != 1:
        relative = [str(path.relative_to(project)) for path in candidates]
        raise ValueError(
            "Could not identify exactly one network module. Pass network_module_path explicitly. "
            f"Candidates: {relative or 'none'}"
        )
    return candidates[0]


def _flavor_dimension(content: str) -> str:
    """Infer the existing environment flavor dimension without inventing one."""
    patterns = (
        r"flavorDimensions\s*(?:\+=|=)?\s*[\[\(]?\s*[\"']([^\"']+)",
        r"dimension\s*(?:=\s*)?[\"']([^\"']+)[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return match.group(1)
    raise ValueError("No flavor dimension was detected; configure one before activating MockDebug.")


def _insert_inside_block(content: str, block_name: str, insertion: str) -> str:
    """Insert directly after a named Gradle block's opening brace."""
    match = re.search(rf"\b{re.escape(block_name)}\s*\{{", content)
    if not match:
        raise ValueError(f"Could not locate the Gradle {block_name} block.")
    position = match.end()
    return content[:position] + "\n" + insertion + content[position:]


def _render_gradle(content: str, kotlin_dsl: bool, dimension: str) -> str:
    if GRADLE_BEGIN in content:
        raise ValueError("Temporary mock Gradle wiring already exists.")
    if kotlin_dsl:
        insertion = (
            f"        {GRADLE_BEGIN}\n"
            f'        create("mock") {{\n            dimension = "{dimension}"\n        }}\n'
            f"        {GRADLE_END}\n"
        )
    else:
        insertion = (
            f"        {GRADLE_BEGIN}\n"
            f'        mock {{\n            dimension "{dimension}"\n        }}\n'
            f"        {GRADLE_END}\n"
        )
    return _insert_inside_block(content, "productFlavors", insertion)


def _render_network(content: str, package_name: str) -> str:
    """Wire the generated interceptor into one unambiguous OkHttp builder."""
    if NETWORK_BEGIN in content:
        raise ValueError("Temporary mock network wiring already exists.")
    builder_positions = [match.start() for match in re.finditer(r"OkHttpClient\s*\.\s*Builder\s*\(", content)]
    if len(builder_positions) != 1:
        raise ValueError(
            "Expected exactly one OkHttpClient.Builder in the selected network module; "
            "provide a dedicated module or wire the interceptor through a reviewed project change."
        )
    build_match = re.search(r"\.build\s*\(\s*\)", content[builder_positions[0] :])
    if not build_match:
        raise ValueError("Could not find .build() for the selected OkHttpClient.Builder.")
    position = builder_positions[0] + build_match.start()
    insertion = (
        f"\n            {NETWORK_BEGIN}\n"
        f"            .addInterceptor({package_name}.network.MockApiInterceptor())\n"
        f"            {NETWORK_END}\n            "
    )
    return content[:position] + insertion + content[position:]


def plan_integration(
    project_path: str,
    package_name: str,
    network_module_path: str = "",
) -> dict[str, Any]:
    """Return validated paths and updated source for a reversible MockDebug adapter."""
    package = validate_package_name(package_name)
    project = Path(project_path)
    profile = inspect_project(project_path)
    module_name = profile.get("recommended_module")
    module = next((item for item in profile["modules"] if item["name"] == module_name), None)
    if not module:
        raise ValueError("No Android application module was detected.")
    gradle_path = Path(safe_join(project_path, module["build_file"], label="module build file"))
    network_path = _discover_network_module(project, network_module_path)
    gradle_content = gradle_path.read_text(encoding="utf-8")
    network_content = network_path.read_text(encoding="utf-8")
    dimension = _flavor_dimension(gradle_content)
    mock_debug_dir = safe_join(
        project_path,
        module_name,
        "src",
        "mockDebug",
        label="mock source set",
    )
    return {
        "module": module_name,
        "gradle_path": str(gradle_path),
        "network_path": str(network_path),
        "mock_debug_dir": mock_debug_dir,
        "gradle_content": _render_gradle(gradle_content, module["dsl"] == "kotlin", dimension),
        "network_content": _render_network(network_content, package),
        "profile": profile,
    }
