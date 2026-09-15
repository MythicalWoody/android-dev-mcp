"""Shared registration helper for domain tool modules."""

from collections.abc import Callable, Iterable
from typing import Any


def register_tools(mcp: Any, tools: Iterable[Callable[..., Any]]) -> None:
    """Register each function with FastMCP while preserving its public name."""
    for tool in tools:
        mcp.tool()(tool)
