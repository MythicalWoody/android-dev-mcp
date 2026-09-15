"""Allow-listed Android build tooling."""

from .. import runtime
from ._registration import register_tools

TOOLS = (runtime.run_gradle,)


def register(mcp) -> None:
    register_tools(mcp, TOOLS)
