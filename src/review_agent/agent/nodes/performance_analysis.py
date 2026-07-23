"""Performance-analysis node: a third LLM pass, gated by the risk pre-filter.

Reimplements no LLM or resilience logic — it calls reviewer.review_performance
through the shared run_with_retry, exactly like bug and security. It is only
reached for files the risk filter judged worth the pass; files that fail the
filter get a "skipped" performance result from the router instead.
"""

from ... import reviewer
from ..state import AnalysisInput, FileReviewResult
from ._runner import run_with_retry


def analyze_performance(state: AnalysisInput) -> dict[str, list[FileReviewResult]]:
    """Analyse one file for performance issues; always return one result."""
    file = state["file"]
    result = run_with_retry(file, reviewer.review_performance, source="performance")
    return {"results": [result]}
