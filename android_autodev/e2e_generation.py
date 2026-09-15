"""Fail-closed Appium test generation from a small typed action vocabulary."""

from __future__ import annotations

import json

from .security import (
    validate_activity_name,
    validate_package_name,
    validate_python_identifier,
)


SUPPORTED_ACTIONS = {"click", "send_keys", "scroll", "swipe", "assert", "navigate"}
SUPPORTED_ASSERTIONS = {"text_content", "visibility", "navigation", "message"}


def _validated_actions(user_flows: list[dict]) -> list[dict]:
    """Convert parser output into data-only actions or reject the entire test plan."""
    actions = []
    for index, flow in enumerate(user_flows, start=1):
        action = str(flow.get("action", "unknown"))
        if action not in SUPPORTED_ACTIONS:
            raise ValueError(
                f"Flow step {index} is unsupported ({flow.get('raw', action)!r}); "
                "the generator will not emit a passing TODO."
            )
        target = str(flow.get("target", "")).strip()
        if action in {"click", "send_keys", "assert", "navigate"} and not target:
            raise ValueError(f"Flow step {index} requires an explicit target.")
        actions.append(
            {
                "action": action,
                "target": target,
                "value": str(flow.get("value", "")),
                "description": str(flow.get("raw", "")),
            }
        )
    return actions


def _validated_assertions(assertions: list[dict]) -> list[dict]:
    checks = []
    for index, assertion in enumerate(assertions, start=1):
        kind = str(assertion.get("type", ""))
        expected = str(assertion.get("expected", "")).strip()
        if kind not in SUPPORTED_ASSERTIONS or not expected:
            raise ValueError(f"Assertion {index} is unsupported or has no expected value.")
        checks.append({"type": kind, "expected": expected})
    return checks


def build_appium_script(
    package_name: str,
    activity: str,
    ui_elements: list[dict],
    user_flows: list[dict],
    assertions: list[dict],
    test_name: str,
) -> str:
    """Build an executable test whose untrusted spec content remains JSON data."""
    package = validate_package_name(package_name)
    activity = validate_activity_name(activity)
    test_name = validate_python_identifier(test_name, "test_name")
    actions = _validated_actions(user_flows)
    checks = _validated_assertions(assertions)
    element_ids = [str(item["id"]) for item in ui_elements if item.get("id")]
    accessibility_ids = [
        str(item["accessibility_id"])
        for item in ui_elements
        if item.get("accessibility_id")
    ]
    plan_is_empty = not actions and not checks and not element_ids and not accessibility_ids
    plan_lacks_checks = not (
        checks
        or element_ids
        or accessibility_ids
        or any(action["action"] in {"assert", "navigate"} for action in actions)
    )

    class_name = "Test" + "".join(part.title() for part in test_name.removeprefix("test_").split("_"))
    return f'''"""Generated Appium test using Android AutoDev's fail-closed action DSL."""

import json
import os
import subprocess

import pytest
from appium import webdriver
try:
    from appium.options.android import UiAutomator2Options
except ImportError:
    from appium.options import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


PACKAGE_NAME = {package!r}
ACTIVITY_NAME = {activity!r}
ACTIONS = {json.dumps(actions, ensure_ascii=False, indent=4)}
ASSERTIONS = {json.dumps(checks, ensure_ascii=False, indent=4)}
KNOWN_ELEMENT_IDS = {json.dumps(element_ids, ensure_ascii=False, indent=4)}
KNOWN_ACCESSIBILITY_IDS = {json.dumps(accessibility_ids, ensure_ascii=False, indent=4)}
PLAN_IS_EMPTY = {plan_is_empty!r}
PLAN_LACKS_CHECKS = {plan_lacks_checks!r}


def resolve_device_udid():
    """Use the leased runner device and never guess among multiple devices."""
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


def find_required_element(driver, target, timeout=10):
    """Resolve a required element through bounded strategies or fail the test."""
    resource_id = target.lower().replace(" ", "_")
    ui_automator_text = json.dumps(target)[1:-1]
    strategies = [
        (AppiumBy.ID, f"{{PACKAGE_NAME}}:id/{{resource_id}}"),
        (AppiumBy.ACCESSIBILITY_ID, target),
        (AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{{ui_automator_text}}")'),
    ]
    for by, value in strategies:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
        except (TimeoutException, NoSuchElementException):
            continue
    pytest.fail(f"Required UI element was not found: {{target}}")


@pytest.fixture(scope="session")
def driver():
    """Create a state-preserving Appium session against the runner-owned server."""
    device_udid = resolve_device_udid()
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = device_udid
    options.udid = device_udid
    options.app_package = PACKAGE_NAME
    options.app_activity = ACTIVITY_NAME
    options.automation_name = "UiAutomator2"
    options.no_reset = True
    options.full_reset = False
    options.new_command_timeout = 300
    host = os.environ.get("APPIUM_SERVER_URL", "http://127.0.0.1:4723")
    session = webdriver.Remote(host, options=options)
    yield session
    session.quit()


def execute_action(driver, action):
    """Interpret one validated action; unsupported actions are hard failures."""
    kind = action["action"]
    target = action["target"]
    if kind == "click":
        find_required_element(driver, target).click()
    elif kind == "send_keys":
        element = find_required_element(driver, target)
        element.clear()
        element.send_keys(action["value"])
    elif kind == "scroll":
        driver.find_element(
            AppiumBy.ANDROID_UIAUTOMATOR,
            "new UiScrollable(new UiSelector().scrollable(true)).scrollForward()",
        )
    elif kind == "swipe":
        size = driver.get_window_size()
        driver.swipe(size["width"] // 2, int(size["height"] * 0.8), size["width"] // 2, int(size["height"] * 0.2), 500)
    elif kind == "assert":
        assert target in driver.page_source, f"Expected content was not visible: {{target}}"
    elif kind == "navigate":
        WebDriverWait(driver, 10).until(lambda active: target.lower() in active.current_activity.lower())
    else:
        pytest.fail(f"Unsupported generated action: {{kind}}")


def execute_assertion(driver, check):
    """Evaluate each declared expectation without skips or unconditional passes."""
    expected = check["expected"]
    if check["type"] == "navigation":
        assert expected.lower() in driver.current_activity.lower(), (
            f"Expected activity {{expected!r}}, got {{driver.current_activity!r}}"
        )
    else:
        assert expected in driver.page_source, f"Expected UI content was absent: {{expected}}"


class {class_name}:
    """Execute the approved spec plan and its required UI assertions."""

    def test_main_flow(self, driver):
        """Run every action and assertion; any unsupported or missing item fails."""
        if PLAN_IS_EMPTY:
            pytest.fail("The generated plan contains no executable checks")
        for action in ACTIONS:
            execute_action(driver, action)
        for check in ASSERTIONS:
            execute_assertion(driver, check)
        if PLAN_LACKS_CHECKS:
            pytest.fail("The generated plan performs actions but verifies no outcome")

    def test_required_elements_present(self, driver):
        """Require every extracted ID and accessibility label to be discoverable."""
        for element_id in KNOWN_ELEMENT_IDS:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((AppiumBy.ID, f"{{PACKAGE_NAME}}:id/{{element_id}}"))
            )
        for label in KNOWN_ACCESSIBILITY_IDS:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((AppiumBy.ACCESSIBILITY_ID, label))
            )
'''


def build_conftest(package_name: str, activity: str) -> str:
    """Generate only shared command-line options; driver ownership stays in each test."""
    validate_package_name(package_name)
    validate_activity_name(activity)
    return '''"""Shared options for Android AutoDev generated Appium tests."""

def pytest_addoption(parser):
    """Expose runner-owned device and Appium endpoint overrides."""
    parser.addoption("--device", default="", help="ADB serial selected by the workflow")
    parser.addoption("--appium-host", default="", help="Appium URL selected by the workflow")
'''
