"""Figma reference retrieval, caching, and visual-comparison tools."""

from .. import runtime
from ._registration import register_tools

TOOLS = (
    runtime.cache_figma_reference,
    runtime.get_figma_reference_cache,
    runtime.fetch_figma_design_context,
    runtime.compare_ui_to_figma,
    runtime.compare_screenshots,
)


def register(mcp) -> None:
    register_tools(mcp, TOOLS)
