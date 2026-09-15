"""Backward-compatible Android AutoDev MCP entry point.

Tool registration now lives in ``android_autodev.tools``. Existing integrations
can continue launching ``python server.py`` and tests can continue importing the
runtime helpers from ``server``.
"""

from android_autodev import runtime as _runtime
from android_autodev.app import mcp, run


def __getattr__(name: str):
    """Forward legacy helper imports without replacing this module object."""
    return getattr(_runtime, name)

if __name__ == "__main__":
    run()
