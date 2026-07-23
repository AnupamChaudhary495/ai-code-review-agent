"""Bug-analysis node: a thin wrapper over reviewer.review_file for one file.

Reimplements no LLM logic — it calls the Phase 4 review_file() and runs it
through the shared retry runner, which turns any failure into a recorded
"analysis unavailable" result rather than an exception that aborts the graph.
"""

from ... import reviewer
from ..state import AnalysisInput, FileReviewResult
from ._runner import MAX_ATTEMPTS, run_with_retry

__all__ = ["analyze_file", "MAX_ATTEMPTS"]


def analyze_file(state: AnalysisInput) -> dict[str, list[FileReviewResult]]:
    """Analyse one file for bugs; always return exactly one FileReviewResult."""
    file = state["file"]
    result = run_with_retry(file, reviewer.review_file, source="bug")
    return {"results": [result]}
