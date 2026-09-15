"""MCP application composition and lifecycle."""

import asyncio
import os

from . import runtime
from .tools import TOOL_MODULES

mcp = runtime.mcp

for tool_module in TOOL_MODULES:
    tool_module.register(mcp)


def run() -> None:
    """Run the stdio MCP server with best-effort temporary-state cleanup."""
    runtime._cleanup_review_workspaces()
    try:
        mcp.run(transport="stdio")
    finally:
        asyncio.run(runtime._cleanup_owned_appium_processes())
        runtime._cleanup_stale_allowed_project_sessions(owner_pid=os.getpid())
        runtime._cleanup_review_workspaces(owner_pid=os.getpid())
