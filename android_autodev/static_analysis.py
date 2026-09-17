"""Discovery helpers for project-provided Detekt and ktlint Gradle tasks."""

from __future__ import annotations

import asyncio
import os
import re
import signal


_TASK_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s+-\s+", re.MULTILINE)


async def _terminate_gradle(process: asyncio.subprocess.Process) -> None:
    """Terminate task discovery and every Gradle child on timeout."""
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


async def discover_tasks(project_path: str, module: str) -> dict[str, str]:
    """Discover module analysis tasks without accepting arbitrary command input."""
    normalized_module = module.strip().replace("/", ":")
    task = f":{normalized_module}:tasks" if normalized_module else "tasks"
    process = await asyncio.create_subprocess_exec(
        "./gradlew",
        task,
        "--all",
        "--console=plain",
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
    except asyncio.TimeoutError:
        await _terminate_gradle(process)
        raise RuntimeError("Gradle task discovery timed out after 90 seconds.")
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_gradle(process))
        raise
    text = output.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"Gradle task discovery failed: {text[-4000:]}")

    names = set(_TASK_RE.findall(text))
    prefix = f":{normalized_module}:" if normalized_module else ""
    detekt = next((name for name in ("detektUatDebug", "detekt") if name in names), "")
    ktlint = next((name for name in ("ktlintUatDebugCheck", "ktlintCheck") if name in names), "")
    return {
        "detekt": f"{prefix}{detekt}" if detekt else "",
        "ktlint": f"{prefix}{ktlint}" if ktlint else "",
    }


def configured_plugins(project_path: str) -> dict[str, bool]:
    """Lightweight doctor check for declared Detekt and ktlint plugins."""
    corpus = []
    for directory, names, files in os.walk(project_path):
        names[:] = [name for name in names if name not in {".git", ".gradle", "build"}]
        for name in files:
            if name in {"build.gradle", "build.gradle.kts", "libs.versions.toml"}:
                try:
                    with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as handle:
                        corpus.append(handle.read(200_000).lower())
                except OSError:
                    continue
    joined = "\n".join(corpus)
    return {"detekt": "detekt" in joined, "ktlint": "ktlint" in joined}
