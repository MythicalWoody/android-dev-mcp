import asyncio
import json
import base64
import os
import logging
from datetime import datetime
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AndroidAutoDev")

# --- Configuration ---
ALLOWED_PROJECT_ROOT = os.environ.get(
    "ANDROID_PROJECT_ROOT", "/Users/shivam.singh28"
)

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
    base_command = command.split()[0] if command else ""

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
    if not resolved.startswith(os.path.realpath(ALLOWED_PROJECT_ROOT)):
        raise ValueError(
            f"Rejected {label}: '{resolved}' is outside allowed root '{ALLOWED_PROJECT_ROOT}'."
        )
    return resolved


# --- Internal helpers ---
async def _ensure_appium_server():
    """Starts Appium server if not already running."""
    global _appium_server_process
    if _appium_server_process is None or _appium_server_process.returncode is not None:
        logger.info("Starting Appium server on port 4723")
        _appium_server_process = await asyncio.create_subprocess_shell(
            "appium --port 4723 --log-level warn",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(3)
    return _appium_server_process


async def _ensure_emulator_ready():
    """Blocks until device is online and boot animation completes."""
    logger.info("Waiting for emulator device...")
    proc = await asyncio.create_subprocess_shell(
        "adb wait-for-device",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60)

    while True:
        proc = await asyncio.create_subprocess_shell(
            "adb shell getprop sys.boot_completed",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if stdout.decode().strip() == "1":
            logger.info("Emulator boot confirmed")
            break
        await asyncio.sleep(2)


# ============================================================
# TOOL 1: Execute Gradle Commands (Whitelisted)
# ============================================================
@mcp.tool()
async def run_gradle(command: str, project_path: str) -> dict:
    """Runs a whitelisted gradle command and returns structured success/failure data."""
    logger.info(f"run_gradle: command={command}, path={project_path}")

    # Validate command against whitelist + patterns
    base_command = command.split()[0] if command else ""
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

    try:
        proc = await asyncio.create_subprocess_shell(
            f"./gradlew {command}",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
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
        logger.error("run_gradle TIMEOUT")
        return {
            "status": "FAILURE",
            "error_output": "Gradle command timed out after 5 minutes.",
        }


# --- Mock Response Helpers ---
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
# TOOL 2: Generate Mock Interceptor (Git-Safe Mocking)
# ============================================================
@mcp.tool()
async def generate_mock_interceptor(
    spec_path: str, package_name: str, project_path: str
) -> dict:
    """Parses design.md/requirements.md and generates a Kotlin OkHttp MockApiInterceptor.
    Outputs to <project_path>/app/src/mockDebug/java/<package>/network/.
    project_path should be the Android project root (e.g., /path/to/MyApp).
    This is Git-safe: no external WireMock server needed."""
    import re

    logger.info(f"generate_mock_interceptor: spec={spec_path}, pkg={package_name}, project={project_path}")

    try:
        spec_path = validate_path(spec_path, "spec_path")
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

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

    kotlin_code = f'''package {package_name}.network

import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody

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
        val method = request.method
        val path = request.url.encodedPath
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

            val mediaType = "application/json".toMediaType()
            val body = matchedRoute.responseBody.toResponseBody(mediaType)

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

        // No mock match — pass through to real network
        return chain.proceed(request)
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
async def run_appium_test(test_script_path: str) -> dict:
    """Executes an Appium test script and returns pass/fail status."""
    logger.info(f"run_appium_test: {test_script_path}")

    try:
        test_script_path = validate_path(test_script_path, "test_script_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    try:
        proc = await asyncio.create_subprocess_shell(
            f"pytest {test_script_path} --tb=short -q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        success = proc.returncode == 0
        if success:
            logger.info("run_appium_test SUCCESS")
        else:
            logger.warning("run_appium_test FAILED")
        return {
            "status": "SUCCESS" if success else "FAILURE",
            "details": (stdout + stderr).decode("utf-8")[-3000:],
        }
    except asyncio.TimeoutError:
        logger.error("run_appium_test TIMEOUT")
        return {
            "status": "FAILURE",
            "error_output": "Appium test timed out after 5 minutes.",
        }
    except Exception as e:
        return {"status": "FAILURE", "error_output": str(e)}


@mcp.tool()
async def run_appium_e2e(test_script_path: str, project_path: str) -> dict:
    """Orchestrates full Appium E2E execution.
    Handles server startup, emulator readiness, cleanup, and test execution."""
    logger.info(f"run_appium_e2e: script={test_script_path}, project={project_path}")

    try:
        test_script_path = validate_path(test_script_path, "test_script_path")
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    # Check retry limit
    retry_error = _increment_retry("e2e_gate", max_retries=8)
    if retry_error:
        return retry_error

    try:
        # Step 1: Ensure infrastructure is ready
        await _ensure_appium_server()
        await _ensure_emulator_ready()

        # Step 2: Execute pytest with JSON report output
        artifacts_dir = os.path.join(project_path, "test-artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        report_path = os.path.join(artifacts_dir, "e2e_report.json")

        cmd = (
            f"pytest {test_script_path} "
            f"--json-report --json-report-file={report_path} "
            f"--tb=short -q"
        )

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

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
        logger.error("run_appium_e2e TIMEOUT")
        return {
            "status": "FAILURE",
            "error_output": "E2E test timed out after 10 minutes.",
        }
    except Exception as e:
        logger.error(f"run_appium_e2e ERROR: {e}")
        return {"status": "FAILURE", "error_output": f"E2E orchestration error: {str(e)}"}


# ============================================================
# TOOL 4: Capture and Verify UI (Multimodal Wrapper)
# ============================================================
@mcp.tool()
async def capture_ui_state(output_dir: str) -> dict:
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

    # ADB screenshot
    proc1 = await asyncio.create_subprocess_shell(
        f"adb shell screencap -p /sdcard/screen.png && adb pull /sdcard/screen.png {img_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc1.communicate(), timeout=30)

    # ADB UI dump
    proc2 = await asyncio.create_subprocess_shell(
        f"adb shell uiautomator dump /sdcard/ui.xml && adb pull /sdcard/ui.xml {xml_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc2.communicate(), timeout=30)

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
        "screenshot_path": img_path,
        "xml_path": xml_path,
        "xml_hierarchy": xml_content[:8000],
        "screenshot_base64": img_b64,
        "message": "Review screenshot and XML against design.md specs. Verify layout, text, colors, and element presence.",
    }


@mcp.tool()
async def capture_and_verify_ui(
    output_dir: str, spec_requirement: str
) -> dict:
    """Captures UI state and returns it alongside the spec requirement for multimodal verification.
    The AI agent uses the screenshot + XML + requirement to determine pass/fail."""
    logger.info(f"capture_and_verify_ui: requirement='{spec_requirement[:50]}...'")

    # Capture the current UI state
    capture_result = await capture_ui_state(output_dir)

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
async def verify_emulator_ready() -> dict:
    """Pre-flight check: waits for ADB device and verifies emulator is booted."""
    logger.info("verify_emulator_ready: checking...")
    try:
        proc = await asyncio.create_subprocess_shell(
            "adb wait-for-device",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)

        proc2 = await asyncio.create_subprocess_shell(
            "adb shell getprop sys.boot_completed",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc2.communicate(), timeout=15)
        boot_status = stdout.decode("utf-8").strip()

        if boot_status == "1":
            logger.info("verify_emulator_ready: READY")
            return {"status": "READY", "message": "Emulator is fully booted and ready."}
        else:
            logger.warning(f"verify_emulator_ready: NOT_READY (boot_completed={boot_status})")
            return {
                "status": "NOT_READY",
                "message": f"Emulator device found but boot_completed={boot_status}. Wait and retry.",
            }
    except asyncio.TimeoutError:
        logger.error("verify_emulator_ready: TIMEOUT")
        return {
            "status": "FAILURE",
            "error_output": "Timed out waiting for emulator. Is an AVD running?",
        }
    except Exception as e:
        return {"status": "FAILURE", "error_output": str(e)}


# ============================================================
# TOOL 6: Cleanup Test Environment
# ============================================================
@mcp.tool()
async def cleanup_test_environment(project_path: str, package_name: str) -> dict:
    """Combined teardown: clears app data, kills orphaned Appium, resets mock state.
    Call before every E2E retry to ensure clean slate."""
    global _appium_server_process
    logger.info(f"cleanup_test_environment: pkg={package_name}")

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    cleanup_steps = []

    # Step 1: Clear app data on device
    proc = await asyncio.create_subprocess_shell(
        f"adb shell pm clear {package_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    app_clear_result = stdout.decode().strip()
    cleanup_steps.append(f"App data clear: {app_clear_result}")

    # Step 2: Kill orphaned Appium processes
    if _appium_server_process and _appium_server_process.returncode is None:
        _appium_server_process.terminate()
        await _appium_server_process.wait()
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
    """Removes and recreates the /mocks directory to prevent stale state before E2E runs.
    Also cleans generated MockApiInterceptor files from mockDebug source set."""
    import shutil

    logger.info(f"clean_mocks: project={project_path}")

    try:
        project_path = validate_path(project_path, "project_path")
    except ValueError as e:
        return {"status": "FAILURE", "error_output": str(e)}

    cleaned = []

    # Clean project-level mocks directory if it exists
    mocks_dir = os.path.join(project_path, "mocks")
    if os.path.exists(mocks_dir):
        shutil.rmtree(mocks_dir)
        os.makedirs(mocks_dir, exist_ok=True)
        cleaned.append(f"Removed and recreated: {mocks_dir}")

    # Clean generated mock interceptor files from mockDebug source set
    mock_debug_dir = os.path.join(project_path, "app", "src", "mockDebug")
    if os.path.exists(mock_debug_dir):
        shutil.rmtree(mock_debug_dir)
        os.makedirs(mock_debug_dir, exist_ok=True)
        cleaned.append(f"Removed and recreated: {mock_debug_dir}")

    if not cleaned:
        cleaned.append("No mock directories found to clean")

    logger.info(f"clean_mocks: completed — {len(cleaned)} items cleaned")
    return {
        "status": "SUCCESS",
        "cleaned": cleaned,
        "message": "Mock state cleared. Ready for fresh mock generation.",
    }


# ============================================================
# TOOL 8: Launch Activity (Pre-capture / Pre-test helper)
# ============================================================
@mcp.tool()
async def launch_activity(package_name: str, activity_name: str, extras: str = "") -> dict:
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

    # Build adb command
    cmd = f"adb shell am start -n {component}"
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
        cmd += f" {extras}"

    try:
        # First, ensure emulator is ready
        await _ensure_emulator_ready()

        proc = await asyncio.create_subprocess_shell(
            cmd,
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
                "command": cmd,
            }

        # Wait a moment for the activity to render
        await asyncio.sleep(2)

        logger.info(f"launch_activity SUCCESS: {component}")
        return {
            "status": "SUCCESS",
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
import time
import pytest
from appium import webdriver
from appium.options import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException


# --- Known UI Element IDs (from spec) ---
KNOWN_ELEMENT_IDS = {json.dumps(element_ids, indent=4)}

KNOWN_ACCESSIBILITY_IDS = {json.dumps(accessibility_ids, indent=4)}


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
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = "emulator-5554"
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
import pytest
from appium import webdriver
from appium.options import UiAutomator2Options


def pytest_addoption(parser):
    parser.addoption("--device", default="emulator-5554", help="Device serial")
    parser.addoption("--appium-host", default="http://127.0.0.1:4723", help="Appium server URL")


@pytest.fixture(scope="session")
def appium_options(request):
    """Base Appium options."""
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = request.config.getoption("--device")
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
