"""Domain-focused MCP tool registration modules."""

from . import appium, build, device, figma, mocks, project, quality, review, workflow

TOOL_MODULES = (workflow, project, review, build, mocks, device, appium, figma, quality)

__all__ = ["TOOL_MODULES"]
