"""API-mode selection and temporary mock-environment tools."""

from .. import runtime
from ._registration import register_tools

TOOLS = (
    runtime.select_api_mode,
    runtime.activate_mock_environment,
    runtime.deactivate_mock_environment,
    runtime.generate_mock_interceptor,
    runtime.clean_mocks,
    runtime.verify_no_temporary_mock_wiring,
)


def register(mcp) -> None:
    register_tools(mcp, TOOLS)
