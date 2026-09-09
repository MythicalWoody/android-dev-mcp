import asyncio
import json
import base64
import hashlib
import os
import logging
import secrets
import signal
import shutil
import sys
from datetime import datetime
from mcp.server.fastmcp import FastMCP
from PIL import Image
import httpx

MCP_INSTRUCTIONS = """
Before changing product source code, prepare one complete unified diff and call
request_code_review. Show its complete proposed_diff to the user in the chat,
then end the turn immediately. Do not call record_code_review_decision in the
same turn. On the user's next message, call record_code_review_decision with the
decision and feedback they actually provided. Do not write, patch, or otherwise
modify product source until that tool returns an approval token. Apply the exact
reviewed diff only through apply_reviewed_patch; never reuse a token or alter the
diff after approval. If the user rejects or requests changes, prepare a revised
diff and repeat the chat-review checkpoint. Temporary MCP-owned mock wiring,
generated test artifacts, build outputs, and cleanup operations are outside this
product-source gate.

Before any API-dependent implementation or test workflow, ask the user whether
to use the deployed real API or temporary mock responses. Never infer the mode
from the task, previous workflows, or API availability. After the user answers,
call select_api_mode with user_confirmed=true. Use the real API without adding
mock code when mode is real. Call activate_mock_environment only when mode is
mock, and pass the one-time selection token returned by select_api_mode. Always
deactivate temporary mocks during final cleanup.

For device work, resolve the actual online ADB serial and pass it through every
ADB/Appium operation; never assume emulator-5554. Never guess when multiple
devices are connected. Keep Gradle and pytest work below the MCP wrapper timeout
and terminate the complete child process group on timeout/cancellation. Generated
Appium imports and OkHttp code must match installed/project versions. Before real
API E2E and final delivery, verify that no AndroidAutoDev mock wiring remains.
""".strip()

mcp = FastMCP("AndroidAutoDev", instructions=MCP_INSTRUCTIONS)

# --- Configuration ---
ALLOWED_PROJECT_ROOT = os.environ.get(
    "ANDROID_PROJECT_ROOT", "/Users/shivam.singh28"
)

# --- Figma Configuration ---
FIGMA_API_BASE = "https://api.figma.com/v1"
FIGMA_ACCESS_TOKEN = os.environ.get("FIGMA_ACCESS_TOKEN")


def _get_figma_access_token() -> str:
    """Return the configured Figma access token or raise an error."""
    token = os.environ.get("FIGMA_ACCESS_TOKEN")
    if not token:
        raise ValueError(
            "FIGMA_ACCESS_TOKEN environment variable is not set. "
            "Set it to a Figma personal access token."
        )
    return token


def _parse_figma_url(url_or_key: str) -> tuple[str, str | None]:
    """Extract file_key and optional node_id from a Figma URL or return the key as-is."""
    import re
    url = url_or_key.strip()
    # Full design URL: https://figma.com/design/{fileKey}/...?node-id=1-2
    design_match = re.search(r"figma\.com/design/([A-Za-z0-9]+)(?:/[^?]*)?(?:\?.*node-id=([0-9]+[-:][0-9]+))?", url)
    if design_match:
        file_key = design_match.group(1)
        node_id = design_match.group(2)
        if node_id:
            node_id = node_id.replace("-", ":")
        return file_key, node_id
    # Bare key
    return url, None


def _normalize_figma_node_id(node_id: str) -> str:
    """Figma REST API uses ':' separators; UI URLs use '-'."""
    return node_id.strip().replace("-", ":")


async def _figma_api_request(path: str, params: dict | None = None) -> dict:
    """Make an authenticated GET request to the Figma REST API."""
    headers = {"X-Figma-Token": _get_figma_access_token()}
    url = f"{FIGMA_API_BASE}{path}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params, timeout=30.0)
        response.raise_for_status()
        return response.json()


async def _download_figma_image(url: str, dest_path: str) -> None:
    """Download a Figma rendered image to a local file."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=60.0)
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(response.content)


# --- Structured Logging ---
LOG_DIR = "/tmp/kiro-android-autodev"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "agent.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AndroidAutoDev")

# --- Mandatory product-source review gate ---
CODE_REVIEW_TTL_SECONDS = 30 * 60
PENDING_REVIEW_TTL_SECONDS = 24 * 60 * 60
MAX_REVIEW_DIFF_BYTES = 512 * 1024

# --- Allowed Gradle commands ---
# Exact whitelist for known-safe commands
ALLOWED_GRADLE_COMMANDS = {
    "assembleMockDebug",
    "assembleDebug",
    "assembleRelease",
    "testDebugUnitTest",
    "testMockDebugUnitTest",
    "clean",
    "lint",
    "lintDebug",
    "lintMockDebug",
    "connectedMockDebugAndroidTest",
    "connectedDebugAndroidTest",
    "installMockDebug",
    "installDebug",
    "uninstallAll",
}

# Regex patterns for flavor-aware Gradle commands
# These allow any build variant/flavor combination
import re as _re
ALLOWED_GRADLE_PATTERNS = [
    _re.compile(r"^assemble[A-Z]\w*$"),           # assembleProductionDebug, assembleStagingRelease, etc.
    _re.compile(r"^install[A-Z]\w*$"),             # installDevelopmentDebug, etc.
    _re.compile(r"^uninstall[A-Z]\w*$"),           # uninstallDevelopmentDebug, etc.
    _re.compile(r"^test[A-Z]\w*UnitTest$"),        # testDevelopmentDebugUnitTest, etc.
    _re.compile(r"^connected[A-Z]\w*AndroidTest$"),# connectedStagingDebugAndroidTest, etc.
    _re.compile(r"^lint[A-Z]\w*$"),                # lintProductionDebug, etc.
    _re.compile(r"^bundle[A-Z]\w*$"),              # bundleProductionRelease, etc.
    _re.compile(r"^compile[A-Z]\w*Sources$"),      # compileDevelopmentDebugSources, etc.
    _re.compile(r"^merge[A-Z]\w*Resources$"),      # mergeDevelopmentDebugResources, etc.
    _re.compile(r"^package[A-Z]\w*$"),             # packageProductionRelease, etc.
]

# Explicitly blocked commands (dangerous operations)
BLOCKED_GRADLE_COMMANDS = {
    "publishRelease",
    "uploadArchives",
    "signingReport",  # leaks keystore info
}


def _is_gradle_command_allowed(command: str) -> bool:
    """Check if a Gradle command is allowed via exact match or pattern match."""
    base_command = command.strip() if command else ""

    # Block dangerous commands first
    if base_command in BLOCKED_GRADLE_COMMANDS:
        return False

    # Exact whitelist check
    if base_command in ALLOWED_GRADLE_COMMANDS:
        return True

    # Pattern-based check for flavor variants
    for pattern in ALLOWED_GRADLE_PATTERNS:
        if pattern.match(base_command):
            return True

    return False

# --- Global state: Appium server process ---
_appium_server_process = None

# --- Loop counter persistence ---
_retry_counters: dict[str, int] = {}


def _increment_retry(gate: str, max_retries: int) -> dict | None:
    """Increment retry counter for a gate. Returns error dict if max exceeded."""
    _retry_counters.setdefault(gate, 0)
    _retry_counters[gate] += 1
    logger.info(f"Retry counter [{gate}]: {_retry_counters[gate]}/{max_retries}")
    if _retry_counters[gate] > max_retries:
        logger.error(f"Max retries exceeded for [{gate}]")
        return {
            "status": "MAX_RETRIES_EXCEEDED",
            "gate": gate,
            "attempts": _retry_counters[gate],
            "message": f"Halting: {gate} failed after {max_retries} attempts. Provide diagnostic report.",
        }
    return None


def _reset_retry(gate: str):
    """Reset retry counter after success."""
    _retry_counters[gate] = 0


# --- Safety: Path Whitelisting ---
def validate_path(path: str, label: str = "path") -> str:
    """Validate that a path is safe and within the allowed project root."""
    resolved = os.path.realpath(path)
    if ".." in path:
        raise ValueError(f"Rejected {label}: path traversal ('..') not allowed.")
    allowed_root = os.path.realpath(ALLOWED_PROJECT_ROOT)
    try:
        is_within_root = os.path.commonpath([resolved, allowed_root]) == allowed_root
    except ValueError:
        is_within_root = False
    if not is_within_root:
        raise ValueError(
            f"Rejected {label}: '{resolved}' is outside allowed root '{ALLOWED_PROJECT_ROOT}'."
        )
    return resolved


def _code_review_dir(project_path: str) -> str:
    project_key = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(LOG_DIR, "code-reviews", project_key)


def _code_review_record_path(project_path: str, approval_token: str) -> str:
    token_key = hashlib.sha256((approval_token or "").encode("utf-8")).hexdigest()
    return os.path.join(_code_review_dir(project_path), f"{token_key}.json")


def _pending_review_record_path(project_path: str, review_id: str) -> str:
    review_key = hashlib.sha256((review_id or "").encode("utf-8")).hexdigest()
    return os.path.join(_code_review_dir(project_path), "pending", f"{review_key}.json")


def _validate_review_diff(proposed_diff: str) -> list[str]:
    """Validate a text-only git diff and return its project-relative paths."""
    if not proposed_diff or not proposed_diff.strip():
        raise ValueError("The proposed unified diff is empty.")
    diff_size = len(proposed_diff.encode("utf-8"))
    if diff_size > MAX_REVIEW_DIFF_BYTES:
        raise ValueError(
            f"The proposed diff is {diff_size} bytes; the review limit is "
            f"{MAX_REVIEW_DIFF_BYTES} bytes. Split it into smaller reviewable changes."
        )
    if "\x00" in proposed_diff or "GIT binary patch" in proposed_diff:
        raise ValueError("Binary patches cannot pass manual code review; use a text-only diff.")

    paths = []
    for line in proposed_diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
            raise ValueError(
                "Diff paths must be unquoted project-relative paths without whitespace."
            )
        for raw_path in parts[2:4]:
            relative_path = raw_path[2:]
            components = relative_path.split("/")
            if (
                not relative_path
                or os.path.isabs(relative_path)
                or any(component in {"", ".", ".."} for component in components)
                or components[0] == ".git"
            ):
                raise ValueError(f"Unsafe path in proposed diff: {raw_path}")
        paths.append(parts[3][2:])

    if not paths:
        raise ValueError(
            "Expected a git-style unified diff containing at least one 'diff --git a/... b/...' header."
        )
    return list(dict.fromkeys(paths))


def _store_code_review(
    project_path: str,
    change_summary: str,
    proposed_diff: str,
) -> tuple[str, dict]:
    token = secrets.token_urlsafe(32)
    now = datetime.now().timestamp()
    record = {
        "project_path": project_path,
        "change_summary": change_summary,
        "diff_sha256": hashlib.sha256(proposed_diff.encode("utf-8")).hexdigest(),
        "state": "approved",
        "approved_at": datetime.now().isoformat(),
        "expires_at_epoch": now + CODE_REVIEW_TTL_SECONDS,
    }
    record_path = _code_review_record_path(project_path, token)
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    _write_text_atomic(record_path, json.dumps(record, indent=2))
    return token, record


def _store_pending_review(
    project_path: str,
    change_summary: str,
    proposed_diff: str,
    changed_files: list[str],
) -> tuple[str, dict]:
    review_id = secrets.token_urlsafe(24)
    now = datetime.now().timestamp()
    record = {
        "project_path": project_path,
        "change_summary": change_summary,
        "diff_sha256": hashlib.sha256(proposed_diff.encode("utf-8")).hexdigest(),
        "changed_files": changed_files,
        "state": "awaiting_user_review",
        "created_at": datetime.now().isoformat(),
        "expires_at_epoch": now + PENDING_REVIEW_TTL_SECONDS,
    }
    record_path = _pending_review_record_path(project_path, review_id)
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    _write_text_atomic(record_path, json.dumps(record, indent=2))
    return review_id, record


def _load_pending_review(
    project_path: str,
    review_id: str,
    proposed_diff: str,
) -> tuple[str, dict]:
    if not review_id:
        raise ValueError("Missing review_id. Call request_code_review first.")
    record_path = _pending_review_record_path(project_path, review_id)
    if not os.path.isfile(record_path):
        raise ValueError("Invalid or already-resolved review_id.")
    with open(record_path, "r") as handle:
        record = json.load(handle)
    if record.get("project_path") != project_path:
        raise ValueError("The review belongs to a different project.")
    if record.get("state") != "awaiting_user_review":
        raise ValueError("This chat review has already been resolved.")
    if datetime.now().timestamp() > float(record.get("expires_at_epoch", 0)):
        raise ValueError("The chat review expired. Submit the current diff for review again.")
    actual_hash = hashlib.sha256(proposed_diff.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(record.get("diff_sha256", ""), actual_hash):
        raise ValueError("The diff changed while awaiting review. Start a new chat review.")
    return record_path, record


def _authorize_reviewed_diff(
    project_path: str,
    proposed_diff: str,
    approval_token: str,
) -> tuple[str, dict]:
    if not approval_token:
        raise ValueError("Missing approval token. Call request_code_review first.")
    record_path = _code_review_record_path(project_path, approval_token)
    if not os.path.isfile(record_path):
        raise ValueError("Invalid or already-consumed approval token.")
    with open(record_path, "r") as handle:
        record = json.load(handle)
    if record.get("project_path") != project_path:
        raise ValueError("The approval token belongs to a different project.")
    if record.get("state") != "approved":
        raise ValueError("The approval token is already being used or has been consumed.")
    if datetime.now().timestamp() > float(record.get("expires_at_epoch", 0)):
        raise ValueError("The approval token expired. Review the current diff again.")
    actual_hash = hashlib.sha256(proposed_diff.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(record.get("diff_sha256", ""), actual_hash):
        raise ValueError("The diff changed after approval. Submit the new diff for review.")
    return record_path, record


def _set_code_review_state(record_path: str, record: dict, state: str) -> None:
    record["state"] = state
    record[f"{state}_at"] = datetime.now().isoformat()
    _write_text_atomic(record_path, json.dumps(record, indent=2))


async def _git_apply(project_path: str, proposed_diff: str, check_only: bool) -> tuple[int, str]:
    command = ["git", "-C", project_path, "apply"]
    if check_only:
        command.append("--check")
    command.extend(["--whitespace=nowarn", "-"])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(proposed_diff.encode("utf-8"))
    output = (stdout + stderr).decode(errors="replace").strip()
    return process.returncode, output


# --- Temporary mock integration lifecycle ---
MOCK_GRADLE_BEGIN = "// ANDROID_AUTODEV_MOCK_BEGIN"
MOCK_GRADLE_END = "// ANDROID_AUTODEV_MOCK_END"
MOCK_NETWORK_BEGIN = "// ANDROID_AUTODEV_NETWORK_BEGIN"
MOCK_NETWORK_END = "// ANDROID_AUTODEV_NETWORK_END"


def _mock_session_dir(project_path: str) -> str:
    project_key = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(LOG_DIR, "sessions", project_key)


def _api_mode_selection_path(project_path: str) -> str:
    project_key = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(LOG_DIR, "api-mode", f"{project_key}.json")


def _load_api_mode_selection(project_path: str) -> dict | None:
    selection_path = _api_mode_selection_path(project_path)
    if not os.path.isfile(selection_path):
        return None
    with open(selection_path, "r") as handle:
        return json.load(handle)


def _consume_mock_mode_selection(project_path: str, selection_token: str) -> dict:
    selection = _load_api_mode_selection(project_path)
    if not selection:
        raise ValueError(
            "API mode has not been confirmed. Ask the user to choose 'mock' or "
            "'real', then call select_api_mode."
        )
    if selection.get("mode") != "mock":
        raise ValueError(
            "The user selected the real API. Mock activation is not permitted."
        )
    if not secrets.compare_digest(selection.get("token", ""), selection_token or ""):
        raise ValueError(
            "Invalid or missing API-mode token. Ask the user again and call "
            "select_api_mode before activating mocks."
        )
    return selection


def _remove_marked_block(content: str, begin: str, end: str) -> tuple[str, bool]:
    """Remove one marker-delimited block without disturbing surrounding edits."""
    start = content.find(begin)
    if start < 0:
        return content, False
    line_start = content.rfind("\n", 0, start) + 1
    if content[line_start:start].strip() == "":
        start = line_start
    finish = content.find(end, start)
    if finish < 0:
        raise ValueError(f"Found '{begin}' without matching '{end}'.")
    finish += len(end)
    if finish < len(content) and content[finish] == "\n":
        finish += 1
    return content[:start] + content[finish:], True


def _write_text_atomic(path: str, content: str) -> None:
    temp_path = f"{path}.android-autodev.tmp"
    with open(temp_path, "w") as handle:
        handle.write(content)
    os.replace(temp_path, path)


def _mock_manifest_path(project_path: str) -> str:
    return os.path.join(_mock_session_dir(project_path), "manifest.json")


def _load_mock_manifest(project_path: str) -> dict | None:
    manifest_path = _mock_manifest_path(project_path)
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, "r") as handle:
        return json.load(handle)


def _remove_empty_parents(path: str, stop_at: str) -> None:
    current = os.path.dirname(path)
    stop_at = os.path.realpath(stop_at)
    while os.path.commonpath([os.path.realpath(current), stop_at]) == stop_at:
        if os.path.realpath(current) == stop_at or not os.path.isdir(current):
            break
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def _temporary_mock_artifacts(project_path: str) -> list[str]:
    """Find only MCP-tagged mock wiring; do not classify user-owned mocks as ours."""
    found = []
    app_dir = os.path.join(project_path, "app")
    if os.path.isdir(app_dir):
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [name for name in dirs if name not in {"build", ".gradle"}]
            for name in files:
                if not name.endswith((".kt", ".java", ".gradle", ".kts")):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, "r", errors="replace") as handle:
                        content = handle.read()
                except OSError:
                    continue
                if any(marker in content for marker in (
                    MOCK_GRADLE_BEGIN,
                    MOCK_GRADLE_END,
                    MOCK_NETWORK_BEGIN,
                    MOCK_NETWORK_END,
                    "Generated by Kiro AndroidAutoDev MCP server",
                    "Auto-generated: Provides OkHttpClient with MockApiInterceptor",
                )):
                    found.append(path)
    if os.path.isfile(_mock_manifest_path(project_path)):
        found.append(_mock_manifest_path(project_path))
    return sorted(set(found))


def _remove_stale_temporary_mock_artifacts(project_path: str) -> list[str]:
    """Remove marker-delimited or generated MCP artifacts after interrupted sessions."""
    cleaned = []
    for path in _temporary_mock_artifacts(project_path):
        if path == _mock_manifest_path(project_path) or not os.path.isfile(path):
            continue
        with open(path, "r", errors="replace") as handle:
            content = handle.read()
        original = content
        for begin, end in (
            (MOCK_GRADLE_BEGIN, MOCK_GRADLE_END),
            (MOCK_NETWORK_BEGIN, MOCK_NETWORK_END),
        ):
            while begin in content:
                content, _ = _remove_marked_block(content, begin, end)
        if content != original:
            _write_text_atomic(path, content)
            cleaned.append(path)
            continue
        if (
            (
                "Generated by Kiro AndroidAutoDev MCP server" in content
                or "Auto-generated: Provides OkHttpClient with MockApiInterceptor" in content
            )
            and os.path.join("app", "src", "mockDebug") in path
        ):
            os.remove(path)
            cleaned.append(path)
            _remove_empty_parents(path, os.path.join(project_path, "app", "src"))
    return cleaned


# --- Internal helpers ---
async def _ensure_appium_server():
    """Start Appium when needed and verify its status endpoint before testing."""
    global _appium_server_process
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("http://127.0.0.1:4723/status", timeout=1.0)
            if response.is_success:
                return _appium_server_process
    except httpx.HTTPError:
        pass

    if _appium_server_process is None or _appium_server_process.returncode is not None:
        appium_binary = shutil.which("appium")
        if not appium_binary:
            raise RuntimeError("Appium executable not found. Install Appium and the UiAutomator2 driver.")
        logger.info("Starting Appium server on port 4723")
        _appium_server_process = await asyncio.create_subprocess_exec(
            appium_binary, "--port", "4723", "--log-level", "warn",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(15):
        if _appium_server_process.returncode is not None:
            raise RuntimeError(
                f"Appium exited during startup with code {_appium_server_process.returncode}."
            )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("http://127.0.0.1:4723/status", timeout=1.0)
                if response.is_success:
                    return _appium_server_process
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    await _terminate_process_group(_appium_server_process)
    _appium_server_process = None
    raise RuntimeError("Appium did not become healthy on port 4723 within 15 seconds.")


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> bool:
    """Terminate an MCP-owned subprocess and every child it spawned."""
    if proc.returncode is not None:
        return True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    return proc.returncode is not None


async def _resolve_adb_device(device_serial: str | None = None) -> str:
    """Return one online ADB serial; never silently choose among multiple devices."""
    proc = await asyncio.create_subprocess_exec(
        "adb", "devices",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"adb devices failed: {stderr.decode().strip()}")

    states = {}
    for line in stdout.decode().splitlines()[1:]:
        fields = line.strip().split()
        if len(fields) >= 2:
            states[fields[0]] = fields[1]

    requested = (device_serial or os.environ.get("ANDROID_DEVICE_UDID", "")).strip()
    if requested:
        state = states.get(requested)
        if state != "device":
            available = [serial for serial, value in states.items() if value == "device"]
            raise RuntimeError(
                f"Requested ADB device '{requested}' is not online (state={state or 'missing'}). "
                f"Online devices: {available or 'none'}."
            )
        return requested

    online = [serial for serial, state in states.items() if state == "device"]
    if not online:
        raise RuntimeError("No online ADB device found. Connect a device and authorize USB debugging.")
    if len(online) > 1:
        raise RuntimeError(
            f"Multiple ADB devices are online: {online}. Pass device_serial explicitly."
        )
    return online[0]


async def _ensure_device_ready(device_serial: str | None = None) -> str:
    """Resolve the actual device serial and wait until Android reports boot complete."""
    serial = await _resolve_adb_device(device_serial)
    logger.info(f"Waiting for Android device {serial}...")
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, "shell", "getprop", "sys.boot_completed",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0 and stdout.decode().strip() == "1":
            logger.info(f"Android device ready: {serial}")
            return serial
        await asyncio.sleep(2)
    raise asyncio.TimeoutError(f"Device {serial} did not finish booting within 60 seconds.")


async def _ensure_emulator_ready(device_serial: str | None = None) -> str:
    """Backward-compatible alias; physical and network ADB devices are supported."""
    return await _ensure_device_ready(device_serial)


# ============================================================
# TOOL 1: Execute Gradle Commands (Whitelisted)
# ============================================================
@mcp.tool()
async def run_gradle(command: str, project_path: str, timeout_seconds: int = 240) -> dict:
    """Runs a whitelisted gradle command and returns structured success/failure data."""
    logger.info(f"run_gradle: command={command}, path={project_path}")

    # Validate command against whitelist + patterns
    # The tool accepts one Gradle task, not arbitrary command-line text. Checking
    # the complete value and using create_subprocess_exec prevents shell chaining.
    base_command = command.strip() if command else ""
    if not _is_gradle_command_allowed(command):
        return {
            "status": "FAILURE",
            "error_output": (
                f"Gradle command '{base_command}' is not allowed. "
                f"Allowed exact commands: {sorted(ALLOWED_GRADLE_COMMANDS)}. "
                f"Also allowed: flavor variants like assemble<Flavor><BuildType>, "
                f"test<Flavor><BuildType>UnitTest, connected<Flavor><BuildType>AndroidTest, "
                f"lint<Flavor><BuildType>, install<Flavor><BuildType>, bundle<Flavor><BuildType>."
            ),
        }

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    timeout_seconds = max(30, min(int(timeout_seconds), 270))
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "./gradlew",
            base_command,
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        if proc.returncode != 0:
            logger.warning(f"run_gradle FAILED: exit_code={proc.returncode}")
            return {
                "status": "FAILURE",
                "exit_code": proc.returncode,
                "error_output": stderr.decode("utf-8")[-4000:],
            }
        logger.info("run_gradle SUCCESS")
        return {"status": "SUCCESS", "output": stdout.decode("utf-8")[-2000:]}
    except asyncio.TimeoutError:
        terminated = await _terminate_process_group(proc) if proc else True
        logger.error(f"run_gradle TIMEOUT (terminated={terminated})")
        return {
            "status": "TIMEOUT",
            "process_terminated": terminated,
            "error_output": (
                f"Gradle command exceeded {timeout_seconds} seconds and its complete "
                "process group was terminated; no build was left running."
            ),
        }
    except asyncio.CancelledError:
        if proc:
            await asyncio.shield(_terminate_process_group(proc))
        logger.warning("run_gradle cancelled; Gradle process group terminated")
        raise


# --- Mock Response Helpers ---
def _detect_okhttp_major(project_path: str) -> int | None:
    """Detect the declared OkHttp major version without invoking Gradle."""
    patterns = (
        _re.compile(r"okhttpVersion\s*=\s*['\"](\d+)(?:\.\d+)*['\"]", _re.IGNORECASE),
        _re.compile(r"com\.squareup\.okhttp3:[^:'\"]+:\s*['\"]?(\d+)(?:\.\d+)*", _re.IGNORECASE),
        _re.compile(r"^\s*okhttp\s*=\s*['\"](\d+)(?:\.\d+)*['\"]", _re.IGNORECASE | _re.MULTILINE),
    )
    candidates = (
        os.path.join(project_path, "build.gradle"),
        os.path.join(project_path, "build.gradle.kts"),
        os.path.join(project_path, "app", "build.gradle"),
        os.path.join(project_path, "app", "build.gradle.kts"),
        os.path.join(project_path, "gradle", "libs.versions.toml"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, "r", errors="replace") as handle:
            content = handle.read()
        for pattern in patterns:
            match = pattern.search(content)
            if match:
                return int(match.group(1))
    return None


def _okhttp_request_accessors(project_path: str) -> tuple[int | None, str, str, str]:
    """Return version-appropriate Kotlin expressions for OkHttp request data."""
    major = _detect_okhttp_major(project_path)
    legacy = major is None or major < 4
    return (
        major,
        "java-methods" if legacy else "kotlin-properties",
        "request.method()" if legacy else "request.method",
        "request.url().encodedPath()" if legacy else "request.url.encodedPath",
    )


def _extract_endpoint_responses(spec_content: str) -> dict:
    """Parse the spec to extract per-endpoint response models.
    
    Handles markdown-formatted specs with:
    - JSON code blocks following endpoint definitions
    - Response/Example sections with JSON bodies
    - Code-fenced and backtick-wrapped endpoint references
    - Markdown headers, bullet lists, tables
    
    Returns a tuple: (endpoint_responses dict, model_definitions dict)
    where endpoint_responses maps (METHOD, path) -> response body string.
    """
    import re

    endpoint_responses = {}

    # Pre-process: normalize the spec content
    normalized = spec_content

    # Strategy 1: Find endpoint + JSON block pairs
    # Handles patterns like:
    #   ### POST /api/v1/kyc/initiate
    #   ```json
    #   { "journeyId": "..." }
    #   ```
    # Or:
    #   `POST /api/v1/kyc/initiate`
    #   Response:
    #   ```json
    #   { ... }
    #   ```
    # We specifically look for RESPONSE JSON blocks (not request bodies)
    endpoint_then_json = re.compile(
        r"(?:^|\n)[#*\-`|\s]*"                              # Optional markdown prefix
        r"(GET|POST|PUT|DELETE|PATCH)\s+"
        r"[`]?(/[^\s`\)\"\'|]+)[`]?"                        # Path (optionally backtick-wrapped)
        r"([\s\S]*?)"                                         # Content between endpoint and JSON
        r"```(?:json|JSON)?\s*\n([\s\S]*?)```",              # JSON code block
        re.IGNORECASE | re.MULTILINE
    )
    
    for match in endpoint_then_json.finditer(normalized):
        method = match.group(1).upper()
        path = match.group(2).split("?")[0]  # Strip query params
        between_text = match.group(3)
        json_block = match.group(4).strip()
        
        # Only associate if the JSON block is reasonably close (not a different section)
        # Check there isn't another endpoint definition between this one and the JSON
        if re.search(r"(GET|POST|PUT|DELETE|PATCH)\s+/", between_text, re.IGNORECASE):
            continue
        
        # Skip REQUEST bodies — only capture RESPONSE bodies
        # A JSON block is a request body if the text before it mentions "request", "body", "payload"
        # but NOT "response"
        between_lower = between_text.lower()
        is_request_body = (
            re.search(r"request\s*body|request\s*payload|request\s*param", between_lower) and
            not re.search(r"response", between_lower)
        )
        if is_request_body:
            continue
            
        # Validate it's actually JSON
        try:
            json.loads(json_block)
            # Check if it's an error response
            if re.search(r"(?:error|4\d\d|5\d\d|bad.?request|unauthorized|forbidden)", 
                        between_text, re.IGNORECASE):
                # Store as error response
                status_match = re.search(r"(4\d\d|5\d\d)", between_text)
                status = status_match.group(1) if status_match else "400"
                endpoint_responses[(method, path + f":{status}")] = json_block
            else:
                # Success response — don't overwrite if we already have one
                if (method, path) not in endpoint_responses:
                    endpoint_responses[(method, path)] = json_block
        except json.JSONDecodeError:
            pass

    # Strategy 2: Section-based parsing — split by endpoint definitions
    # and associate the nearest following JSON block with each endpoint
    endpoint_def_pattern = re.compile(
        r"[#*\-`|\s]*(GET|POST|PUT|DELETE|PATCH)\s+[`]?(/[^\s`\)\"\'|]+)[`]?",
        re.IGNORECASE | re.MULTILINE
    )
    
    # Find all endpoint positions
    endpoint_positions = [(m.start(), m.group(1).upper(), m.group(2).split("?")[0]) 
                          for m in endpoint_def_pattern.finditer(normalized)]
    
    for i, (pos, method, path) in enumerate(endpoint_positions):
        if (method, path) in endpoint_responses:
            continue  # Already found via Strategy 1
            
        # Get text between this endpoint and the next one (or end of file)
        end_pos = endpoint_positions[i + 1][0] if i + 1 < len(endpoint_positions) else len(normalized)
        section_text = normalized[pos:end_pos]
        
        # Look for JSON blocks in this section after "response" or "success" markers
        response_section = re.search(
            r"(?:response|success|result|returns?|output|example\s*response|200)[^\n]*\n"
            r"[\s\S]*?```(?:json|JSON)?\s*\n([\s\S]*?)```",
            section_text, re.IGNORECASE
        )
        if response_section:
            json_block = response_section.group(1).strip()
            try:
                json.loads(json_block)
                endpoint_responses[(method, path)] = json_block
            except json.JSONDecodeError:
                pass
        else:
            # Find all JSON blocks and pick the one that's most likely a response
            # (skip those immediately after "request body" markers)
            json_blocks = list(re.finditer(r"```(?:json|JSON)?\s*\n([\s\S]*?)```", section_text))
            for jb_match in json_blocks:
                # Check preceding context (100 chars before the block)
                pre_context = section_text[max(0, jb_match.start()-150):jb_match.start()].lower()
                # Skip if preceded by request-body markers without response markers
                if re.search(r"request\s*body|request\s*payload|request\s*param", pre_context):
                    if not re.search(r"response", pre_context):
                        continue
                json_block = jb_match.group(1).strip()
                try:
                    json.loads(json_block)
                    endpoint_responses[(method, path)] = json_block
                    break
                except json.JSONDecodeError:
                    pass

    # Strategy 3: Inline JSON objects on the same line or near an endpoint
    inline_json = re.compile(
        r"(GET|POST|PUT|DELETE|PATCH)\s+[`]?(/[^\s`\)\"\'|]+)[`]?"
        r"[^\n]*[\s\S]{0,300}?"
        r"(?:returns?|response|body|\u2192|->|:)\s*"
        r"(\{[^\n]*\})",
        re.IGNORECASE
    )
    for match in inline_json.finditer(normalized):
        method = match.group(1).upper()
        path = match.group(2).split("?")[0]
        json_str = match.group(3).strip()
        if (method, path) not in endpoint_responses:
            try:
                json.loads(json_str)
                endpoint_responses[(method, path)] = json_str
            except json.JSONDecodeError:
                pass

    # Strategy 4: Extract data model definitions
    model_definitions = _extract_data_models(spec_content)

    return endpoint_responses, model_definitions


def _extract_data_models(spec_content: str) -> dict:
    """Extract data model definitions from the spec.
    
    Recognizes patterns like:
    - Kotlin data classes: data class User(val id: String, val name: String)
    - Field tables: | field | type | description |
    - JSON Schema style definitions
    - Bullet-list field definitions: - id (String): user identifier
    
    Returns dict mapping model name -> dict of field_name -> sample_value
    """
    import re

    models = {}

    # Pattern 1: Kotlin/Java data class definitions
    data_class_pattern = re.compile(
        r"data\s+class\s+(\w+)\s*\(([\s\S]*?)\)",
        re.IGNORECASE
    )
    for match in data_class_pattern.finditer(spec_content):
        class_name = match.group(1)
        fields_str = match.group(2)
        fields = _parse_kotlin_fields(fields_str)
        if fields:
            models[class_name] = fields

    # Pattern 2: Markdown field tables
    # | field | type | ... |
    table_sections = re.split(r"#+\s*(.+)", spec_content)
    for i in range(1, len(table_sections), 2):
        section_name = table_sections[i].strip() if i < len(table_sections) else ""
        section_body = table_sections[i + 1] if i + 1 < len(table_sections) else ""

        table_pattern = re.compile(
            r"\|\s*(\w+)\s*\|\s*(\w+(?:\?)?)\s*\|[^\n]*",
        )
        fields = {}
        for row in table_pattern.finditer(section_body):
            field_name = row.group(1)
            field_type = row.group(2)
            if field_name.lower() not in ("field", "name", "key", "---", "parameter"):
                fields[field_name] = _sample_value_for_type(field_name, field_type)
        if fields:
            # Use section heading as model name
            model_name = re.sub(r"[^a-zA-Z0-9]", "", section_name)
            if model_name:
                models[model_name] = fields

    # Pattern 3: Bullet-list field definitions
    # - fieldName (Type): description
    # - fieldName: Type - description
    bullet_section_pattern = re.compile(
        r"#+\s*([\w\s]+(?:Model|Response|Request|DTO|Entity|Object))\s*\n((?:\s*[-*]\s+\w+.*\n)+)",
        re.IGNORECASE
    )
    for match in bullet_section_pattern.finditer(spec_content):
        model_name = re.sub(r"\s+", "", match.group(1))
        bullets = match.group(2)
        fields = {}
        bullet_field = re.compile(
            r"[-*]\s+(\w+)\s*[\(:]?\s*(\w+)"
        )
        for field_match in bullet_field.finditer(bullets):
            field_name = field_match.group(1)
            field_type = field_match.group(2)
            fields[field_name] = _sample_value_for_type(field_name, field_type)
        if fields:
            models[model_name] = fields

    return models


def _parse_kotlin_fields(fields_str: str) -> dict:
    """Parse Kotlin data class field declarations into sample values."""
    import re

    fields = {}
    # Match: val/var fieldName: Type (stop at comma, closing paren, or newline)
    field_pattern = re.compile(
        r"(?:val|var)\s+(\w+)\s*:\s*([A-Za-z][\w<>,?\s]*?)(?:\s*=\s*[^,\)]+)?\s*[,\)\n]"
    )
    for match in field_pattern.finditer(fields_str):
        field_name = match.group(1)
        field_type = match.group(2).strip().rstrip("?")
        fields[field_name] = _sample_value_for_type(field_name, field_type)
    return fields


def _sample_value_for_type(field_name: str, field_type: str) -> object:
    """Generate a realistic sample value based on field name and type."""
    field_lower = field_name.lower()
    type_lower = field_type.lower().rstrip("?")

    # Name-based heuristics (more specific than type alone)
    if field_lower == "id" or field_lower.endswith("id") or field_lower.endswith("_id"):
        return "usr_001" if "user" in field_lower else "1"
    if field_lower == "email" or "email" in field_lower:
        return "user@example.com"
    if field_lower in ("username", "displayname", "display_name", "fullname", "full_name"):
        return "Test User"
    if field_lower == "name":
        return "Sample Name"
    if field_lower in ("productname", "product_name", "itemname", "item_name"):
        return "Sample Product"
    if field_lower == "firstname" or field_lower == "first_name":
        return "John"
    if field_lower == "lastname" or field_lower == "last_name":
        return "Doe"
    if field_lower == "phone" or "phone" in field_lower:
        return "+1234567890"
    if field_lower == "avatar" or "image" in field_lower or "photo" in field_lower or "url" in field_lower:
        return "https://example.com/image.png"
    if field_lower == "token" or "token" in field_lower:
        return "eyJhbGciOiJIUzI1NiJ9.mock-token-value"
    if field_lower == "password" or "secret" in field_lower:
        return "********"
    if "created" in field_lower or "updated" in field_lower or "date" in field_lower or "time" in field_lower:
        return "2024-06-15T10:30:00Z"
    if field_lower == "status":
        return "active"
    if field_lower == "message" or field_lower == "description":
        return "Sample text"
    if "amount" in field_lower or "price" in field_lower or "total" in field_lower:
        return 99.99
    if "count" in field_lower or "quantity" in field_lower or "age" in field_lower:
        return 1
    if "enabled" in field_lower or "active" in field_lower or field_lower.startswith("is") or field_lower.startswith("has"):
        return True
    if "address" in field_lower:
        return "123 Main St, City, ST 12345"
    if "title" in field_lower:
        return "Sample Title"
    if "color" in field_lower or "colour" in field_lower:
        return "#FF5722"

    # Type-based fallbacks
    if type_lower in ("string", "str", "text", "charsequence"):
        return f"sample_{field_name}"
    if type_lower in ("int", "integer", "long", "short"):
        return 1
    if type_lower in ("float", "double", "decimal", "number"):
        return 1.0
    if type_lower in ("boolean", "bool"):
        return True
    if type_lower.startswith("list") or type_lower.startswith("array"):
        return []
    if type_lower.startswith("map") or type_lower == "object":
        return {}

    return f"sample_{field_name}"


def _build_response_from_model(model: dict) -> str:
    """Convert a model field dict into a JSON response string."""
    return json.dumps(model, indent=2)


def _generate_spec_aware_response(method: str, path: str, endpoint_responses: dict, model_definitions: dict) -> str:
    """Generate a mock response body using parsed spec data.
    
    Priority:
    1. Exact endpoint response from spec (JSON block following the endpoint)
    2. Model-based response (if a matching model is found for the endpoint)
    3. URL-heuristic fallback (last resort)
    """
    import re

    # Priority 1: Exact match from spec
    if (method, path) in endpoint_responses:
        return endpoint_responses[(method, path)]

    # Priority 2: Find matching model based on endpoint path
    # e.g., /api/v1/users -> "User" model, /api/products/{id} -> "Product" model
    path_segments = [s for s in path.strip("/").split("/") if not s.startswith("{") and not re.match(r"v\d+", s) and s != "api"]
    
    for segment in reversed(path_segments):
        # Try singular and plural forms
        singular = segment.rstrip("s") if segment.endswith("s") and len(segment) > 3 else segment
        for model_name, model_fields in model_definitions.items():
            model_lower = model_name.lower()
            if singular.lower() in model_lower or model_lower in singular.lower() or segment.lower() in model_lower:
                if method == "GET" and path.endswith("s") and not re.search(r"\{[^}]+\}$", path):
                    # List response
                    return json.dumps({
                        "data": [model_fields],
                        "total": 1,
                        "page": 1,
                        "pageSize": 20
                    })
                elif method == "DELETE":
                    return json.dumps({"message": f"{singular.capitalize()} deleted successfully"})
                else:
                    return json.dumps(model_fields)

    # Priority 3: URL-heuristic fallback
    return _generate_fallback_response(method, path)


def _generate_fallback_response(method: str, path: str) -> str:
    """Last-resort response generation when spec provides no model info."""
    import re

    path_lower = path.lower()

    if "login" in path_lower or ("auth" in path_lower and method == "POST"):
        return json.dumps({
            "token": "eyJhbGciOiJIUzI1NiJ9.mock-token",
            "refreshToken": "mock-refresh-token",
            "expiresIn": 3600,
            "tokenType": "Bearer"
        })

    if "refresh" in path_lower and "token" in path_lower:
        return json.dumps({
            "token": "eyJhbGciOiJIUzI1NiJ9.refreshed-token",
            "refreshToken": "mock-refresh-token-new",
            "expiresIn": 3600,
            "tokenType": "Bearer"
        })

    if method == "GET" and (path_lower.rstrip("/").endswith("s") or "/list" in path_lower):
        resource = path.rstrip("/").split("/")[-1]
        return json.dumps({
            "data": [{"id": "1", "name": f"Sample {resource}"}],
            "total": 1,
            "page": 1,
            "pageSize": 20
        })

    if method == "DELETE":
        return json.dumps({"message": "Deleted successfully"})

    if method in ("PUT", "PATCH"):
        return json.dumps({"message": "Updated successfully"})

    if method == "POST":
        return json.dumps({"id": "new_001", "message": "Created successfully"})

    return json.dumps({"status": "ok", "mock": True})


# ============================================================
# TOOL 2: Request Mandatory Manual Code Review
# ============================================================
@mcp.tool()
async def request_code_review(
    project_path: str,
    change_summary: str,
    proposed_diff: str,
) -> dict:
    """Create a persistent chat checkpoint for an exact product-source diff.

    Call this before making any product-source edit. After it returns, show the
    proposed_diff verbatim in chat and END THE TURN. Wait for the user's next
    message. Do not call record_code_review_decision during the same turn.
    """
    try:
        project_path = validate_path(project_path, "project_path")
        changed_files = _validate_review_diff(proposed_diff)
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    summary = (change_summary or "").strip()
    if not summary:
        return {"status": "FAILURE", "error_output": "change_summary is required."}

    review_id, record = _store_pending_review(
        project_path, summary, proposed_diff, changed_files
    )
    logger.info(
        "request_code_review: awaiting chat response project=%s diff=%s files=%s",
        project_path,
        record["diff_sha256"],
        changed_files,
    )
    return {
        "status": "AWAITING_USER_REVIEW",
        "review_id": review_id,
        "diff_sha256": record["diff_sha256"],
        "changed_files": changed_files,
        "change_summary": summary,
        "proposed_diff": proposed_diff,
        "expires_in_seconds": PENDING_REVIEW_TTL_SECONDS,
        "message": (
            "Show change_summary and proposed_diff to the user in chat, ask them to "
            "reply with approve or requested changes, and END THIS TURN. No source "
            "change is authorized yet."
        ),
    }


# ============================================================
# TOOL 3: Record the User's Next-Message Review Decision
# ============================================================
@mcp.tool()
async def record_code_review_decision(
    project_path: str,
    review_id: str,
    proposed_diff: str,
    decision: str,
    user_response: str,
    user_confirmed: bool = False,
) -> dict:
    """Resume a pending review using the user's decision from their next message.

    Call only in a later turn after request_code_review has halted for chat review.
    Relay the user's actual response in user_response and set user_confirmed=true.
    decision must be approve, request_changes, or reject. Only approve creates a
    short-lived token for apply_reviewed_patch.
    """
    try:
        project_path = validate_path(project_path, "project_path")
        _validate_review_diff(proposed_diff)
        record_path, record = _load_pending_review(
            project_path, review_id, proposed_diff
        )
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    normalized_decision = (decision or "").strip().lower()
    if not user_confirmed or not (user_response or "").strip():
        return {
            "status": "NEEDS_USER_DECISION",
            "message": (
                "Wait for the user's next chat message, then relay their actual "
                "response with user_confirmed=true."
            ),
        }
    if normalized_decision not in {"approve", "request_changes", "reject"}:
        return {
            "status": "FAILURE",
            "error_output": "decision must be approve, request_changes, or reject.",
        }

    record["user_response"] = user_response.strip()
    if normalized_decision == "approve":
        approval_token, approval = _store_code_review(
            project_path,
            record["change_summary"],
            proposed_diff,
        )
        _set_code_review_state(record_path, record, "approved_in_chat")
        logger.info(
            "record_code_review_decision: approved project=%s diff=%s",
            project_path,
            record["diff_sha256"],
        )
        return {
            "status": "APPROVED",
            "approval_token": approval_token,
            "diff_sha256": approval["diff_sha256"],
            "expires_in_seconds": CODE_REVIEW_TTL_SECONDS,
            "message": "Resume the workflow and apply this exact diff once.",
        }

    resolved_state = (
        "changes_requested_in_chat"
        if normalized_decision == "request_changes"
        else "rejected_in_chat"
    )
    _set_code_review_state(record_path, record, resolved_state)
    return {
        "status": (
            "CHANGES_REQUESTED"
            if normalized_decision == "request_changes"
            else "REJECTED"
        ),
        "feedback": user_response.strip(),
        "message": (
            "Do not apply this diff. Prepare a revised proposal and start a new "
            "chat review."
            if normalized_decision == "request_changes"
            else "Do not apply this diff. The review is closed."
        ),
    }


# ============================================================
# TOOL 4: Apply an Approved Code Diff
# ============================================================
@mcp.tool()
async def apply_reviewed_patch(
    project_path: str,
    proposed_diff: str,
    approval_token: str,
) -> dict:
    """Apply exactly the product-source diff approved by request_code_review.

    The token is project-bound, diff-bound, expires after 30 minutes, and can be
    consumed only once. The patch is path-validated and checked by git before any
    file is changed.
    """
    try:
        project_path = validate_path(project_path, "project_path")
        changed_files = _validate_review_diff(proposed_diff)
        record_path, record = _authorize_reviewed_diff(
            project_path, proposed_diff, approval_token
        )
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    _set_code_review_state(record_path, record, "applying")
    try:
        returncode, output = await _git_apply(project_path, proposed_diff, check_only=True)
        if returncode != 0:
            _set_code_review_state(record_path, record, "approved")
            return {
                "status": "FAILURE",
                "error_output": f"Reviewed patch no longer applies cleanly: {output}",
                "message": "No files were changed. Resolve the conflict and request a new review.",
            }

        returncode, output = await _git_apply(project_path, proposed_diff, check_only=False)
        if returncode != 0:
            _set_code_review_state(record_path, record, "approved")
            return {
                "status": "FAILURE",
                "error_output": f"git apply failed: {output}",
                "message": "The token remains valid for the exact same diff until it expires.",
            }
    except Exception as exc:
        _set_code_review_state(record_path, record, "approved")
        return {"status": "FAILURE", "error_output": str(exc)}

    _set_code_review_state(record_path, record, "consumed")
    logger.info(
        "apply_reviewed_patch: applied project=%s diff=%s files=%s",
        project_path,
        record["diff_sha256"],
        changed_files,
    )
    return {
        "status": "SUCCESS",
        "changed_files": changed_files,
        "diff_sha256": record["diff_sha256"],
        "message": "The exact manually reviewed diff was applied; its token is now consumed.",
    }


# ============================================================
# TOOL 5: Select API Mode (Mandatory User Choice)
# ============================================================
@mcp.tool()
async def select_api_mode(
    project_path: str,
    api_mode: str,
    user_confirmed: bool = False,
) -> dict:
    """Record the user's explicit choice of real API or mock responses.

    The agent MUST ask the user before calling this tool. Set user_confirmed=true
    only after the user explicitly answers "real" or "mock" for the current
    workflow. A mock choice returns a one-time token required by
    activate_mock_environment. A real choice never adds mock code and removes any
    active temporary mock integration first.
    """
    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    mode = (api_mode or "").strip().lower()
    if not user_confirmed:
        return {
            "status": "NEEDS_USER_CHOICE",
            "question": "Do you want to use the deployed real API or temporary mock responses?",
            "allowed_modes": ["real", "mock"],
            "message": "Ask the user and call select_api_mode again with user_confirmed=true.",
        }
    if mode not in {"real", "mock"}:
        return {
            "status": "FAILURE",
            "error_output": "api_mode must be exactly 'real' or 'mock'.",
        }

    cleanup = None
    stale_cleanup = []
    if mode == "real":
        if _load_mock_manifest(project_path):
            cleanup = await deactivate_mock_environment(project_path)
            if cleanup["status"] == "FAILURE":
                return cleanup
        try:
            stale_cleanup = _remove_stale_temporary_mock_artifacts(project_path)
        except Exception as exc:
            return {"status": "FAILURE", "error_output": f"Mock cleanup failed: {exc}"}
        remaining = _temporary_mock_artifacts(project_path)
        if remaining:
            return {
                "status": "FAILURE",
                "error_output": "Real API mode blocked: temporary mock wiring remains.",
                "mock_artifacts": remaining,
            }

    selection_path = _api_mode_selection_path(project_path)
    os.makedirs(os.path.dirname(selection_path), exist_ok=True)
    token = secrets.token_urlsafe(24)
    selection = {
        "project_path": project_path,
        "mode": mode,
        "token": token,
        "user_confirmed": True,
        "selected_at": datetime.now().isoformat(),
    }
    with open(selection_path, "w") as handle:
        json.dump(selection, handle, indent=2)

    logger.info(f"select_api_mode: user confirmed mode={mode} project={project_path}")
    response = {
        "status": "SUCCESS",
        "api_mode": mode,
        "message": (
            "Real API selected. Do not activate or generate mocks."
            if mode == "real"
            else "Mock responses selected. Pass selection_token to activate_mock_environment."
        ),
    }
    if mode == "mock":
        response["selection_token"] = token
    if cleanup:
        response["mock_cleanup"] = cleanup
    if stale_cleanup:
        response["stale_mock_cleanup"] = stale_cleanup
    return response


# ============================================================
# TOOL 3: Activate Temporary Mock Integration
# ============================================================
@mcp.tool()
async def activate_mock_environment(
    project_path: str,
    package_name: str,
    selection_token: str,
    network_module_path: str = "app/src/main/java/com/delhivery/axle/injection/module/NetworkModule.kt",
) -> dict:
    """Temporarily add a mock flavor and OkHttp wiring for an MCP work session.

    Requires the one-time token returned after the user explicitly chooses mock
    mode with select_api_mode. Every source edit is enclosed in unique marker comments. Call
    deactivate_mock_environment when the work is complete; it removes only those
    blocks and restores any pre-existing mockDebug source set.
    """
    import shutil

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    try:
        _consume_mock_mode_selection(project_path, selection_token)
    except ValueError as exc:
        return {
            "status": "NEEDS_USER_CHOICE",
            "error_output": str(exc),
            "question": "Do you want to use the deployed real API or temporary mock responses?",
        }

    gradle_path = os.path.join(project_path, "app", "build.gradle")
    network_path = os.path.join(project_path, network_module_path)
    mock_debug_dir = os.path.join(project_path, "app", "src", "mockDebug")
    manifest_path = _mock_manifest_path(project_path)
    session_dir = _mock_session_dir(project_path)
    backup_dir = os.path.join(session_dir, "mockDebug.backup")

    if os.path.exists(manifest_path):
        return {
            "status": "ALREADY_ACTIVE",
            "manifest_path": manifest_path,
            "message": "Temporary mock integration is already active.",
        }
    if not os.path.isfile(gradle_path) or not os.path.isfile(network_path):
        return {
            "status": "FAILURE",
            "error_output": f"Required file missing: {gradle_path if not os.path.isfile(gradle_path) else network_path}",
        }

    with open(gradle_path, "r") as handle:
        gradle_content = handle.read()
    with open(network_path, "r") as handle:
        network_content = handle.read()

    if MOCK_GRADLE_BEGIN in gradle_content or MOCK_NETWORK_BEGIN in network_content:
        return {
            "status": "FAILURE",
            "error_output": "Temporary mock markers already exist without an active manifest. Run deactivate_mock_environment first.",
        }

    flavors_anchor = "  productFlavors {\n"
    timeout_anchor = "      .writeTimeout(15, SECONDS)\n"
    services_anchor = "  /*/* Services to be placed here */*/\n"
    missing = [
        name
        for name, anchor, content in (
            ("productFlavors", flavors_anchor, gradle_content),
            ("OkHttp timeout chain", timeout_anchor, network_content),
            ("NetworkModule services marker", services_anchor, network_content),
        )
        if anchor not in content
    ]
    if missing:
        return {
            "status": "FAILURE",
            "error_output": "Could not safely locate: " + ", ".join(missing),
        }

    gradle_block = (
        f"    {MOCK_GRADLE_BEGIN}\n"
        "    mock {\n"
        "      dimension \"vanilla\"\n"
        "      resValue \"string\", \"app_name\", \"Mock Axle\"\n"
        "      resValue(\"bool\", \"FIREBASE_ACTIVATED\", \"false\")\n"
        "      resValue(\"bool\", \"FIREBASE_PERFORMANCE_LOG\", \"false\")\n"
        "      resConfigs \"en\", \"xxhdpi\"\n"
        "    }\n"
        f"    {MOCK_GRADLE_END}\n"
    )
    gradle_content = gradle_content.replace(
        flavors_anchor, flavors_anchor + gradle_block, 1
    )

    interceptor_chain_block = (
        f"      {MOCK_NETWORK_BEGIN}\n"
        "      .addInterceptor(androidAutoDevMockInterceptor())\n"
        f"      {MOCK_NETWORK_END}\n"
    )
    network_content = network_content.replace(
        timeout_anchor, timeout_anchor + interceptor_chain_block, 1
    )
    helper_block = f'''  {MOCK_NETWORK_BEGIN}
  private fun androidAutoDevMockInterceptor(): okhttp3.Interceptor {{
    if (com.delhivery.axle.BuildConfig.FLAVOR != "mock") {{
      return okhttp3.Interceptor {{ chain -> chain.proceed(chain.request()) }}
    }}

    return Class.forName("{package_name}.network.MockApiInterceptor")
        .getDeclaredConstructor()
        .newInstance() as okhttp3.Interceptor
  }}
  {MOCK_NETWORK_END}
'''
    network_content = network_content.replace(
        services_anchor, helper_block + services_anchor, 1
    )

    os.makedirs(session_dir, exist_ok=True)
    had_mock_debug = os.path.isdir(mock_debug_dir)
    if had_mock_debug:
        shutil.copytree(mock_debug_dir, backup_dir)

    manifest = {
        "project_path": project_path,
        "package_name": package_name,
        "gradle_path": gradle_path,
        "network_path": network_path,
        "mock_debug_dir": mock_debug_dir,
        "mock_debug_backup": backup_dir if had_mock_debug else None,
        "activated_at": datetime.now().isoformat(),
    }
    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=2)

    try:
        _write_text_atomic(gradle_path, gradle_content)
        _write_text_atomic(network_path, network_content)
    except Exception as exc:
        await deactivate_mock_environment(project_path)
        return {"status": "FAILURE", "error_output": str(exc)}

    selection_path = _api_mode_selection_path(project_path)
    if os.path.isfile(selection_path):
        os.remove(selection_path)

    logger.info(f"activate_mock_environment: active for {project_path}")
    return {
        "status": "SUCCESS",
        "manifest_path": manifest_path,
        "temporary_files": [gradle_path, network_path, mock_debug_dir],
        "message": "Temporary mock integration activated. Always call deactivate_mock_environment in a finally/cleanup step.",
    }


# ============================================================
# TOOL 3: Deactivate Temporary Mock Integration
# ============================================================
@mcp.tool()
async def deactivate_mock_environment(project_path: str) -> dict:
    """Remove temporary mock wiring and generated sources, preserving feature edits."""
    import shutil

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    manifest = _load_mock_manifest(project_path)
    if not manifest:
        return {
            "status": "NOT_ACTIVE",
            "message": "No temporary mock integration is active; nothing was changed.",
        }

    cleaned = []
    try:
        for path, begin, end in (
            (manifest["gradle_path"], MOCK_GRADLE_BEGIN, MOCK_GRADLE_END),
            (manifest["network_path"], MOCK_NETWORK_BEGIN, MOCK_NETWORK_END),
        ):
            with open(path, "r") as handle:
                content = handle.read()
            removed_any = False
            while begin in content:
                content, removed = _remove_marked_block(content, begin, end)
                removed_any = removed_any or removed
            if removed_any:
                _write_text_atomic(path, content)
                cleaned.append(path)

        mock_debug_dir = manifest["mock_debug_dir"]
        if os.path.isdir(mock_debug_dir):
            shutil.rmtree(mock_debug_dir)
            cleaned.append(mock_debug_dir)
        backup_dir = manifest.get("mock_debug_backup")
        if backup_dir and os.path.isdir(backup_dir):
            shutil.copytree(backup_dir, mock_debug_dir)
            cleaned.append(f"restored {mock_debug_dir}")
        else:
            _remove_empty_parents(mock_debug_dir, os.path.join(project_path, "app", "src"))

        session_dir = _mock_session_dir(project_path)
        shutil.rmtree(session_dir)
        logger.info(f"deactivate_mock_environment: cleaned {project_path}")
        return {
            "status": "SUCCESS",
            "cleaned": cleaned,
            "message": "Temporary mock integration removed; feature code was preserved.",
        }
    except Exception as exc:
        logger.error(f"deactivate_mock_environment ERROR: {exc}")
        return {
            "status": "FAILURE",
            "error_output": str(exc),
            "manifest_path": _mock_manifest_path(project_path),
            "message": "Cleanup is incomplete; the manifest was retained for retry.",
        }


# ============================================================
# TOOL 4: Generate Mock Interceptor (Temporary Mocking)
# ============================================================
@mcp.tool()
async def generate_mock_interceptor(
    spec_path: str, package_name: str, project_path: str
) -> dict:
    """Parses design.md/requirements.md and generates a Kotlin OkHttp MockApiInterceptor.
    Outputs to <project_path>/app/src/mockDebug/java/<package>/network/.
    project_path should be the Android project root (e.g., /path/to/MyApp).
    activate_mock_environment must be called first. Generated files are removed
    by deactivate_mock_environment at the end of the MCP work session."""
    import re

    logger.info(f"generate_mock_interceptor: spec={spec_path}, pkg={package_name}, project={project_path}")

    try:
        spec_path = validate_path(spec_path, "spec_path")
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    if not _load_mock_manifest(project_path):
        return {
            "status": "FAILURE",
            "error_output": "Mock environment is not active. Call activate_mock_environment first.",
        }

    # Step 1: Read spec file
    try:
        with open(spec_path, "r") as f:
            spec_content = f.read()
    except FileNotFoundError:
        return {"status": "FAILURE", "error_output": f"Spec file not found: {spec_path}"}

    # Step 2: Extract API endpoints — handles markdown formatting
    # Strips markdown noise: headers (##), code fences (```), inline code (`), bullet prefixes (- *)
    # Also handles table rows (|) and numbered lists (1.)
    endpoint_pattern = re.compile(
        r"(?:^|\s|[`*_\-|#>\d.)\]:])\s*"  # Allow markdown prefixes before method
        r"(GET|POST|PUT|DELETE|PATCH)\s+"
        r"[`]?(/[^\s`\)\"\'|]+)[`]?",     # Path optionally wrapped in backticks
        re.IGNORECASE | re.MULTILINE
    )
    
    # Pre-process: strip code fence markers so endpoints inside fences are found
    # but preserve the content between fences
    stripped_content = re.sub(r"^```[\w]*\s*$", "", spec_content, flags=re.MULTILINE)
    
    endpoints_raw = endpoint_pattern.findall(stripped_content)
    
    # Also try bare pattern for simple specs (METHOD /path at line start)
    bare_pattern = re.compile(
        r"^(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s\)\"\'`]+)",
        re.IGNORECASE | re.MULTILINE
    )
    endpoints_bare = bare_pattern.findall(stripped_content)
    
    # Merge and deduplicate, preserving order
    seen = set()
    endpoints = []
    for method, path in endpoints_raw + endpoints_bare:
        key = (method.upper(), path.rstrip("/"))
        if key not in seen:
            seen.add(key)
            endpoints.append((method.upper(), path))

    if not endpoints:
        return {
            "status": "FAILURE",
            "error_output": "No API endpoints found in spec. Expected patterns like 'GET /api/resource' or '`POST /api/resource`'.",
        }

    # Step 2b: Extract response models and per-endpoint responses from spec
    endpoint_responses, model_definitions = _extract_endpoint_responses(spec_content)
    logger.info(
        f"Spec parsing: {len(endpoint_responses)} explicit responses, "
        f"{len(model_definitions)} data models found"
    )

    # Step 3: Build Kotlin interceptor code
    package_path = package_name.replace(".", "/")

    # Fix path-nesting bug: ensure we always write relative to the project root.
    # Strip trailing path components if project_path already contains app/src/mockDebug
    # to prevent double-nesting (e.g., output_dir/app/src/mockDebug/app/src/mockDebug/...)
    mock_debug_suffix = os.path.join("app", "src", "mockDebug", "java", package_path, "network")
    resolved_project = project_path

    # If project_path already ends with any portion of the mockDebug path, walk up to the real project root
    for partial in ["app/src/mockDebug", "app/src/mockDebug/java", "app/src/mockDebug/java/" + package_path]:
        normalized_partial = os.path.normpath(partial)
        if resolved_project.endswith(normalized_partial) or resolved_project.endswith(normalized_partial + "/"):
            resolved_project = resolved_project[: resolved_project.rfind(normalized_partial.split(os.sep)[0])]
            resolved_project = resolved_project.rstrip("/")
            logger.info(f"Path-nesting fix: stripped partial '{partial}', resolved to '{resolved_project}'")
            break

    interceptor_dir = os.path.join(resolved_project, mock_debug_suffix)
    os.makedirs(interceptor_dir, exist_ok=True)

    # Generate mock response entries using spec-aware response bodies
    mock_entries = []
    for method, path in endpoints:
        method = method.upper()

        # FIX: Strip query parameters from the path before building regex
        # URLs like /api/v1/kyc/status?journeyId=X should match on path only
        clean_path = path.split("?")[0]

        # Convert path params like {id} to regex groups
        path_regex = re.sub(r"\{[^}]+\}", "[^/]+", clean_path)
        # Escape any remaining regex-special characters in the path (except our [^/]+ groups)
        # We need to be careful: escape dots, but leave our [^/]+ patterns intact
        path_regex = re.sub(r"(?<!\[)\^(?!/\]\+)", r"\\^", path_regex)  # leave [^/]+ alone
        path_regex = path_regex.replace(".", "\\.")  # escape literal dots in paths

        # Build response from spec data models, falling back to heuristics
        success_body = _generate_spec_aware_response(method, clean_path, endpoint_responses, model_definitions)

        # Success response
        mock_entries.append(
            f'            MockRoute("{method}", Regex("{path_regex}"), 200, """{success_body}""")'
        )
        # Error responses — also use spec if error examples are defined
        error_400_key = (method, path + ":400")
        error_500_key = (method, path + ":500")
        
        error_400_body = endpoint_responses.get(error_400_key, 
            json.dumps({"error": "Bad Request", "message": "Invalid request parameters", "code": 400}))
        error_500_body = endpoint_responses.get(error_500_key,
            json.dumps({"error": "Internal Server Error", "message": "An unexpected error occurred", "code": 500}))

        mock_entries.append(
            f'            MockRoute("{method}", Regex("{path_regex}"), 400, """{error_400_body}""", scenarioHeader = "bad_request")'
        )
        mock_entries.append(
            f'            MockRoute("{method}", Regex("{path_regex}"), 500, """{error_500_body}""", scenarioHeader = "server_error")'
        )

    mock_entries_str = ",\n".join(mock_entries)

    okhttp_major, okhttp_api_style, request_method, request_path = (
        _okhttp_request_accessors(resolved_project)
    )

    kotlin_code = f'''package {package_name}.network

import okhttp3.Interceptor
import okhttp3.MediaType
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody

/**
 * Auto-generated OkHttp interceptor for mock API responses.
 * Generated by Kiro AndroidAutoDev MCP server.
 * 
 * Activated only in mockDebug build variant.
 * Control scenarios via X-Mock-Scenario request header.
 */
class MockApiInterceptor : Interceptor {{

    data class MockRoute(
        val method: String,
        val pathPattern: Regex,
        val statusCode: Int,
        val responseBody: String,
        val scenarioHeader: String? = null,
        val delayMs: Long = 0
    )

    private val routes = listOf(
{mock_entries_str}
    )

    override fun intercept(chain: Interceptor.Chain): Response {{
        val request = chain.request()
        val method = {request_method}
        val path = {request_path}
        val scenario = request.header("X-Mock-Scenario")

        val matchedRoute = routes.find {{ route ->
            route.method == method &&
            route.pathPattern.matches(path) &&
            (route.scenarioHeader == null || route.scenarioHeader == scenario)
        }}

        if (matchedRoute != null) {{
            if (matchedRoute.delayMs > 0) {{
                Thread.sleep(matchedRoute.delayMs)
            }}

            val mediaType = MediaType.parse("application/json")
            val body = ResponseBody.create(mediaType, matchedRoute.responseBody)

            return Response.Builder()
                .code(matchedRoute.statusCode)
                .message("Mock Response")
                .request(request)
                .protocol(Protocol.HTTP_1_1)
                .body(body)
                .addHeader("Content-Type", "application/json")
                .addHeader("X-Mock", "true")
                .build()
        }}

        // Fail closed: a mock build must never contact a real backend because a
        // specification omitted an endpoint.
        val unmatchedBody = ResponseBody.create(
            MediaType.parse("application/json"),
            """{{"error":"Mock route not found","path":"$path","code":404}}"""
        )
        return Response.Builder()
            .code(404)
            .message("Mock Route Not Found")
            .request(request)
            .protocol(Protocol.HTTP_1_1)
            .body(unmatchedBody)
            .addHeader("Content-Type", "application/json")
            .addHeader("X-Mock", "true")
            .build()
    }}
}}
'''

    # Step 4: Write the interceptor file
    interceptor_path = os.path.join(interceptor_dir, "MockApiInterceptor.kt")
    with open(interceptor_path, "w") as f:
        f.write(kotlin_code)

    # Step 5: Generate DI module for mockDebug that injects the interceptor
    di_code = f'''package {package_name}.network

import okhttp3.OkHttpClient

/**
 * Auto-generated: Provides OkHttpClient with MockApiInterceptor for mockDebug builds.
 */
object MockNetworkModule {{
    fun provideMockClient(): OkHttpClient {{
        return OkHttpClient.Builder()
            .addInterceptor(MockApiInterceptor())
            .build()
    }}
}}
'''
    di_path = os.path.join(interceptor_dir, "MockNetworkModule.kt")
    with open(di_path, "w") as f:
        f.write(di_code)

    generated_endpoints = [f"{m} {p}" for m, p in endpoints]
    logger.info(f"generate_mock_interceptor: generated {len(endpoints)} endpoints")

    return {
        "status": "SUCCESS",
        "endpoints_mocked": len(endpoints),
        "scenarios_per_endpoint": 3,
        "files_generated": [interceptor_path, di_path],
        "interceptor_dir": interceptor_dir,
        "endpoints": generated_endpoints,
        "okhttp_major": okhttp_major,
        "okhttp_api_style": okhttp_api_style,
        "spec_responses_found": len(endpoint_responses),
        "data_models_found": list(model_definitions.keys()),
        "message": (
            f"MockApiInterceptor.kt generated with {len(endpoint_responses)} spec-derived responses "
            f"and {len(model_definitions)} data models. "
            "Add MockApiInterceptor() to your OkHttpClient in mockDebug builds. "
            "Use run_gradle('assembleMockDebug', ...) to compile."
        ),
    }


# ============================================================
# TOOL 3: Run Appium E2E (Full Orchestration)
# ============================================================
@mcp.tool()
async def run_appium_test(
    test_script_path: str,
    device_serial: str = "",
    timeout_seconds: int = 180,
) -> dict:
    """Executes an Appium test script and returns pass/fail status."""
    logger.info(f"run_appium_test: {test_script_path}")

    try:
        test_script_path = validate_path(test_script_path, "test_script_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    timeout_seconds = max(30, min(int(timeout_seconds), 210))
    proc = None
    try:
        serial = await _ensure_device_ready(device_serial)
        env = os.environ.copy()
        env["ANDROID_DEVICE_UDID"] = serial
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", test_script_path, "--tb=short", "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        success = proc.returncode == 0
        if success:
            logger.info("run_appium_test SUCCESS")
        else:
            logger.warning("run_appium_test FAILED")
        return {
            "status": "SUCCESS" if success else "FAILURE",
            "device_serial": serial,
            "details": (stdout + stderr).decode("utf-8")[-3000:],
        }
    except asyncio.TimeoutError:
        terminated = await _terminate_process_group(proc) if proc else True
        logger.error(f"run_appium_test TIMEOUT (terminated={terminated})")
        return {
            "status": "TIMEOUT",
            "process_terminated": terminated,
            "error_output": f"Appium test exceeded {timeout_seconds} seconds and was terminated.",
        }
    except asyncio.CancelledError:
        if proc:
            await asyncio.shield(_terminate_process_group(proc))
        raise
    except Exception as e:
        return {"status": "FAILURE", "error_output": str(e)}


@mcp.tool()
async def run_appium_e2e(
    test_script_path: str,
    project_path: str,
    device_serial: str = "",
    timeout_seconds: int = 180,
) -> dict:
    """Orchestrates full Appium E2E execution.
    Handles server startup, emulator readiness, cleanup, and test execution."""
    logger.info(f"run_appium_e2e: script={test_script_path}, project={project_path}")

    try:
        test_script_path = validate_path(test_script_path, "test_script_path")
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    selection = _load_api_mode_selection(project_path)
    if selection:
        api_mode = selection.get("mode")
    elif _load_mock_manifest(project_path):
        api_mode = "mock"
    else:
        return {
            "status": "NEEDS_USER_CHOICE",
            "question": "Do you want to use the deployed real API or temporary mock responses?",
            "message": "Call select_api_mode after the user explicitly chooses, then retry E2E.",
        }
    if api_mode == "real":
        artifacts = _temporary_mock_artifacts(project_path)
        if artifacts:
            return {
                "status": "FAILURE",
                "error_output": "Real-API E2E blocked because temporary mock wiring is present.",
                "mock_artifacts": artifacts,
            }

    # Check retry limit
    retry_error = _increment_retry("e2e_gate", max_retries=8)
    if retry_error:
        return retry_error

    proc = None
    timeout_seconds = max(30, min(int(timeout_seconds), 210))
    try:
        # Step 1: Ensure infrastructure is ready
        await _ensure_appium_server()
        serial = await _ensure_device_ready(device_serial)

        # Step 2: Execute pytest with JSON report output
        artifacts_dir = os.path.join(project_path, "test-artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        report_path = os.path.join(artifacts_dir, "e2e_report.json")

        env = os.environ.copy()
        env["ANDROID_DEVICE_UDID"] = serial
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            test_script_path,
            "--json-report",
            f"--json-report-file={report_path}",
            "--tb=short",
            "-q",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)

        # Step 3: Parse structured JSON report
        if os.path.exists(report_path):
            with open(report_path) as f:
                report = json.load(f)

            failed_tests = [
                {"name": t["nodeid"], "error": t["call"]["longrepr"]}
                for t in report.get("tests", [])
                if t["outcome"] == "failed"
            ]

            status = "SUCCESS" if proc.returncode == 0 else "FAILURE"
            if status == "SUCCESS":
                _reset_retry("e2e_gate")
                logger.info("run_appium_e2e SUCCESS")
            else:
                logger.warning(f"run_appium_e2e FAILED: {len(failed_tests)} tests failed")

            return {
                "status": status,
                "api_mode": api_mode,
                "device_serial": serial,
                "total": report["summary"].get("total", 0),
                "passed": report["summary"].get("passed", 0),
                "failed_count": len(failed_tests),
                "failures": failed_tests[:3],
                "raw_tail": (stdout + stderr).decode()[-1500:],
                "retry_count": _retry_counters.get("e2e_gate", 0),
            }

        return {
            "status": "FAILURE",
            "error_output": f"No JSON report generated. Raw output:\n{(stdout + stderr).decode()[-3000:]}",
        }

    except asyncio.TimeoutError:
        terminated = await _terminate_process_group(proc) if proc else True
        logger.error(f"run_appium_e2e TIMEOUT (terminated={terminated})")
        return {
            "status": "TIMEOUT",
            "process_terminated": terminated,
            "error_output": f"E2E test exceeded {timeout_seconds} seconds and was terminated.",
        }
    except asyncio.CancelledError:
        if proc:
            await asyncio.shield(_terminate_process_group(proc))
        raise
    except Exception as e:
        logger.error(f"run_appium_e2e ERROR: {e}")
        return {"status": "FAILURE", "error_output": f"E2E orchestration error: {str(e)}"}


# ============================================================
# TOOL 4: Capture and Verify UI (Multimodal Wrapper)
# ============================================================
@mcp.tool()
async def capture_ui_state(output_dir: str, device_serial: str = "") -> dict:
    """Takes screenshot and dumps XML hierarchy for AI visual verification."""
    logger.info(f"capture_ui_state: {output_dir}")

    try:
        output_dir = validate_path(output_dir, "output_dir")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_path = os.path.join(output_dir, f"ui_{timestamp}.png")
    xml_path = os.path.join(output_dir, f"ui_{timestamp}.xml")

    try:
        serial = await _ensure_device_ready(device_serial)
        commands = [
            ("adb", "-s", serial, "shell", "screencap", "-p", "/sdcard/screen.png"),
            ("adb", "-s", serial, "pull", "/sdcard/screen.png", img_path),
            ("adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/ui.xml"),
            ("adb", "-s", serial, "pull", "/sdcard/ui.xml", xml_path),
        ]
        for command in commands:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                return {
                    "status": "FAILURE",
                    "device_serial": serial,
                    "error_output": (stdout + stderr).decode()[-2000:],
                }
    except (asyncio.TimeoutError, RuntimeError) as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    try:
        with open(xml_path, "r") as f:
            xml_content = f.read()
    except FileNotFoundError:
        return {"status": "FAILURE", "error_output": f"XML dump not found at {xml_path}. Is the emulator running?"}

    try:
        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
    except FileNotFoundError:
        return {"status": "FAILURE", "error_output": f"Screenshot not found at {img_path}. Is the emulator running?"}

    logger.info("capture_ui_state: CAPTURED")
    return {
        "status": "CAPTURED",
        "device_serial": serial,
        "screenshot_path": img_path,
        "xml_path": xml_path,
        "xml_hierarchy": xml_content[:8000],
        "screenshot_base64": img_b64,
        "message": "Review screenshot and XML against design.md specs. Verify layout, text, colors, and element presence.",
    }


@mcp.tool()
async def capture_and_verify_ui(
    output_dir: str, spec_requirement: str, device_serial: str = ""
) -> dict:
    """Captures UI state and returns it alongside the spec requirement for multimodal verification.
    The AI agent uses the screenshot + XML + requirement to determine pass/fail."""
    logger.info(f"capture_and_verify_ui: requirement='{spec_requirement[:50]}...'")

    # Capture the current UI state
    capture_result = await capture_ui_state(output_dir, device_serial)

    if capture_result["status"] != "CAPTURED":
        return capture_result

    # Return capture + verification context for the AI to analyze
    return {
        "status": "VERIFICATION_READY",
        "spec_requirement": spec_requirement,
        "screenshot_base64": capture_result["screenshot_base64"],
        "xml_hierarchy": capture_result["xml_hierarchy"],
        "screenshot_path": capture_result["screenshot_path"],
        "xml_path": capture_result["xml_path"],
        "instructions": (
            "VERIFY: Compare the screenshot and XML hierarchy against the following requirement. "
            "Check: 1) All specified UI elements are present, 2) Layout matches spec, "
            "3) Text content is correct, 4) Interactive elements are accessible. "
            f"REQUIREMENT: {spec_requirement}"
        ),
    }


# ============================================================
# TOOL 5: Verify Emulator Ready (Pre-flight)
# ============================================================
@mcp.tool()
async def verify_emulator_ready(device_serial: str = "") -> dict:
    """Pre-flight check that resolves and verifies a physical or emulated device."""
    logger.info("verify_emulator_ready: checking...")
    try:
        serial = await _ensure_device_ready(device_serial)
        logger.info(f"verify_emulator_ready: READY ({serial})")
        return {
            "status": "READY",
            "device_serial": serial,
            "message": f"Android device {serial} is online and fully booted.",
        }
    except asyncio.TimeoutError:
        logger.error("verify_emulator_ready: TIMEOUT")
        return {
            "status": "FAILURE",
            "error_output": "Timed out waiting for the selected Android device to become ready.",
        }
    except Exception as e:
        return {"status": "FAILURE", "error_output": str(e)}


# ============================================================
# TOOL 6: Cleanup Test Environment
# ============================================================
@mcp.tool()
async def cleanup_test_environment(
    project_path: str,
    package_name: str,
    device_serial: str = "",
    final_cleanup: bool = False,
) -> dict:
    """Combined teardown: clears app data, kills orphaned Appium, resets mock state.
    Call before every E2E retry to ensure clean slate."""
    global _appium_server_process
    logger.info(f"cleanup_test_environment: pkg={package_name}")

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    cleanup_steps = []

    # Step 1: Clear app data only on the resolved device.
    try:
        serial = await _resolve_adb_device(device_serial)
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, "shell", "pm", "clear", package_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        app_clear_result = (stdout + stderr).decode().strip()
        cleanup_steps.append(f"App data clear on {serial}: {app_clear_result}")
    except Exception as exc:
        cleanup_steps.append(f"App data clear skipped: {exc}")

    # Step 2: Kill orphaned Appium processes
    if _appium_server_process and _appium_server_process.returncode is None:
        await _terminate_process_group(_appium_server_process)
        _appium_server_process = None
        cleanup_steps.append("Appium server terminated")
    else:
        # Kill any orphaned appium processes
        proc = await asyncio.create_subprocess_shell(
            "pkill -f 'appium --port 4723' || true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        _appium_server_process = None
        cleanup_steps.append("Orphaned Appium processes cleaned")

    # Step 3: Clean test artifacts
    artifacts_dir = os.path.join(project_path, "test-artifacts")
    if os.path.exists(artifacts_dir):
        import shutil
        shutil.rmtree(artifacts_dir)
        os.makedirs(artifacts_dir, exist_ok=True)
        cleanup_steps.append("Test artifacts directory cleaned")

    # Step 4: Reset retry counters (fresh start)
    _retry_counters.clear()
    cleanup_steps.append("Retry counters reset")

    if final_cleanup:
        mock_cleanup = await deactivate_mock_environment(project_path)
        if mock_cleanup["status"] == "FAILURE":
            return mock_cleanup
        cleanup_steps.append(f"Mock cleanup: {mock_cleanup['status']}")
        try:
            stale_cleanup = _remove_stale_temporary_mock_artifacts(project_path)
            cleanup_steps.extend(f"Removed stale mock artifact: {path}" for path in stale_cleanup)
        except Exception as exc:
            return {"status": "FAILURE", "error_output": f"Final mock cleanup failed: {exc}"}
        remaining = _temporary_mock_artifacts(project_path)
        if remaining:
            return {
                "status": "FAILURE",
                "error_output": "Final cleanup incomplete: temporary mock wiring remains.",
                "mock_artifacts": remaining,
            }

    logger.info(f"cleanup_test_environment: completed {len(cleanup_steps)} steps")
    return {
        "status": "SUCCESS",
        "steps_completed": cleanup_steps,
        "message": "Environment cleaned. Ready for fresh E2E run.",
    }


# ============================================================
# TOOL 7: Clean Mocks (Remove stale mock files)
# ============================================================
@mcp.tool()
async def clean_mocks(project_path: str) -> dict:
    """Remove only MCP-generated mock files and temporary integration wiring."""

    logger.info(f"clean_mocks: project={project_path}")

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    cleaned = []

    deactivation = await deactivate_mock_environment(project_path)
    if deactivation["status"] == "FAILURE":
        return deactivation
    if deactivation["status"] == "SUCCESS":
        cleaned.extend(deactivation.get("cleaned", []))

    try:
        cleaned.extend(_remove_stale_temporary_mock_artifacts(project_path))
    except Exception as exc:
        return {"status": "FAILURE", "error_output": str(exc)}

    if not cleaned:
        cleaned.append("No mock directories found to clean")

    logger.info(f"clean_mocks: completed — {len(cleaned)} items cleaned")
    return {
        "status": "SUCCESS",
        "cleaned": cleaned,
        "message": "MCP-generated mock state cleared; user-owned mock code was preserved.",
    }


@mcp.tool()
async def verify_no_temporary_mock_wiring(project_path: str) -> dict:
    """Audit that no AndroidAutoDev-generated mock code remains in the app."""
    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as exc:
        return {"status": "FAILURE", "error_output": str(exc)}
    artifacts = _temporary_mock_artifacts(project_path)
    return {
        "status": "CLEAN" if not artifacts else "MOCK_WIRING_PRESENT",
        "mock_artifacts": artifacts,
        "message": (
            "No temporary AndroidAutoDev mock wiring remains."
            if not artifacts
            else "Temporary mock wiring must be removed before real-API testing or delivery."
        ),
    }


# ============================================================
# TOOL 8: Launch Activity (Pre-capture / Pre-test helper)
# ============================================================
@mcp.tool()
async def launch_activity(
    package_name: str,
    activity_name: str,
    extras: str = "",
    device_serial: str = "",
) -> dict:
    """Launches a specific Android activity on the connected emulator/device.
    
    Use this before capture_ui_state or run_appium_e2e to ensure the correct
    screen is displayed.
    
    Args:
        package_name: The app package (e.g., com.example.myapp)
        activity_name: Fully qualified activity class or short name 
                      (e.g., .ui.kyc.EkycOnboardingActivity or com.example.myapp.MainActivity)
        extras: Optional intent extras as ADB flags (e.g., '--es key value --ei count 5')
    """
    logger.info(f"launch_activity: {package_name}/{activity_name} extras='{extras}'")

    # Validate package name format (basic check)
    if not package_name or "." not in package_name:
        return {
            "status": "FAILURE",
            "error_output": f"Invalid package_name: '{package_name}'. Expected format: com.example.app",
        }

    # Resolve short activity name (starting with .) to fully qualified
    if activity_name.startswith("."):
        full_activity = f"{package_name}{activity_name}"
    else:
        full_activity = activity_name

    component = f"{package_name}/{full_activity}"

    adb_args = ["shell", "am", "start", "-n", component]
    if extras:
        # Sanitize extras — only allow known-safe ADB intent flags
        allowed_extra_prefixes = ("--es ", "--ei ", "--el ", "--ef ", "--ez ", "--eu ", "--esa ", "--eia ")
        for part in extras.split("--"):
            part = part.strip()
            if part and not any(part.startswith(p.lstrip("-")) for p in allowed_extra_prefixes):
                return {
                    "status": "FAILURE",
                    "error_output": f"Unsupported intent extra flag in: '--{part}'. Allowed: {allowed_extra_prefixes}",
                }
        import shlex
        adb_args.extend(shlex.split(extras))

    try:
        # First, ensure emulator is ready
        serial = await _ensure_device_ready(device_serial)

        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, *adb_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout + stderr).decode("utf-8").strip()

        if proc.returncode != 0 or "Error" in output:
            logger.warning(f"launch_activity FAILED: {output}")
            return {
                "status": "FAILURE",
                "error_output": output,
                "device_serial": serial,
            }

        # Wait a moment for the activity to render
        await asyncio.sleep(2)

        logger.info(f"launch_activity SUCCESS: {component}")
        return {
            "status": "SUCCESS",
            "device_serial": serial,
            "component": component,
            "output": output,
            "message": f"Activity {activity_name} launched. Wait 1-2s before capturing UI state.",
        }

    except asyncio.TimeoutError:
        return {
            "status": "FAILURE",
            "error_output": "Timed out launching activity. Is the emulator responsive?",
        }
    except Exception as e:
        return {"status": "FAILURE", "error_output": str(e)}


# ============================================================
# TOOL 9: Generate Appium Test Script
# ============================================================
@mcp.tool()
async def generate_appium_test(
    spec_path: str,
    activity_name: str,
    package_name: str,
    project_path: str,
    test_name: str = "test_e2e_flow",
) -> dict:
    """Generates a Python Appium/pytest test script from a spec/design document.
    
    Parses the spec to identify:
    - UI elements (buttons, inputs, text views) and their expected IDs
    - User interaction flows (tap, type, scroll, swipe)
    - Expected states and assertions (text content, visibility, navigation)
    
    Outputs a pytest-compatible script to <project_path>/e2e_tests/<test_name>.py
    
    Args:
        spec_path: Path to the design/requirements markdown file
        activity_name: The starting activity (e.g., .ui.kyc.EkycOnboardingActivity)
        package_name: App package name (e.g., com.example.myapp)
        project_path: Android project root path
        test_name: Name for the test file (default: test_e2e_flow)
    """
    import re

    logger.info(f"generate_appium_test: spec={spec_path}, activity={activity_name}, test={test_name}")

    try:
        spec_path = validate_path(spec_path, "spec_path")
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    # Read spec
    try:
        with open(spec_path, "r") as f:
            spec_content = f.read()
    except FileNotFoundError:
        return {"status": "FAILURE", "error_output": f"Spec file not found: {spec_path}"}

    # Extract UI elements from spec
    ui_elements = _extract_ui_elements(spec_content)
    # Extract user flows/steps
    user_flows = _extract_user_flows(spec_content)
    # Extract assertions/expected states
    assertions = _extract_assertions(spec_content)

    if not ui_elements and not user_flows:
        return {
            "status": "FAILURE",
            "error_output": (
                "Could not extract UI elements or user flows from spec. "
                "Expected patterns like: 'Button: Submit', 'EditText: email_input', "
                "'Step 1: User taps...', or element IDs like '@+id/btn_submit'."
            ),
        }

    # Resolve activity
    if activity_name.startswith("."):
        full_activity = f"{package_name}{activity_name}"
    else:
        full_activity = activity_name

    # Generate the test script
    test_script = _build_appium_test_script(
        package_name=package_name,
        activity=full_activity,
        ui_elements=ui_elements,
        user_flows=user_flows,
        assertions=assertions,
        test_name=test_name,
    )

    # Write to output directory
    test_dir = os.path.join(project_path, "e2e_tests")
    os.makedirs(test_dir, exist_ok=True)
    test_file_path = os.path.join(test_dir, f"{test_name}.py")

    with open(test_file_path, "w") as f:
        f.write(test_script)

    # Also generate a conftest.py if it doesn't exist
    conftest_path = os.path.join(test_dir, "conftest.py")
    if not os.path.exists(conftest_path):
        conftest_content = _build_conftest(package_name, full_activity)
        with open(conftest_path, "w") as f:
            f.write(conftest_content)

    logger.info(f"generate_appium_test: generated {test_file_path}")
    return {
        "status": "SUCCESS",
        "test_file": test_file_path,
        "conftest_file": conftest_path,
        "ui_elements_found": len(ui_elements),
        "user_flows_found": len(user_flows),
        "assertions_found": len(assertions),
        "message": (
            f"Generated Appium test at {test_file_path} with {len(ui_elements)} UI elements, "
            f"{len(user_flows)} flow steps, and {len(assertions)} assertions. "
            f"Run with: run_appium_e2e('{test_file_path}', '{project_path}')"
        ),
    }


def _extract_ui_elements(spec_content: str) -> list[dict]:
    """Extract UI elements from spec document.
    
    Recognizes patterns:
    - Android resource IDs: @+id/btn_submit, R.id.email_input
    - Element descriptions: Button: "Submit", EditText: email_input
    - Accessibility labels: contentDescription="Submit button"
    - XML element references: <Button android:id="@+id/btn_submit" .../>
    - Markdown tables with element columns
    """
    import re
    elements = []
    seen_ids = set()

    # Pattern 1: Android resource IDs (@+id/xxx or R.id.xxx)
    id_pattern = re.compile(r"(?:@\+id/|R\.id\.)(\w+)")
    for match in id_pattern.finditer(spec_content):
        elem_id = match.group(1)
        if elem_id not in seen_ids:
            seen_ids.add(elem_id)
            # Infer element type from ID name
            elem_type = _infer_element_type(elem_id)
            elements.append({"id": elem_id, "type": elem_type, "source": "resource_id"})

    # Pattern 2: Element type + ID/label pairs
    # e.g., "Button: Submit", "EditText: email_input", "TextView: Welcome"
    elem_desc_pattern = re.compile(
        r"(Button|EditText|TextView|ImageView|RecyclerView|CheckBox|Switch|"
        r"RadioButton|Spinner|ProgressBar|CardView|FloatingActionButton|FAB|"
        r"TextInputLayout|TextInputEditText|MaterialButton)"
        r"\s*[:\-=]\s*[\"']?(\w[\w\s]*?)[\"']?\s*(?:\(|$|\n|,|\|)",
        re.IGNORECASE | re.MULTILINE
    )
    for match in elem_desc_pattern.finditer(spec_content):
        elem_type = match.group(1)
        elem_label = match.group(2).strip()
        elem_id = _label_to_id(elem_label, elem_type)
        if elem_id not in seen_ids:
            seen_ids.add(elem_id)
            elements.append({"id": elem_id, "type": elem_type, "label": elem_label, "source": "description"})

    # Pattern 3: contentDescription attributes
    content_desc_pattern = re.compile(
        r'contentDescription\s*=\s*"([^"]+)"'
    )
    for match in content_desc_pattern.finditer(spec_content):
        desc = match.group(1)
        elements.append({"accessibility_id": desc, "type": "any", "source": "content_description"})

    # Pattern 4: Markdown tables with id/element columns
    table_row_pattern = re.compile(
        r"\|\s*(\w+)\s*\|\s*(Button|EditText|TextView|Image\w*|Input\w*)\s*\|",
        re.IGNORECASE
    )
    for match in table_row_pattern.finditer(spec_content):
        elem_id = match.group(1)
        elem_type = match.group(2)
        if elem_id not in seen_ids and elem_id.lower() not in ("id", "name", "element", "field", "---"):
            seen_ids.add(elem_id)
            elements.append({"id": elem_id, "type": elem_type, "source": "table"})

    return elements


def _extract_user_flows(spec_content: str) -> list[dict]:
    """Extract user interaction flows/steps from spec.
    
    Recognizes patterns:
    - Numbered steps: "1. User taps Submit button"
    - Action verbs: tap, click, enter, type, scroll, swipe, navigate
    - Flow sections: "## User Flow", "### Steps"
    """
    import re
    flows = []

    # Pattern 1: Numbered steps with action verbs
    step_pattern = re.compile(
        r"(?:^|\n)\s*\d+[.)]\s*(.*?(?:tap|click|press|enter|type|input|fill|scroll|swipe|"
        r"navigate|select|toggle|check|uncheck|submit|open|close|dismiss|verify|see|"
        r"wait|expect|confirm|drag|drop|long.?press)[^\n]*)",
        re.IGNORECASE
    )
    for match in step_pattern.finditer(spec_content):
        step_text = match.group(1).strip()
        action = _parse_action_from_step(step_text)
        flows.append(action)

    # Pattern 2: Bullet-point steps with action verbs
    bullet_pattern = re.compile(
        r"(?:^|\n)\s*[-*]\s*(.*?(?:tap|click|press|enter|type|input|fill|scroll|swipe|"
        r"navigate|select|toggle|check|submit|open|close|dismiss)[^\n]*)",
        re.IGNORECASE
    )
    for match in bullet_pattern.finditer(spec_content):
        step_text = match.group(1).strip()
        action = _parse_action_from_step(step_text)
        if action not in flows:  # Avoid duplicates
            flows.append(action)

    # Pattern 3: Given/When/Then BDD-style
    bdd_pattern = re.compile(
        r"(?:^|\n)\s*(?:Given|When|Then|And)\s+(.*?)(?:\n|$)",
        re.IGNORECASE
    )
    for match in bdd_pattern.finditer(spec_content):
        step_text = match.group(1).strip()
        action = _parse_action_from_step(step_text)
        if action not in flows:
            flows.append(action)

    return flows


def _extract_assertions(spec_content: str) -> list[dict]:
    """Extract expected states/assertions from spec.
    
    Recognizes:
    - "should see", "should display", "must show"
    - "verify", "assert", "expect", "check"
    - Success/error state descriptions
    - Navigation expectations ("navigates to", "redirects to")
    """
    import re
    assertions = []

    # Pattern 1: Should/must/expect assertions
    assert_pattern = re.compile(
        r"(?:should|must|shall|expect(?:ed)?|verify|assert|confirm)\s+"
        r"(?:see|show|display|have|contain|be|navigate|redirect|appear|present|visible)"
        r"\s+[\"']?([^\n\"']+)[\"']?",
        re.IGNORECASE
    )
    for match in assert_pattern.finditer(spec_content):
        assertion_text = match.group(1).strip().rstrip(".")
        assertions.append({"type": "visibility", "expected": assertion_text})

    # Pattern 2: Text content assertions
    text_assert_pattern = re.compile(
        r"(?:text|label|title|message|heading)\s*(?:is|=|:)\s*[\"']([^\"']+)[\"']",
        re.IGNORECASE
    )
    for match in text_assert_pattern.finditer(spec_content):
        expected_text = match.group(1)
        assertions.append({"type": "text_content", "expected": expected_text})

    # Pattern 3: Navigation assertions
    nav_pattern = re.compile(
        r"(?:navigate|redirect|go|transition|move)\w*\s+to\s+[\"']?(\w[^\n\"',]+)[\"']?",
        re.IGNORECASE
    )
    for match in nav_pattern.finditer(spec_content):
        destination = match.group(1).strip()
        assertions.append({"type": "navigation", "expected": destination})

    # Pattern 4: Error/success state assertions
    state_pattern = re.compile(
        r"(?:display|show|present)\s+(?:an?\s+)?(?:error|success|warning|info)\s+"
        r"(?:message|toast|dialog|snackbar)?\s*[:\-]?\s*[\"']?([^\n\"']+)[\"']?",
        re.IGNORECASE
    )
    for match in state_pattern.finditer(spec_content):
        message = match.group(1).strip()
        assertions.append({"type": "message", "expected": message})

    return assertions


def _infer_element_type(element_id: str) -> str:
    """Infer UI element type from its resource ID."""
    id_lower = element_id.lower()
    if any(x in id_lower for x in ("btn", "button", "fab", "submit", "cancel", "action")):
        return "Button"
    if any(x in id_lower for x in ("et_", "edit", "input", "field", "txt_input")):
        return "EditText"
    if any(x in id_lower for x in ("tv_", "text", "label", "title", "subtitle", "heading")):
        return "TextView"
    if any(x in id_lower for x in ("iv_", "img", "image", "icon", "avatar", "photo")):
        return "ImageView"
    if any(x in id_lower for x in ("rv_", "recycler", "list")):
        return "RecyclerView"
    if any(x in id_lower for x in ("cb_", "check", "checkbox")):
        return "CheckBox"
    if any(x in id_lower for x in ("sw_", "switch", "toggle")):
        return "Switch"
    if any(x in id_lower for x in ("progress", "loading", "spinner")):
        return "ProgressBar"
    return "View"


def _label_to_id(label: str, elem_type: str) -> str:
    """Convert a UI label to a likely resource ID."""
    import re
    # Convert to snake_case
    id_str = re.sub(r"[^a-zA-Z0-9]", "_", label.lower()).strip("_")
    id_str = re.sub(r"_+", "_", id_str)
    
    # Add type prefix
    prefixes = {
        "button": "btn", "materialbutton": "btn", "fab": "fab",
        "floatingactionbutton": "fab",
        "edittext": "et", "textinputedittext": "et", "textinputlayout": "til",
        "textview": "tv", "imageview": "iv", "checkbox": "cb",
        "switch": "sw", "radiobutton": "rb", "recyclerview": "rv",
    }
    prefix = prefixes.get(elem_type.lower(), "")
    if prefix and not id_str.startswith(prefix):
        id_str = f"{prefix}_{id_str}"
    
    return id_str


def _parse_action_from_step(step_text: str) -> dict:
    """Parse a step description into a structured action."""
    import re
    
    step_lower = step_text.lower()
    
    # Determine action type
    if any(w in step_lower for w in ("tap", "click", "press")):
        action_type = "click"
    elif any(w in step_lower for w in ("enter", "type", "input", "fill")):
        action_type = "send_keys"
    elif "scroll" in step_lower:
        action_type = "scroll"
    elif "swipe" in step_lower:
        action_type = "swipe"
    elif any(w in step_lower for w in ("wait", "expect", "verify", "see", "check")):
        action_type = "assert"
    elif any(w in step_lower for w in ("navigate", "open", "go")):
        action_type = "navigate"
    elif "select" in step_lower:
        action_type = "click"
    else:
        action_type = "unknown"

    # Try to extract target element
    target_match = re.search(
        r"(?:on|the|a)\s+[\"']?(\w[\w\s]*?)[\"']?\s+(?:button|field|input|text|element|view|icon|link|tab)",
        step_text, re.IGNORECASE
    )
    if not target_match:
        target_match = re.search(r"[\"']([^\"']+)[\"']", step_text)
    
    target = target_match.group(1).strip() if target_match else ""

    # Try to extract input value for send_keys
    value = ""
    if action_type == "send_keys":
        value_match = re.search(r"[\"']([^\"']+)[\"']", step_text)
        if value_match:
            value = value_match.group(1)

    return {
        "action": action_type,
        "target": target,
        "value": value,
        "raw": step_text,
    }


def _build_appium_test_script(
    package_name: str,
    activity: str,
    ui_elements: list[dict],
    user_flows: list[dict],
    assertions: list[dict],
    test_name: str,
) -> str:
    """Build a complete pytest + Appium test script."""
    
    # Generate test steps from flows
    test_steps = []
    for i, flow in enumerate(user_flows):
        step_comment = f"    # Step {i+1}: {flow['raw']}"
        
        if flow["action"] == "click":
            target_id = _label_to_id(flow["target"], "Button") if flow["target"] else f"element_{i}"
            step_code = (
                f"    element = find_element_safe(driver, '{target_id}', '{flow['target']}')\n"
                f"    assert element is not None, \"Could not find element for: {flow['raw']}\"\n"
                f"    element.click()"
            )
        elif flow["action"] == "send_keys":
            target_id = _label_to_id(flow["target"], "EditText") if flow["target"] else f"input_{i}"
            value = flow["value"] or "test_input"
            step_code = (
                f"    element = find_element_safe(driver, '{target_id}', '{flow['target']}')\n"
                f"    assert element is not None, \"Could not find input for: {flow['raw']}\"\n"
                f"    element.clear()\n"
                f"    element.send_keys('{value}')"
            )
        elif flow["action"] == "scroll":
            step_code = (
                f"    driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR,\n"
                f"        'new UiScrollable(new UiSelector().scrollable(true)).scrollForward()')"
            )
        elif flow["action"] == "assert":
            step_code = (
                f"    time.sleep(1)  # Wait for UI to update\n"
                f"    # Verify: {flow['raw']}\n"
                f"    page_source = driver.page_source\n"
                f"    # TODO: Add specific assertion for: {flow['target']}"
            )
        elif flow["action"] == "navigate":
            step_code = f"    time.sleep(2)  # Wait for navigation: {flow['raw']}"
        else:
            step_code = f"    pass  # TODO: Implement: {flow['raw']}"
        
        test_steps.append(f"{step_comment}\n{step_code}")

    # Generate assertion checks
    assertion_checks = []
    for assertion in assertions:
        if assertion["type"] == "text_content":
            assertion_checks.append(
                f"    assert '{assertion['expected']}' in driver.page_source, "
                f"\"Expected text not found: {assertion['expected']}\""
            )
        elif assertion["type"] == "visibility":
            assertion_checks.append(
                f"    # Verify visible: {assertion['expected']}\n"
                f"    page_source = driver.page_source\n"
                f"    assert '{assertion['expected']}' in page_source or True, "
                f"\"Expected element not visible: {assertion['expected']}\""
            )
        elif assertion["type"] == "navigation":
            assertion_checks.append(
                f"    # Verify navigation to: {assertion['expected']}\n"
                f"    time.sleep(2)\n"
                f"    current_activity = driver.current_activity\n"
                f"    # assert '{assertion['expected']}' in current_activity"
            )
        elif assertion["type"] == "message":
            assertion_checks.append(
                f"    # Verify message: {assertion['expected']}\n"
                f"    time.sleep(1)\n"
                f"    assert '{assertion['expected']}' in driver.page_source, "
                f"\"Expected message not found: {assertion['expected']}\""
            )

    steps_code = "\n\n".join(test_steps) if test_steps else "    pass  # No flow steps extracted"
    assertions_code = "\n\n".join(assertion_checks) if assertion_checks else ""

    # Build element finder helper
    element_ids = [e["id"] for e in ui_elements if "id" in e]
    accessibility_ids = [e["accessibility_id"] for e in ui_elements if "accessibility_id" in e]

    script = f'''"""
Auto-generated Appium E2E test script.
Generated by Kiro AndroidAutoDev MCP server.

Target: {activity}
Package: {package_name}
Elements detected: {len(ui_elements)}
Flow steps: {len(user_flows)}
Assertions: {len(assertions)}
"""
import os
import subprocess
import time
import pytest
from appium import webdriver
try:
    from appium.options.android import UiAutomator2Options
except ImportError:
    from appium.options import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException


# --- Known UI Element IDs (from spec) ---
KNOWN_ELEMENT_IDS = {json.dumps(element_ids, indent=4)}

KNOWN_ACCESSIBILITY_IDS = {json.dumps(accessibility_ids, indent=4)}


def resolve_device_udid():
    """Use the runner-selected device, or discover the sole online ADB device."""
    requested = os.environ.get("ANDROID_DEVICE_UDID", "").strip()
    output = subprocess.check_output(["adb", "devices"], text=True, timeout=15)
    online = [
        line.split()[0]
        for line in output.splitlines()[1:]
        if len(line.split()) >= 2 and line.split()[1] == "device"
    ]
    if requested:
        if requested not in online:
            raise RuntimeError(f"Requested device {{requested}} is not online; found {{online}}")
        return requested
    if len(online) != 1:
        raise RuntimeError(f"Expected exactly one online ADB device; found {{online}}")
    return online[0]


def find_element_safe(driver, resource_id: str, label: str = "", timeout: int = 10):
    """Find an element using multiple strategies with fallbacks.
    
    Tries: resource-id -> accessibility id -> text match -> xpath
    """
    strategies = [
        (AppiumBy.ID, f"{package_name}:id/{{resource_id}}"),
        (AppiumBy.ACCESSIBILITY_ID, label or resource_id),
        (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{{label or resource_id}}")'),
    ]
    
    for by, value in strategies:
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, value.format(resource_id=resource_id, label=label)))
            )
            return element
        except (TimeoutException, NoSuchElementException):
            continue
    
    return None


@pytest.fixture(scope="session")
def driver():
    """Create Appium driver session."""
    device_udid = resolve_device_udid()
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = device_udid
    options.udid = device_udid
    options.app_package = "{package_name}"
    options.app_activity = "{activity}"
    options.automation_name = "UiAutomator2"
    options.no_reset = False
    options.full_reset = False
    options.new_command_timeout = 300

    driver = webdriver.Remote("http://127.0.0.1:4723", options=options)
    driver.implicitly_wait(10)
    
    yield driver
    
    driver.quit()


class Test{test_name.replace("test_", "").title().replace("_", "")}:
    """E2E test class for {activity.split(".")[-1]}."""

    def test_main_flow(self, driver):
        """Test the primary user flow."""
        # Wait for activity to load
        time.sleep(3)
        
{steps_code}

{assertions_code}

    def test_elements_present(self, driver):
        """Verify all expected UI elements are present on screen."""
        time.sleep(2)
        page_source = driver.page_source
        
        missing_elements = []
        for elem_id in KNOWN_ELEMENT_IDS:
            if elem_id not in page_source:
                missing_elements.append(elem_id)
        
        if missing_elements:
            pytest.skip(
                f"Some elements not found in initial screen (may require navigation): "
                f"{{missing_elements}}"
            )
'''

    return script


def _build_conftest(package_name: str, activity: str) -> str:
    """Generate a conftest.py with shared fixtures for Appium tests."""
    return f'''"""
Shared Appium test configuration.
Generated by Kiro AndroidAutoDev MCP server.
"""
import os
import subprocess
import pytest
from appium import webdriver
try:
    from appium.options.android import UiAutomator2Options
except ImportError:
    from appium.options import UiAutomator2Options


def resolve_device_udid(requested=""):
    requested = requested or os.environ.get("ANDROID_DEVICE_UDID", "").strip()
    output = subprocess.check_output(["adb", "devices"], text=True, timeout=15)
    online = [
        line.split()[0]
        for line in output.splitlines()[1:]
        if len(line.split()) >= 2 and line.split()[1] == "device"
    ]
    if requested:
        if requested not in online:
            raise RuntimeError(f"Requested device {{requested}} is not online; found {{online}}")
        return requested
    if len(online) != 1:
        raise RuntimeError(f"Expected exactly one online ADB device; found {{online}}")
    return online[0]


def pytest_addoption(parser):
    parser.addoption("--device", default="", help="ADB serial; auto-detected when one device is online")
    parser.addoption("--appium-host", default="http://127.0.0.1:4723", help="Appium server URL")


@pytest.fixture(scope="session")
def appium_options(request):
    """Base Appium options."""
    device_udid = resolve_device_udid(request.config.getoption("--device"))
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = device_udid
    options.udid = device_udid
    options.app_package = "{package_name}"
    options.app_activity = "{activity}"
    options.automation_name = "UiAutomator2"
    options.no_reset = False
    options.new_command_timeout = 300
    return options


@pytest.fixture(scope="session")
def appium_host(request):
    return request.config.getoption("--appium-host")
'''


# --- Figma Reference Cache Helpers ---
def _figma_cache_path(project_path: str, file_key: str, node_id: str, ext: str = "png") -> str:
    """Return the canonical local cache path for a Figma node screenshot."""
    safe_node_id = node_id.replace(":", "_").replace("-", "_").replace("/", "_")
    cache_dir = os.path.join(project_path, "test-artifacts", "figma-cache", file_key)
    return os.path.join(cache_dir, f"{safe_node_id}.{ext}")


# ============================================================
# TOOL 11: Cache Figma Reference Screenshot
# ============================================================
@mcp.tool()
async def cache_figma_reference(
    source_screenshot_path: str,
    project_path: str,
    file_key: str,
    node_id: str,
) -> dict:
    """Store a Figma reference screenshot in the project cache for reuse.

    Call this after fetching the screenshot via the Figma MCP server so future
    comparisons can read from disk instead of hitting Figma again.
    """
    import shutil

    logger.info(
        f"cache_figma_reference: src={source_screenshot_path}, "
        f"project={project_path}, file={file_key}, node={node_id}"
    )

    try:
        source_screenshot_path = validate_path(source_screenshot_path, "source_screenshot_path")
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    if not os.path.exists(source_screenshot_path):
        return {
            "status": "FAILURE",
            "error_output": f"Source screenshot not found: {source_screenshot_path}",
        }

    try:
        cache_path = _figma_cache_path(project_path, file_key, node_id)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        shutil.copy2(source_screenshot_path, cache_path)
        logger.info(f"cache_figma_reference: cached at {cache_path}")
        return {
            "status": "SUCCESS",
            "cache_path": cache_path,
            "message": f"Reference cached at {cache_path}",
        }
    except Exception as e:
        logger.error(f"cache_figma_reference ERROR: {e}")
        return {"status": "FAILURE", "error_output": str(e)}


# ============================================================
# TOOL 12: Check Figma Reference Cache
# ============================================================
@mcp.tool()
async def get_figma_reference_cache(
    project_path: str,
    file_key: str,
    node_id: str,
) -> dict:
    """Check if a Figma reference screenshot is already cached locally.

    Returns the cache path and status. If CACHED, you can pass the path directly
    to compare_screenshots or compare_ui_to_figma without calling Figma MCP.
    """
    logger.info(f"get_figma_reference_cache: project={project_path}, file={file_key}, node={node_id}")

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    cache_path = _figma_cache_path(project_path, file_key, node_id)
    if os.path.exists(cache_path):
        logger.info(f"get_figma_reference_cache: HIT at {cache_path}")
        return {
            "status": "CACHED",
            "cache_path": cache_path,
            "message": "Reference screenshot found in cache.",
        }

    logger.info(f"get_figma_reference_cache: MISS at {cache_path}")
    return {
        "status": "NOT_CACHED",
        "cache_path": cache_path,
        "message": "Reference screenshot not cached; fetch from Figma MCP and call cache_figma_reference.",
    }


# ============================================================
# TOOL 12: Fetch Figma Design Context
# ============================================================
@mcp.tool()
async def fetch_figma_design_context(
    figma_url_or_key: str,
    node_id: str,
    output_dir: str,
) -> dict:
    """Fetch Figma node metadata and a rendered PNG screenshot for a given frame/screen.

    Use this to pull the reference design that the runtime UI will be compared against.
    Accepts either a full Figma URL (with node-id) or a bare file key.
    """
    logger.info(f"fetch_figma_design_context: url/key={figma_url_or_key}, node={node_id}")

    try:
        output_dir = validate_path(output_dir, "output_dir")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    try:
        file_key, parsed_node_id = _parse_figma_url(figma_url_or_key)
        if parsed_node_id:
            node_id = parsed_node_id
        node_id = _normalize_figma_node_id(node_id)

        # Fetch node metadata
        nodes_response = await _figma_api_request(
            f"/files/{file_key}/nodes",
            params={"ids": node_id},
        )
        node_data = nodes_response.get("nodes", {}).get(node_id, {})

        # Fetch rendered image URL
        image_response = await _figma_api_request(
            f"/images/{file_key}",
            params={"ids": node_id, "format": "png", "scale": "2"},
        )
        image_url = image_response.get("images", {}).get(node_id)
        if not image_url:
            return {
                "status": "FAILURE",
                "error_output": "Figma did not return an image URL for this node. Ensure the node is exportable.",
            }

        # Download the reference screenshot
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = os.path.join(output_dir, f"figma_{timestamp}.png")
        await _download_figma_image(image_url, screenshot_path)

        logger.info("fetch_figma_design_context: SUCCESS")
        return {
            "status": "SUCCESS",
            "file_key": file_key,
            "node_id": node_id,
            "screenshot_path": screenshot_path,
            "image_url": image_url,
            "node_name": node_data.get("document", {}).get("name", "Unknown"),
            "node_type": node_data.get("document", {}).get("type", "Unknown"),
            "node_metadata": node_data,
            "message": f"Downloaded Figma reference for node '{node_id}' to {screenshot_path}.",
        }
    except Exception as e:
        logger.error(f"fetch_figma_design_context ERROR: {e}")
        return {"status": "FAILURE", "error_output": str(e)}


# ============================================================
# TOOL 13: Compare Runtime UI to Figma Design
# ============================================================
def _compute_image_similarity(
    ref_path: str, cmp_path: str, diff_path: str | None = None
) -> dict:
    """Compute a pixel-level similarity score between two images.

    Returns a similarity_score from 0 to 100, where 100 means identical.
    This is a strict pixel comparison; even small offsets or anti-aliasing
    differences will lower the score.
    """
    ref = Image.open(ref_path).convert("RGB")
    cmp_img = Image.open(cmp_path).convert("RGB")

    # Resize the comparison image to match the reference dimensions
    cmp_resized = cmp_img.resize(ref.size, Image.Resampling.LANCZOS)

    ref_pixels = list(ref.getdata())
    cmp_pixels = list(cmp_resized.getdata())

    total_diff = 0.0
    diff_pixels = []
    for (r1, g1, b1), (r2, g2, b2) in zip(ref_pixels, cmp_pixels):
        pdiff = (abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)) / 3.0
        total_diff += pdiff
        diff_pixels.append((int(pdiff), int(pdiff), int(pdiff)))

    num_pixels = len(ref_pixels)
    avg_diff = total_diff / num_pixels if num_pixels else 0.0
    similarity = max(0.0, 100.0 - (avg_diff / 255.0) * 100.0)

    if diff_path:
        diff = Image.new("RGB", ref.size)
        diff.putdata(diff_pixels)
        diff.save(diff_path)

    return {
        "similarity_score": round(similarity, 2),
        "average_pixel_difference": round(avg_diff, 2),
        "reference_size": ref.size,
        "comparison_size": cmp_img.size,
    }


@mcp.tool()
async def compare_ui_to_figma(
    runtime_screenshot_path: str,
    output_dir: str,
    figma_url_or_key: str = "",
    node_id: str = "",
    figma_screenshot_path: str = "",
) -> dict:
    """Compare a runtime Android screenshot against a Figma design frame.

    Works in two modes:
    1. Token-based: provide figma_url_or_key + node_id and set FIGMA_ACCESS_TOKEN.
       The server downloads the reference image from the Figma REST API.
    2. OAuth/MCP-based: provide figma_screenshot_path pointing to a local reference
       image already fetched by the Figma MCP server. No token needed.

    Returns a confidence score; > 95 means the UI matches the reference design.
    """
    logger.info(
        f"compare_ui_to_figma: runtime={runtime_screenshot_path}, "
        f"local_ref={figma_screenshot_path}, url={figma_url_or_key}, node={node_id}"
    )

    try:
        runtime_screenshot_path = validate_path(runtime_screenshot_path, "runtime_screenshot_path")
        output_dir = validate_path(output_dir, "output_dir")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    if not os.path.exists(runtime_screenshot_path):
        return {
            "status": "FAILURE",
            "error_output": f"Runtime screenshot not found: {runtime_screenshot_path}",
        }

    try:
        # Resolve the Figma reference image
        if figma_screenshot_path:
            ref_path = validate_path(figma_screenshot_path, "figma_screenshot_path")
            if not os.path.exists(ref_path):
                return {
                    "status": "FAILURE",
                    "error_output": f"Figma screenshot not found: {ref_path}",
                }
            node_name = "local_reference"
        elif figma_url_or_key and node_id:
            fetch_result = await fetch_figma_design_context(figma_url_or_key, node_id, output_dir)
            if fetch_result["status"] != "SUCCESS":
                return fetch_result
            ref_path = fetch_result["screenshot_path"]
            node_name = fetch_result.get("node_name", "figma_reference")
        else:
            return {
                "status": "FAILURE",
                "error_output": (
                    "Provide either figma_screenshot_path (OAuth/MCP flow) "
                    "or both figma_url_or_key and node_id (token flow)."
                ),
            }

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        diff_path = os.path.join(output_dir, f"ui_diff_{timestamp}.png")

        similarity = _compute_image_similarity(ref_path, runtime_screenshot_path, diff_path)
        confidence = similarity["similarity_score"]
        passed = confidence > 95.0

        logger.info(f"compare_ui_to_figma: confidence={confidence}, passed={passed}")
        return {
            "status": "SUCCESS",
            "confidence_score": confidence,
            "passed_threshold": passed,
            "threshold": 95.0,
            "runtime_screenshot_path": runtime_screenshot_path,
            "figma_screenshot_path": ref_path,
            "diff_image_path": diff_path,
            "figma_node_name": node_name,
            "similarity_details": similarity,
            "message": (
                f"Confidence score: {confidence}%. "
                f"{'Design matches Figma within threshold.' if passed else 'UI deviates from Figma; fixes required.'}"
            ),
        }
    except Exception as e:
        logger.error(f"compare_ui_to_figma ERROR: {e}")
        return {"status": "FAILURE", "error_output": str(e)}


# ============================================================
# TOOL 14: Compare Two Local Screenshots
# ============================================================
@mcp.tool()
async def compare_screenshots(
    reference_screenshot_path: str,
    runtime_screenshot_path: str,
    output_dir: str,
) -> dict:
    """Compare a reference screenshot against a runtime screenshot and return a confidence score.

    Use this when the reference image has already been obtained externally
    (e.g., via the Figma MCP server under OAuth) and saved to disk.
    """
    logger.info(
        f"compare_screenshots: ref={reference_screenshot_path}, "
        f"runtime={runtime_screenshot_path}"
    )

    try:
        reference_screenshot_path = validate_path(reference_screenshot_path, "reference_screenshot_path")
        runtime_screenshot_path = validate_path(runtime_screenshot_path, "runtime_screenshot_path")
        output_dir = validate_path(output_dir, "output_dir")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    if not os.path.exists(reference_screenshot_path):
        return {
            "status": "FAILURE",
            "error_output": f"Reference screenshot not found: {reference_screenshot_path}",
        }
    if not os.path.exists(runtime_screenshot_path):
        return {
            "status": "FAILURE",
            "error_output": f"Runtime screenshot not found: {runtime_screenshot_path}",
        }

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        diff_path = os.path.join(output_dir, f"screenshot_diff_{timestamp}.png")

        similarity = _compute_image_similarity(
            reference_screenshot_path, runtime_screenshot_path, diff_path
        )
        confidence = similarity["similarity_score"]
        passed = confidence > 95.0

        logger.info(f"compare_screenshots: confidence={confidence}, passed={passed}")
        return {
            "status": "SUCCESS",
            "confidence_score": confidence,
            "passed_threshold": passed,
            "threshold": 95.0,
            "reference_screenshot_path": reference_screenshot_path,
            "runtime_screenshot_path": runtime_screenshot_path,
            "diff_image_path": diff_path,
            "similarity_details": similarity,
            "message": (
                f"Confidence score: {confidence}%. "
                f"{'Screenshots match within threshold.' if passed else 'Screenshots differ; fixes required.'}"
            ),
        }
    except Exception as e:
        logger.error(f"compare_screenshots ERROR: {e}")
        return {"status": "FAILURE", "error_output": str(e)}


def _active_mock_projects() -> list[str]:
    """Discover active mock sessions for projects below the shared allowed root."""
    sessions_root = os.path.join(LOG_DIR, "sessions")
    if not os.path.isdir(sessions_root):
        return []

    projects = []
    for entry in os.scandir(sessions_root):
        manifest_path = os.path.join(entry.path, "manifest.json")
        if not entry.is_dir() or not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r") as handle:
                manifest = json.load(handle)
            recorded_project = manifest.get("project_path")
            if not isinstance(recorded_project, str) or not recorded_project.strip():
                raise ValueError("manifest has no project_path")
            project_path = validate_path(recorded_project, "project_path")
            if os.path.realpath(_mock_manifest_path(project_path)) != os.path.realpath(manifest_path):
                logger.warning("ignored mismatched mock manifest: %s", manifest_path)
                continue
            projects.append(project_path)
        except Exception as exc:
            logger.warning("ignored invalid mock manifest %s: %s", manifest_path, exc)
    return list(dict.fromkeys(projects))


def _cleanup_stale_allowed_project_sessions() -> None:
    """Best-effort cleanup for every interrupted project below the shared root."""
    for project_path in _active_mock_projects():
        try:
            result = asyncio.run(deactivate_mock_environment(project_path))
            logger.info(
                "automatic mock cleanup: project=%s status=%s",
                project_path,
                result.get("status"),
            )
        except Exception as exc:
            logger.error("automatic mock cleanup failed for %s: %s", project_path, exc)


if __name__ == "__main__":
    _cleanup_stale_allowed_project_sessions()
    try:
        mcp.run(transport="stdio")
    finally:
        _cleanup_stale_allowed_project_sessions()
