# Android AutoDev MCP

Android AutoDev is a local MCP server for review-gated Android changes, UAT
Debug builds, API mocks, device/Appium automation, failure diagnostics, and
Figma visual comparison.

## Development status

`main` contains the stable review-gated workflow described below. Tier 1 safety
gates are implemented and documented on
[`codex/tier1-safety-gates`](https://github.com/MythicalWoody/android-dev-mcp/tree/codex/tier1-safety-gates),
but they are not active on `main` until that branch is merged.

The feature branch adds:

- redacted regex and entropy-based secret scanning before review and again
  immediately before an approved patch is applied;
- an exact-workspace dependency gate that resolves the UAT Debug Maven graph
  and checks it with OSV before dependency changes can be reviewed;
- mandatory Detekt and ktlint execution in `run_quality_gate`, followed by the
  existing UAT Debug lint, unit-test, and build tasks;
- project diagnostics, failure codes, explanatory documentation, and regression
  coverage for all three gates.

Review the complete branch difference or open a pull request from the
[`main...codex/tier1-safety-gates` comparison](https://github.com/MythicalWoody/android-dev-mcp/compare/main...codex/tier1-safety-gates).

## Safety model

- Every integration run starts with `start_workflow`; project, device, retry,
  API-mode, build-variant, Appium port, and artifact state are workflow-scoped.
- Routine builds are restricted to `UatDebug`. `MockDebug` requires the
  workflow's explicit mock selection. Any other variant requires a one-time
  token from `authorize_environment` after current-chat user confirmation.
- Product changes use a persisted unified diff, highlighted chat review, and a
  one-time approval token. Approvals are bound to the diff and Git worktree
  fingerprint, and can be listed or resumed after context loss.
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

## Structure

```text
android_autodev/
├── app.py                 # MCP composition and lifecycle
├── runtime.py             # Backward-compatible tool facade and legacy parsers
├── security.py            # Path, identifier, escaping, permissions, and locks
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

## Recommended workflow

1. Run `doctor(project_path)` and `inspect_android_project(project_path)`.
2. Call `start_workflow(project_path, purpose)`.
3. Ask for real or mock API mode and record it with `select_api_mode`.
4. Prepare changes in `prepare_code_review_workspace`.
5. Submit the exact diff through `request_code_review`, show the returned
   `review_markdown`, and stop for the user's next message.
6. Resume with `record_code_review_decision`; apply an approval only through
   `apply_reviewed_patch`.
7. Run `run_quality_gate` or individual workflow-bound Gradle tasks.
8. Generate and run fail-closed Appium checks on the leased device.
9. Use `collect_failure_bundle` when a gate fails.
10. Run final workflow cleanup and verify that no temporary mock wiring remains.

Pending reviews can be recovered with `list_pending_code_reviews` and
`get_code_review`, or closed with `cancel_code_review`.

## Test

```bash
uv run python -m pytest -q
```

The Tier 1 feature branch includes the additional security-gate regression tests;
the baseline command above runs the tests available on whichever branch is
currently checked out.
