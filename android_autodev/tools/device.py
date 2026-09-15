"""ADB device selection, launch, and UI capture tools."""

from .. import runtime
from ._registration import register_tools

TOOLS = (
    runtime.capture_ui_state,
    runtime.verify_emulator_ready,
    runtime.launch_activity,
)


def register(mcp) -> None:
    register_tools(mcp, TOOLS)
