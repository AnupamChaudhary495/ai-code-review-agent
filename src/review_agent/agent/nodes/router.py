"""Routing: eligibility filtering and the size-tier model-selection seam.

Two responsibilities, both deliberately kept out of the LLM path:

1. Eligibility — skip files with no hunks (binary, patch-omitted, pure
   renames) BEFORE they reach an LLM. Skipped files still produce a result so
   the run reports on every file.
2. Model selection by size tier — a stub today (one model for every tier),
   but a real seam. Phase 6/7 will route small/medium/large/huge files to
   different models and analysis depth for cost control; that routing lands
   here, not by rewriting call sites.
"""

import logging

from langgraph.types import Send

from ...config import get_settings
from ...diffing.models import FileChange, SizeTier
from ..state import FileReviewResult, ReviewState

logger = logging.getLogger(__name__)


def is_eligible(file: FileChange) -> bool:
    """A file is reviewable iff it has parsed hunks to send to the model."""
    return bool(file.hunks) and not file.is_binary and not file.patch_omitted


def skip_reason(file: FileChange) -> str:
    if file.is_binary:
        return "binary file (no patch)"
    if file.patch_omitted:
        return "patch omitted by GitHub (oversized diff)"
    return "no reviewable hunks"


def select_model(file: FileChange) -> str:
    """Choose the model for a file based on its size tier.

    STUB (Phase 5): every tier maps to the single configured model. The seam
    exists so Phase 6/7 can route, e.g., huge files to a cheaper/summarizing
    model without touching bug_analysis or the graph wiring.
    """
    model = get_settings().llm_model
    _MODEL_BY_TIER: dict[SizeTier, str] = {
        SizeTier.SMALL: model,
        SizeTier.MEDIUM: model,
        SizeTier.LARGE: model,
        SizeTier.HUGE: model,
    }
    return _MODEL_BY_TIER[file.size_tier]


def route(state: ReviewState) -> dict[str, list[FileReviewResult]]:
    """Router node: record a 'skipped' result for every ineligible file.

    Runs once per graph invocation (not once per file). Eligible files are
    fanned out to bug-analysis by `fan_out` below.
    """
    skipped = [
        FileReviewResult(path=f.path, status="skipped", source="skipped", reason=skip_reason(f))
        for f in state["diff_files"]
        if not is_eligible(f)
    ]
    for result in skipped:
        logger.info("file skipped", extra={"file": result.path, "reason": result.reason})
    return {"results": skipped}


# Analysis nodes fanned out per eligible file. Each name here becomes one Send
# per file; adding a node (Phase 7 performance) means adding it to this tuple.
_ANALYSIS_NODES = ("bug_analysis", "security_analysis")


def fan_out(state: ReviewState) -> list[Send]:
    """Conditional edge: one Send to EACH analysis node per eligible file.

    An empty list is valid — when no file is eligible the graph simply has no
    fan-out branches and proceeds to completion with only skipped results.
    """
    return [
        Send(node, {"file": f})
        for f in state["diff_files"]
        if is_eligible(f)
        for node in _ANALYSIS_NODES
    ]
