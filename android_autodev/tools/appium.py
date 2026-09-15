"""Appium test generation, execution, verification, and cleanup tools."""

from .. import runtime
from ._registration import register_tools

TOOLS = (
    runtime.run_appium_test,
    runtime.run_appium_e2e,
    runtime.capture_and_verify_ui,
    runtime.cleanup_test_environment,
    runtime.generate_appium_test,
)


def register(mcp) -> None:
    register_tools(mcp, TOOLS)
