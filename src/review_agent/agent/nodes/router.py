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
from ..heuristics.perf_risk_filter import is_perf_worthwhile, perf_skip_reason
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
    """Router node: record the 'skipped' results the fan-out won't produce.

    Runs once per graph invocation (not once per file). Two kinds of skip:
    - ineligible files (binary / patch-omitted / no hunks) — skipped for ALL
      analysis, tagged source="skipped".
    - eligible files that fail the performance risk filter — skipped for the
      performance pass ONLY, tagged source="performance". They still get bug
      and security passes via fan_out. This makes "no performance comment" a
      provable filter decision, not a silent gap.
    """
    results: list[FileReviewResult] = []
    for f in state["diff_files"]:
        if not is_eligible(f):
            results.append(
                FileReviewResult(
                    path=f.path, status="skipped", source="skipped", reason=skip_reason(f)
                )
            )
            logger.info("file skipped (ineligible)", extra={"file": f.path})
        elif not is_perf_worthwhile(f):
            results.append(
                FileReviewResult(
                    path=f.path,
                    status="skipped",
                    source="performance",
                    reason=perf_skip_reason(f),
                )
            )
            logger.info("performance pass skipped by risk filter", extra={"file": f.path})
    return {"results": results}


# Analysis nodes that run on EVERY eligible file.
_UNCONDITIONAL_NODES = ("bug_analysis", "security_analysis")


def fan_out(state: ReviewState) -> list[Send]:
    """Conditional edge: fan out analysis nodes per eligible file.

    Bug and security run on every eligible file; performance runs only on files
    the risk pre-filter judges worth the pass (see ADR-0002). An empty list is
    valid — with no eligible file the graph proceeds to completion with only
    the router's skipped results.
    """
    sends: list[Send] = []
    for f in state["diff_files"]:
        if not is_eligible(f):
            continue
        for node in _UNCONDITIONAL_NODES:
            sends.append(Send(node, {"file": f}))
        if is_perf_worthwhile(f):
            sends.append(Send("performance_analysis", {"file": f}))
    return sends
