# Android Project Integration Guide

This guide explains how to connect your Android project to the Kiro MCP server so the autonomous agent can build, test, and verify your app end-to-end.

---

## Prerequisites

- Kiro IDE with MCP server connected (`android-auto-dev`)
- An authorized physical, network, or emulated Android device visible in `adb devices`
- Appium installed globally (`npm install -g appium`)
- Appium UiAutomator2 driver installed (`appium driver install uiautomator2`)
- Python 3.10+ with `uv` for the MCP server
- Node.js for Appium

---

## Step 1: Add `mock` Product Flavor to Your Android Project

In your `app/build.gradle.kts`, add a `mock` flavor alongside your existing flavors:

```kotlin
android {
    // Your existing config...

    flavorDimensions += "environment"

    productFlavors {
        create("development") {
            dimension = "environment"
            buildConfigField("String", "API_BASE_URL", "\"https://dev-api.yourapp.com\"")
        }
        create("uat") {
            dimension = "environment"
            buildConfigField("String", "API_BASE_URL", "\"https://uat-api.yourapp.com\"")
        }
        create("production") {
            dimension = "environment"
            buildConfigField("String", "API_BASE_URL", "\"https://api.yourapp.com\"")
        }
        // ADD THIS:
        create("mock") {
            dimension = "environment"
            buildConfigField("String", "API_BASE_URL", "\"http://localhost\"")
            // URL doesn't matter — interceptor catches all requests
        }
    }

    buildFeatures {
        buildConfig = true
    }
}
```

---

## Step 2: Create the `mockDebug` Source Set Directory

```bash
mkdir -p app/src/mockDebug/java/<your/package/path>/network/
```

Example for package `com.example.myapp`:
```bash
mkdir -p app/src/mockDebug/java/com/example/myapp/network/
```

The MCP tool `generate_mock_interceptor` will write files here automatically.

---

## Step 3: Wire the Interceptor in `mockDebug` Only

Create `app/src/mockDebug/java/<package>/di/MockNetworkProvider.kt`:

```kotlin
package com.example.myapp.di

import com.example.myapp.network.MockApiInterceptor
import okhttp3.OkHttpClient

/**
 * Provides OkHttpClient with mock interceptor.
 * This file only exists in the mockDebug source set.
 */
object MockNetworkProvider {
    fun provideClient(): OkHttpClient {
        return OkHttpClient.Builder()
            .addInterceptor(MockApiInterceptor())
            .build()
    }
}
```

In your main network module, use a compile-time check:

```kotlin
// In your main source set (e.g., NetworkModule.kt)
import okhttp3.OkHttpClient

object NetworkModule {
    fun provideClient(): OkHttpClient {
        return OkHttpClient.Builder()
            // In non-mock flavors, no interceptor is added
            .build()
    }
}
```

Or use Hilt/Dagger with flavor-specific modules:

```kotlin
// app/src/mock/java/.../di/MockModule.kt
@Module
@InstallIn(SingletonComponent::class)
object MockModule {
    @Provides
    @Singleton
    fun provideOkHttp(): OkHttpClient {
        return OkHttpClient.Builder()
            .addInterceptor(MockApiInterceptor())
            .build()
    }
}
```

---

## Step 4: Create `requirements.md` and `design.md` in Your Project Root

The agent reads these to generate mocks and validate behavior.

**`requirements.md`** — Describe features with API endpoints:

```markdown
# Login Feature

## API Endpoints
- POST /api/v1/auth/login
- GET /api/v1/user/profile
- POST /api/v1/auth/refresh-token

## User Flow
1. User enters email and password
2. Taps "Login" button
3. On success: navigates to Home screen
4. On failure: shows error snackbar

## Error Scenarios
- 400: Invalid credentials → show "Invalid email or password"
- 500: Server error → show "Something went wrong, try again"
- Timeout: → show "Connection timed out"
```

**`design.md`** — Describe UI layout, colors, component specs.

---

## Step 5: Configure the MCP Once for All Projects

The MCP server whitelists paths under `ANDROID_PROJECT_ROOT`. Point it at the
shared parent directory that contains your Android projects, rather than one
specific project. For the standard Android Studio layout:

```bash
export ANDROID_PROJECT_ROOT="/Users/shivam.singh28/StudioProjects"
```

Register the server in the global Kiro configuration at
`~/.kiro/settings/mcp.json` (and in `~/.codex/config.toml` when using Codex), not
inside an individual project's `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "android-auto-dev": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/shivam.singh28/android-dev-mcp", "python", "server.py"],
      "env": {
        "ANDROID_PROJECT_ROOT": "/Users/shivam.singh28/StudioProjects"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

This single registration works for every project below `StudioProjects`. Pass
the current project's absolute path to each MCP tool; no per-project MCP setting
change is required.

---

## Step 6: Using the Agent

Once connected, the autonomous workflow is:

### Initial Prompt to Kiro
```
Build the Login feature following requirements.md and design.md.
Project path: /Users/shivam.singh28/MyAndroidApp
Package: com.example.myapp
```

### What the Agent Does Automatically

1. Runs `doctor` and `inspect_android_project`, then starts an isolated workflow
2. Reads `requirements.md` and `design.md`
3. Asks whether to use the deployed real API or temporary mock responses and records the choice against that workflow
4. Creates an MCP-managed temporary review workspace outside the Android project
5. Prepares and compiles the proposed changes only in that temporary workspace
6. Submits the complete unified diff and removes the temporary workspace
7. Posts the highlighted proposed diff in chat and ends the turn
8. Waits for your next message to approve, reject, or request changes
9. Resumes only after recording that next-message decision
10. Generates and activates mock wiring only when mock mode was explicitly selected
11. Builds and tests the authorized variant
12. Resolves the actual connected ADB device (physical, network, or emulator)
13. Generates and runs the Appium E2E test against that resolved serial
14. On failure: captures UI state, prepares a revised diff, and requests a new review
15. Performs ownership-aware final cleanup and verifies that no temporary mock wiring remains

The pending chat review persists for 24 hours. It does not authorize any source
change. After your next chat message approves the proposal, the MCP issues an
approval token bound to the exact project, diff, and Git worktree fingerprint; that token expires after 30
minutes and works once. Any change to the diff requires a new chat review.

UAT Debug is the default environment for day-to-day integration work. The agent
must not substitute Development, Staging, Production, Release, or another build
variant unless you explicitly request it in the current chat. If UAT Debug does
not exist in the project, the agent must stop and ask. Explicitly choosing mock
API mode authorizes that workflow's Mock Debug variant.
Any other variant additionally requires a one-time token from
`authorize_environment`. The restriction is checked by `run_gradle`, rather
than depending only on prompt instructions.

Whenever code is added or altered, its explanatory comments or documentation
must also be added or updated. Every generated or materially changed class and
non-trivial function explains its responsibility. Non-obvious business rules,
lifecycle behavior, safety constraints, and architectural decisions explain why
the logic exists. A change is incomplete when those comments are missing or
stale; comments that only repeat obvious syntax are avoided.

### The agent halts when:
- All gates pass (success), OR
- Max retries exceeded (provides diagnostic report)

---

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `start_workflow(project_path, purpose)` | Create isolated state and project/resource leases for one task |
| `get_workflow_status(...)` / `cancel_workflow(...)` | Resume, inspect, or safely abandon a workflow |
| `doctor(project_path?)` | Validate Java, SDK, adb, Appium, Node, Figma, and project prerequisites |
| `inspect_android_project(project_path, save_profile?)` | Discover modules, Gradle DSL, flavors, IDs, activity, and integration libraries |
| `prepare_code_review_workspace(project_path, include_untracked_paths?)` | Create a private, quota-limited snapshot from tracked and explicitly selected files |
| `cleanup_code_review_workspace(project_path, review_workspace_path)` | Remove a managed proposal workspace without touching the Android project |
| `request_code_review(project_path, change_summary, proposed_diff, review_workspace_path?)` | Persist the proposed diff, clean its managed workspace, return a syntax-highlighted Markdown `diff` block, and require the AI to end its turn |
| `list_pending_code_reviews(...)` / `get_code_review(...)` / `cancel_code_review(...)` | Recover or close reviews after chat context loss |
| `record_code_review_decision(project_path, review_id, proposed_diff?, decision, user_response, user_confirmed?)` | Resume on the user's next message; issue an apply token only for explicit approval |
| `apply_reviewed_patch(project_path, proposed_diff, approval_token)` | Validate and apply the exact approved diff; rejects altered, expired, unsafe, or reused approvals |
| `authorize_environment(...)` | Issue one exact, one-time non-UAT variant authorization after explicit confirmation |
| `run_gradle(..., workflow_id, environment_authorization_token?)` | Enforce UAT/Mock policy and run one bounded Gradle task with combined diagnostics |
| `run_quality_gate(project_path, workflow_id)` | Run the standard UAT Debug lint, unit-test, and build sequence |
| `generate_mock_interceptor(..., workflow_id)` | Generate escaped OkHttp source inside the workflow's discovered mock source set |
| `run_appium_test(..., workflow_id)` | Run against the leased device and workflow-owned dynamic Appium port |
| `run_appium_e2e(..., workflow_id)` | Full E2E orchestration with isolated reports and fail-closed result parsing |
| `capture_ui_state(..., workflow_id)` | Screenshot + XML dump from the leased device; Base64 is opt-in |
| `capture_and_verify_ui(output_dir, spec_requirement)` | Capture + verification context |
| `verify_emulator_ready(..., workflow_id)` | Resolve, verify, and lease a physical or emulated Android device |
| `cleanup_test_environment(..., workflow_id, clear_app_data?, user_confirmed_data_clear?)` | Preserve app data by default and remove only workflow-owned resources |
| `collect_failure_bundle(...)` | Collect workflow, logcat, device, and package diagnostics |
| `verify_no_temporary_mock_wiring(project_path)` | Final audit for MCP-generated mock wiring |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Gradle command not whitelisted" | Only commands in the allow-list work. Check steering rules for the full list. |
| "Path outside allowed root" | Set `ANDROID_PROJECT_ROOT` env var to cover your project path |
| "No module named 'mcp'" | Run `uv sync` in the MCP server directory |
| `NEEDS_USER_DECISION` | End the current turn and wait for the user's next chat message before recording a decision |
| "diff changed after approval" | Submit the complete current diff through `request_code_review` again |
| Review appears as plain text | Display the tool's `review_markdown` field verbatim; its fenced `diff` block highlights additions and removals in compatible chat clients |
| Temporary proposal files appear in the Android project | Use `prepare_code_review_workspace`; never create `.android-auto-review` or `android-auto-review-copy` inside the project |
| Device timeout | Check `adb devices`; pass `device_serial` when multiple devices are online |
| Appium not found | Install globally: `npm install -g appium` |
| E2E flaky failures | Inspect `collect_failure_bundle`; cleanup preserves app state and never kills an external Appium process |

---

## File Changes in Your Android Project

The agent will only create/modify files in:
- `app/src/mockDebug/java/<package>/network/MockApiInterceptor.kt` (generated)
- `app/src/mockDebug/java/<package>/network/MockNetworkModule.kt` (generated)
- `test-artifacts/` directory (screenshots, XML dumps, reports — add to `.gitignore`)

Add to your `.gitignore`:
```
test-artifacts/
```

AndroidAutoDev-generated `mockDebug` files are temporary. Do not commit them. Final cleanup removes only MCP-tagged mock files and preserves user-owned test infrastructure.

---

## Logs

Tool executions use rotating logs below `ANDROID_AUTODEV_STATE_DIR`. When the
variable is unset, the server uses the platform temporary directory under
`android-autodev/agent.log`.

Check this file for debugging failed runs or understanding the agent's decision path.
