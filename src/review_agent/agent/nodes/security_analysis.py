"""Security-analysis node: runs ALONGSIDE bug_analysis, not instead of it.

Two signals per file, merged into one result:
1. A deterministic secret scan (agent/tools/secret_scan.py) — high-confidence
   hardcoded secrets that must not depend on model judgment. Always runs.
2. An LLM security pass (reviewer.review_security) via the shared retry runner
   — injection, unsafe deserialization, auth mistakes, path traversal, etc.

The secret-scan findings are seeded into the runner so they survive even when
the LLM pass is unavailable. No dedup between bug and security findings on the
same line yet — that is Phase 8's job.
"""

from ... import reviewer
from ..state import AnalysisInput, FileReviewResult
from ..tools.secret_scan import scan_file
from ._runner import run_with_retry


def analyze_security(state: AnalysisInput) -> dict[str, list[FileReviewResult]]:
    """Analyse one file for security issues; always return one FileReviewResult."""
    file = state["file"]
    seed_findings = scan_file(file)  # deterministic, no LLM
    result = run_with_retry(
        file, reviewer.review_security, source="security", seed_findings=seed_findings
    )
    return {"results": [result]}
