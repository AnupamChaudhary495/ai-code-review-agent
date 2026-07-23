"""The multi-file review StateGraph: fan out per eligible file, gather results.

    START -> router -> (fan_out: analysis Sends per file) -> END
                        ├─> bug_analysis          (every eligible file)
                        ├─> security_analysis     (every eligible file)
                        └─> performance_analysis  (only if the risk filter passes)

The router records skipped results (ineligible files, and files the perf risk
filter excludes from the performance pass). Every fanned-out branch runs
concurrently and contributes one result through the `results` reducer. An
eligible, perf-worthy file yields three results (bug + security + performance);
an eligible non-perf-worthy file yields two node results plus a skipped
performance result; an ineligible file yields one skipped result.
"""

import logging

from langgraph.graph import END, START, StateGraph

from ..diffing.models import FileChange
from ..github.diff_fetcher import PullRequestDiff
from .nodes.bug_analysis import analyze_file
from .nodes.performance_analysis import analyze_performance
from .nodes.router import fan_out, route
from .nodes.security_analysis import analyze_security
from .state import FileReviewResult, ReviewState

logger = logging.getLogger(__name__)

_ANALYSIS_NODE_NAMES = ["bug_analysis", "security_analysis", "performance_analysis"]


def build_graph():
    """Compile the review graph. Cheap enough to build per run; no global state."""
    builder = StateGraph(ReviewState)
    builder.add_node("router", route)
    builder.add_node("bug_analysis", analyze_file)
    builder.add_node("security_analysis", analyze_security)
    builder.add_node("performance_analysis", analyze_performance)

    builder.add_edge(START, "router")
    # Conditional fan-out: router -> analysis nodes per file (or straight to
    # END if no file is eligible / no node is dispatched).
    builder.add_conditional_edges("router", fan_out, _ANALYSIS_NODE_NAMES)
    for node in _ANALYSIS_NODE_NAMES:
        builder.add_edge(node, END)
    return builder.compile()


def review_files(files: list[FileChange]) -> list[FileReviewResult]:
    """Run the review graph over a list of files; return one result per file."""
    graph = build_graph()
    final_state = graph.invoke({"diff_files": files, "results": []})
    results = final_state["results"]
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    logger.info(
        "review graph completed",
        extra={"files": len(files), "results": len(results), "by_status": by_status},
    )
    # Stable ordering for callers/tests: by path.
    return sorted(results, key=lambda r: r.path)


def review_pull_request(diff: PullRequestDiff) -> list[FileReviewResult]:
    """Convenience entry point from a Phase 3 PullRequestDiff."""
    return review_files(diff.files)
