# Android Project Integration Guide

This guide explains how to connect your Android project to the Kiro MCP server so the autonomous agent can build, test, and verify your app end-to-end.

---

## Prerequisites

- Kiro IDE with MCP server connected (`android-auto-dev`)
- Android emulator (AVD) available via `adb`
- Appium installed globally (`npm install -g appium`)
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

## Step 5: Set the Project Root Environment Variable

The MCP server whitelists paths under `ANDROID_PROJECT_ROOT`. Set it to cover your Android project:

```bash
export ANDROID_PROJECT_ROOT="/Users/shivam.singh28"
```

Or update the MCP config to pass it:

```json
{
  "mcpServers": {
    "android-auto-dev": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/shivam.singh28/kiro-mcp-server", "python", "server.py"],
      "env": {
        "ANDROID_PROJECT_ROOT": "/Users/shivam.singh28"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

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

1. Reads `requirements.md` and `design.md`
2. Generates feature code (Activities, ViewModels, etc.)
3. Calls `generate_mock_interceptor` → creates `MockApiInterceptor.kt` in `src/mockDebug/`
4. Calls `run_gradle("assembleMockDebug", ...)` → compile gate
5. Calls `run_gradle("testMockDebugUnitTest", ...)` → unit test gate
6. Calls `cleanup_test_environment(...)` → clean slate
7. Generates Appium E2E test script
8. Calls `run_appium_e2e(...)` → E2E gate
9. On failure: calls `capture_and_verify_ui(...)` → visual check, fixes, retries

### The agent halts when:
- All gates pass (success), OR
- Max retries exceeded (provides diagnostic report)

---

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `run_gradle(command, project_path)` | Run whitelisted Gradle commands |
| `generate_mock_interceptor(spec_path, package_name, output_dir)` | Generate OkHttp interceptor from spec |
| `run_appium_test(test_script_path)` | Simple Appium test execution |
| `run_appium_e2e(test_script_path, project_path)` | Full E2E orchestration with retries |
| `capture_ui_state(output_dir)` | Screenshot + XML dump |
| `capture_and_verify_ui(output_dir, spec_requirement)` | Capture + verification context |
| `verify_emulator_ready()` | Pre-flight emulator check |
| `cleanup_test_environment(project_path, package_name)` | Full teardown between runs |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Gradle command not whitelisted" | Only commands in the allow-list work. Check steering rules for the full list. |
| "Path outside allowed root" | Set `ANDROID_PROJECT_ROOT` env var to cover your project path |
| "No module named 'mcp'" | Run `uv sync` in the MCP server directory |
| Emulator timeout | Start your AVD manually before triggering E2E |
| Appium not found | Install globally: `npm install -g appium` |
| E2E flaky failures | Agent calls `cleanup_test_environment` between retries automatically |

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

The `mockDebug` source set files ARE meant to be committed — they're part of your test infrastructure.

---

## Logs

All tool executions are logged to:
```
/tmp/kiro-android-autodev/agent.log
```

Check this file for debugging failed runs or understanding the agent's decision path.
