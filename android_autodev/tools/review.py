"""Manual code-review checkpoint and isolated proposal-workspace tools."""

from .. import runtime
from ._registration import register_tools

TOOLS = (
    runtime.prepare_code_review_workspace,
    runtime.cleanup_code_review_workspace,
    runtime.request_code_review,
    runtime.list_pending_code_reviews,
    runtime.get_code_review,
    runtime.cancel_code_review,
    runtime.record_code_review_decision,
    runtime.apply_reviewed_patch,
)


def register(mcp) -> None:
    register_tools(mcp, TOOLS)
