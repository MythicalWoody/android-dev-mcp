# Android AutoDev MCP

Android AutoDev is a local MCP server for review-gated Android changes, UAT
Debug builds, API mocks, device/Appium automation, failure diagnostics, and
Figma visual comparison.

## Safety model

- Every integration run starts with `start_workflow`; project, device, retry,
  API-mode, build-variant, Appium port, and artifact state are workflow-scoped.
- Routine builds are restricted to `UatDebug`. `MockDebug` requires the
  workflow's explicit mock selection. Any other variant requires a one-time
  token from `authorize_environment` after current-chat user confirmation.
- Product changes use a persisted unified diff, highlighted chat review, and a
  one-time approval token. Approvals are bound to the diff and Git worktree
  fingerprint, and can be listed or resumed after context loss.
- Added diff lines are scanned for known credential formats and high-entropy
  values before review and again immediately before apply. Findings are always
  redacted. Dependency-file diffs must pass an OSV scan of the exact managed
  review workspace before they can be shown for approval.
- `run_quality_gate` fails closed unless the resolved UAT Debug dependency graph
  is vulnerability-free and both project-provided Detekt and ktlint tasks pass.
  Only Maven package coordinates and versions—not project source—are sent to OSV.
- Generated paths and source identifiers are validated. Appium tests interpret
  a small data-only action vocabulary and fail on unsupported or missing checks.
- Every code addition or alteration must add or update accurate explanatory
  comments/docstrings for changed classes, non-trivial functions, safety rules,
  lifecycle behavior, and architectural decisions.
- Cleanup preserves application data by default, terminates only an Appium
  process owned by the workflow, and removes only workflow-owned artifacts.

An MCP cannot intercept edits made through a coding client's unrelated built-in
filesystem tools. Configure the client sandbox/rules to require the MCP review
workflow if review must be an absolute enforcement boundary.

## Tier 1 safety gates

### Secret scanning

`request_code_review` scans every added diff line before a proposal is persisted
or shown to the user. `apply_reviewed_patch` scans the exact approved diff again
at the final mutation boundary, so an approval created under older rules cannot
bypass a newer scanner.

The scanner combines known credential patterns with an entropy check. It covers
private keys, AWS access keys, GitHub and Slack tokens, Google API keys, JWTs,
generic credential assignments, and unlabelled high-entropy strings. Findings
contain only the rule, source location, and a short one-way fingerprint; the
suspected value is never returned in tool output or written to review state.

There is no automatic bypass. Remove the credential, replace it with environment
or secret-store access, then submit a new review.

### Dependency vulnerability scanning

Dependency changes must be made inside a workspace created by
`prepare_code_review_workspace`. Before a review can be displayed, the MCP:

1. verifies that `proposed_diff` exactly matches the managed workspace;
2. resolves `uatDebugRuntimeClasspath` for the recommended Android module;
3. submits only resolved Maven coordinates and versions to the OSV batch API;
4. blocks the review if OSV reports a vulnerability or the scan is incomplete.

Build scripts, settings scripts, version catalogs, Gradle properties, dependency
locks, Gradle verification metadata, wrapper properties, and Kotlin convention
plugins under `buildSrc/` or `build-logic/` trigger this pre-review gate. Project
source code is never sent to OSV.

`scan_dependency_vulnerabilities` also exposes the same fail-closed scan for an
active workflow. `run_quality_gate` runs it again against the applied project so
the final result reflects the dependency graph that will actually be tested.

### Detekt and ktlint enforcement

`run_static_analysis` discovers and runs both analyzers through the project's
Gradle wrapper. It prefers UAT-specific tasks when available:

- Detekt: `detektUatDebug`, then `detekt`
- ktlint: `ktlintUatDebugCheck`, then `ktlintCheck`

Both analyzers are required. Missing tasks and analyzer failures are blocking
quality-gate results, not warnings. `doctor(project_path)` reports whether the
corresponding plugins appear to be configured before a workflow starts.

## Structure

```text
android_autodev/
├── app.py                 # MCP composition and lifecycle
├── runtime.py             # Backward-compatible tool facade and legacy parsers
├── security.py            # Path, identifier, escaping, permissions, and locks
├── security_scanning.py   # Redacted regex and entropy scanning for review diffs
├── dependency_scanning.py # Resolved Gradle graph and OSV vulnerability checks
├── static_analysis.py     # Detekt/ktlint task discovery
├── workflows.py           # Durable workflow state, leases, and environment tokens
├── project.py             # Android project discovery and TOML profiles
├── mocking.py             # Groovy/KTS and OkHttp mock integration planning
├── e2e_generation.py      # Fail-closed Appium action interpreter generation
├── review_workspace.py    # Private, quota-limited proposal snapshots
├── visual.py              # Dimension-aware perceptual comparison and heatmaps
└── tools/                 # Domain MCP registration and orchestration modules
```

Domain behavior now lives in dedicated services. `runtime.py` keeps compatibility
for existing imports while the public MCP surface is composed from `tools/`.

## Run

```bash
export ANDROID_PROJECT_ROOT="/path/containing/android/projects"
uv run android-autodev
```

Optional configuration:

```bash
export ANDROID_AUTODEV_STATE_DIR="/private/path/android-autodev-state"
export ANDROID_DEVICE_UDID="emulator-or-device-serial"
export FIGMA_ACCESS_TOKEN="token-for-rest-api-mode"
```

The state directory defaults to the operating system's temporary directory and
is created with owner-only permissions. Logs rotate automatically.

## Android project prerequisites

In addition to the normal Android SDK and Gradle wrapper setup, a project using
the complete quality gate must provide:

- a `UatDebug` variant and resolvable `uatDebugRuntimeClasspath` configuration;
- a Detekt Gradle task (`detektUatDebug` or `detekt`);
- a ktlint Gradle task (`ktlintUatDebugCheck` or `ktlintCheck`);
- outbound HTTPS access to `https://api.osv.dev` for vulnerability lookup.

Run `doctor(project_path)` before starting work. An unavailable OSV service or
dependency-resolution failure deliberately blocks the scan because an unknown
dependency state cannot be treated as safe.

## Recommended workflow

1. Run `doctor(project_path)` and `inspect_android_project(project_path)`.
2. Call `start_workflow(project_path, purpose)`.
3. Ask for real or mock API mode and record it with `select_api_mode`.
4. Prepare changes in `prepare_code_review_workspace`.
5. Submit the exact diff through `request_code_review`, including `workflow_id`.
   Dependency changes are scanned from that exact workspace. Show the returned
   `review_markdown` and stop for the user's next message.
6. Resume with `record_code_review_decision`; apply an approval only through
   `apply_reviewed_patch`.
7. Run `run_quality_gate`; it scans resolved dependencies, runs Detekt and
   ktlint, then runs UAT Debug lint, unit tests, and assembly.
8. Generate and run fail-closed Appium checks on the leased device.
9. Use `collect_failure_bundle` when a gate fails.
10. Run final workflow cleanup and verify that no temporary mock wiring remains.

Pending reviews can be recovered with `list_pending_code_reviews` and
`get_code_review`, or closed with `cancel_code_review`.

## Quality-gate order

`run_quality_gate(project_path, workflow_id)` stops at the first failing gate:

1. OSV scan of the resolved UAT Debug dependency graph
2. Detekt
3. ktlint
4. `lintUatDebug`
5. `testUatDebugUnitTest`
6. `assembleUatDebug`
7. `connectedUatDebugAndroidTest` when `include_connected_tests=true`

All Gradle tasks remain workflow-bound and subject to the UAT-only environment
policy. Detekt and ktlint tasks are allow-listed as non-variant-changing checks.

## Safety-gate errors

| Error code | Meaning | Recovery |
|---|---|---|
| `REVIEW_SAFETY_CHECK_FAILED` | The review diff is invalid or contains a suspected secret. | Remove the unsafe value and submit a new diff. |
| `DEPENDENCY_REVIEW_WORKSPACE_REQUIRED` | A dependency-sensitive diff was submitted without its managed workspace. | Prepare the edit with `prepare_code_review_workspace` and pass its path plus `workflow_id`. |
| `DEPENDENCY_SCAN_INCOMPLETE` | Gradle resolution, OSV connectivity, or the OSV response was incomplete. | Restore resolution/network access and rerun; do not bypass the gate. |
| `VULNERABLE_DEPENDENCIES` | OSV reported at least one vulnerability. | Upgrade, replace, or remove the dependency before review. |
| `STATIC_ANALYSIS_NOT_CONFIGURED` | Detekt or ktlint does not expose a supported Gradle task. | Configure both analyzers and rerun `doctor`. |
| `STATIC_ANALYSIS_FAILED` | A configured analyzer reported a violation or failed. | Fix the reported issue and rerun the quality gate. |

## Test

```bash
uv run python -m pytest -q
```
