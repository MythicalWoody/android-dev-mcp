"""Android project discovery and portable project-profile generation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .security import atomic_write_text, safe_join


ANDROID_NAME = "{http://schemas.android.com/apk/res/android}name"


def _read_text(path: Path) -> str:
    """Read a bounded UTF-8-ish project file for static discovery."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _settings_modules(project: Path) -> list[str]:
    """Extract Gradle module names without executing project-controlled code."""
    settings = next(
        (candidate for candidate in (project / "settings.gradle.kts", project / "settings.gradle") if candidate.is_file()),
        None,
    )
    if not settings:
        return []
    modules: list[str] = []
    for match in re.finditer(r"(?:include\s*\(|include\s+)([^\n)]+)", _read_text(settings)):
        modules.extend(re.findall(r"['\"]:([^'\"]+)['\"]", match.group(1)))
    return list(dict.fromkeys(module.replace(":", "/") for module in modules))


def _manifest_launcher(module_dir: Path) -> tuple[str | None, str | None]:
    """Find package and launcher activity from the main manifest when available."""
    manifest_path = module_dir / "src" / "main" / "AndroidManifest.xml"
    if not manifest_path.is_file():
        return None, None
    try:
        root = ElementTree.parse(manifest_path).getroot()
    except (OSError, ElementTree.ParseError):
        return None, None
    package = root.attrib.get("package")
    for activity in root.findall("./application/activity") + root.findall("./application/activity-alias"):
        for intent in activity.findall("intent-filter"):
            actions = {node.attrib.get(ANDROID_NAME) for node in intent.findall("action")}
            categories = {node.attrib.get(ANDROID_NAME) for node in intent.findall("category")}
            if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories:
                return package, activity.attrib.get(ANDROID_NAME)
    return package, None


def _product_flavors(content: str) -> list[str]:
    """Extract flavor declarations only from the productFlavors block."""
    opening = re.search(r"\bproductFlavors\s*\{", content)
    if not opening:
        return []
    depth = 1
    index = opening.end()
    while index < len(content) and depth:
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
        index += 1
    if depth:
        return []
    body = content[opening.end() : index - 1]
    kotlin_names = re.findall(r"\bcreate\s*\(\s*[\"']([^\"']+)[\"']\s*\)", body)
    groovy_names = re.findall(r"^\s*([a-z][A-Za-z0-9_]*)\s*\{", body, re.MULTILINE)
    return list(dict.fromkeys(kotlin_names + groovy_names))


def inspect_project(project_path: str) -> dict[str, Any]:
    """Discover Android structure, variants, IDs, and common integration libraries."""
    project = Path(project_path)
    modules = _settings_modules(project)
    if not modules:
        modules = ["app"] if (project / "app").is_dir() else []

    module_profiles: list[dict[str, Any]] = []
    all_files_text: list[str] = []
    for module in modules:
        module_dir = project / module
        build_file = next(
            (candidate for candidate in (module_dir / "build.gradle.kts", module_dir / "build.gradle") if candidate.is_file()),
            None,
        )
        if not build_file:
            continue
        content = _read_text(build_file)
        all_files_text.append(content)
        namespace_match = re.search(r"\bnamespace\s*(?:=\s*)?[\"']([^\"']+)", content)
        application_match = re.search(r"\bapplicationId\s*(?:=\s*)?[\"']([^\"']+)", content)
        flattened_flavors = _product_flavors(content)
        package, launcher = _manifest_launcher(module_dir)
        module_profiles.append(
            {
                "name": module,
                "build_file": str(build_file.relative_to(project)),
                "dsl": "kotlin" if build_file.suffix == ".kts" else "groovy",
                "namespace": namespace_match.group(1) if namespace_match else package,
                "application_id": application_match.group(1) if application_match else package,
                "launcher_activity": launcher,
                "product_flavors": flattened_flavors,
                "has_uat_debug": any(flavor.lower() == "uat" for flavor in flattened_flavors),
            }
        )

    searchable_extensions = {".gradle", ".kts", ".kt", ".java", ".toml"}
    for path in project.rglob("*"):
        if path.is_file() and path.suffix in searchable_extensions and "build" not in path.parts:
            all_files_text.append(_read_text(path)[:100_000])
    corpus = "\n".join(all_files_text).lower()
    integrations = {
        "okhttp": "okhttp" in corpus,
        "retrofit": "retrofit" in corpus,
        "hilt": "hilt" in corpus or "dagger.hilt" in corpus,
        "dagger": "dagger" in corpus,
        "koin": "koin" in corpus,
        "compose": "androidx.compose" in corpus,
        "version_catalog": (project / "gradle" / "libs.versions.toml").is_file(),
    }
    return {
        "project_path": str(project),
        "gradle_wrapper": (project / "gradlew").is_file(),
        "modules": module_profiles,
        "integrations": integrations,
        "recommended_module": next(
            (profile["name"] for profile in module_profiles if profile.get("application_id")),
            module_profiles[0]["name"] if module_profiles else None,
        ),
    }


def profile_as_toml(profile: dict[str, Any]) -> str:
    """Render the stable subset of discovery data as a dependency-free TOML profile."""
    lines = [
        "# Generated by Android AutoDev MCP. Review before committing.",
        "schema_version = 1",
        f'default_variant = "UatDebug"',
        f'recommended_module = "{profile.get("recommended_module") or ""}"',
        "",
    ]
    for module in profile.get("modules", []):
        lines.extend(
            [
                "[[modules]]",
                f'name = "{module["name"]}"',
                f'build_file = "{module["build_file"]}"',
                f'dsl = "{module["dsl"]}"',
                f'application_id = "{module.get("application_id") or ""}"',
                f'launcher_activity = "{module.get("launcher_activity") or ""}"',
                f'has_uat_debug = {str(bool(module.get("has_uat_debug"))).lower()}',
                "",
            ]
        )
    return "\n".join(lines)


def write_profile(project_path: str, profile: dict[str, Any]) -> str:
    """Write a portable project profile inside the inspected project."""
    path = safe_join(project_path, ".android-autodev.toml", label="profile path")
    atomic_write_text(path, profile_as_toml(profile), mode=0o644)
    return path
