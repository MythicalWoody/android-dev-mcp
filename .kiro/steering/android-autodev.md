# Autonomous Android Development Protocol

## Core Directive
You are an autonomous agent. After the user answers initial clarifying questions, propose the high-level coding architecture and **PAUSE for user review/approval**. Do not start implementation until the user confirms the architecture is correct or requests changes. Once the architecture is approved, execute the remaining SDLC loop autonomously.

## Mandatory API Mode Choice

Before every API-dependent implementation or test workflow, explicitly ask:

> Do you want to use the deployed real API or temporary mock responses?

- Never infer the answer from the task, a previous workflow, or API availability.
- After the user answers, call `select_api_mode(project_path, "real"|"mock", true)`.
- If `real` is selected, do not activate or generate mocks. Use the appropriate existing development/UAT variant and the deployed API.
- If `mock` is selected, pass the returned one-time `selection_token` to `activate_mock_environment(...)`, then generate mocks and use the mock variant.
- Never call `activate_mock_environment` before the explicit choice.
- Always remove temporary mock integration during final cleanup.

## Execution Loop Rules

0. **API Mode Gate:** Ask the mandatory API mode question and call `select_api_mode`. Branch all later API setup, build variants, and tests according to the confirmed choice.
1. **Design & Requirements:** Use the provided `requirements.md`, Figma URL, and clarifying answers. Extract design context from Figma when a URL is provided.
2. **Architecture Proposal:** Propose the coding architecture (screen/activity structure, ViewModels, repositories, DI, network layer, navigation flow). **STOP and wait for explicit user approval or modification instructions.**
3. **Code Generation:** Only after architecture approval, write code strictly following `design.md` and the approved architecture.
4. **Compile Gate:** Build the selected mode: `assembleMockDebug` for mock mode, or the user/project-approved deployed-API debug variant for real mode.
   - IF FAILURE: Analyze `error_output`, fix code, and RE-RUN. Max 5 retries.
5. **Unit Test Gate:** Run the matching unit-test variant for the confirmed API mode.
   - IF FAILURE: Fix failing tests/code. RE-RUN. Max 5 retries.
6. **Conditional Mock Generation:** Only in confirmed mock mode, call `activate_mock_environment(...)` with the selection token and then `generate_mock_interceptor(...)`. Skip this step entirely in real mode.
7. **Pre-E2E Cleanup:** Call `cleanup_test_environment("<project_path>", "<package_name>")` before EVERY E2E attempt.
8. **Visual Verification Gate:**
   - Launch the app/activity with `launch_activity(...)`.
   - Run the generated Appium E2E script with `run_appium_e2e("<script_path>", "<project_path>")`.
   - Capture the runtime UI with `capture_ui_state("<project_path>/test-artifacts")`.
   - Check the Figma reference cache with `get_figma_reference_cache("<project_path>", "<file_key>", "<node_id>")`.
     - **If CACHED:** use the returned `cache_path` as the reference screenshot.
     - **If NOT_CACHED:** fetch the Figma node screenshot using the Figma MCP server (OAuth flow), then call `cache_figma_reference("<fetched_path>", "<project_path>", "<file_key>", "<node_id>")` to store it for reuse.
   - Compare the runtime screenshot against the reference using `compare_screenshots("<reference_path>", "<runtime_screenshot>", "<project_path>/test-artifacts")`.
   - IF confidence score is **greater than 95%**: stop development and mark the task as done.
   - ELSE: identify the mismatches, fix the UI/logic, and RE-RUN the visual verification gate. Max 8 retries.

## Definition of Done
Process terminates ONLY when:
- Coding architecture has been reviewed and approved by the user.
- The user explicitly selected real API or mock responses for this workflow.
- The selected mode's Gradle build exits with code 0.
- Unit tests pass rate = 100%.
- In mock mode only, all API endpoints and scenarios are mocked in `MockApiInterceptor.kt`.
- In real mode, no temporary mock integration exists.
- Runtime UI visually matches the Figma design with a confidence score **> 95%**.

## Critical Constraints
- NEVER claim success without executing validation tools.
- NEVER choose real API or mock responses on the user's behalf.
- NEVER activate mocks unless `select_api_mode` returned a token for the user's current explicit mock choice.
- NEVER ask user for next steps during execution loop.
- NEVER use external mock servers (WireMock, MockWebServer). Use ONLY the generated OkHttp interceptor.
- NEVER create files outside `src/mockDebug/` for mocking purposes.
- If max retries exceeded, halt and provide detailed diagnostic report.
- All structured logs are written to `/tmp/kiro-android-autodev/agent.log`.

## Allowed Gradle Commands
assembleMockDebug, assembleDebug, assembleRelease, testDebugUnitTest, testMockDebugUnitTest,
clean, lint, lintDebug, lintMockDebug, connectedMockDebugAndroidTest, connectedDebugAndroidTest,
installMockDebug, installDebug, uninstallAll

**Flavor-aware commands also supported via pattern matching:**
- `assemble<Flavor><BuildType>` (e.g., assembleDevelopmentDebug, assembleProductionRelease)
- `test<Flavor><BuildType>UnitTest` (e.g., testDevelopmentDebugUnitTest)
- `connected<Flavor><BuildType>AndroidTest` (e.g., connectedStagingDebugAndroidTest)
- `lint<Flavor><BuildType>` (e.g., lintProductionDebug)
- `install<Flavor><BuildType>` (e.g., installDevelopmentDebug)
- `bundle<Flavor><BuildType>` (e.g., bundleProductionRelease)

## New Tools Available
- `launch_activity(package_name, activity_name, extras?)` — Launch a specific activity before UI capture/E2E testing.
- `generate_appium_test(spec_path, activity_name, package_name, project_path, test_name?)` — Generate Appium pytest scripts from spec/design documents.
