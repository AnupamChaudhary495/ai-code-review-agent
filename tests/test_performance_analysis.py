"""Performance-analysis node + golden-set detection (harness with mocked LLM).

The performance node routes through the same run_with_retry as bug/security.
Real detection of N+1 / O(n^2) is LLM judgment, so the golden-set detection
here uses a competent-model mock (proves wiring); the live rate is measured by
scripts/eval_security_review.py's sibling and needs ANTHROPIC_API_KEY.
"""

import pytest

from helpers import load_fixture
from review_agent import reviewer
from review_agent.agent.nodes.performance_analysis import analyze_performance
from review_agent.diffing import parser
from review_agent.diffing.models import ChangeType, FileChange, Hunk, SizeTier
from review_agent.reviewer import ReviewResult
from review_agent.schemas.finding import Finding


def make_change(lines: list[str], path: str = "x.py") -> FileChange:
    added = ["+" + ln for ln in lines]
    return FileChange(
        path=path,
        old_path=None,
        change_type=ChangeType.ADDED,
        additions=len(added),
        deletions=0,
        is_binary=False,
        patch_omitted=False,
        language="python",
        size_tier=SizeTier.SMALL,
        hunks=[Hunk(0, 0, 1, len(added), "", added, len(added), 0)],
    )


# file -> expected new-file line of the planted perf issue
PERF_GOLDEN = {"perf/n_plus_one.py": 5, "perf/quadratic.py": 4}


@pytest.fixture
def perf_changes():
    entries = {e["filename"]: e for e in load_fixture("pr_perf_files.json")}
    return {p: parser.parse_file(entries[p]) for p in PERF_GOLDEN}


def perf_finding(path: str, line: int) -> Finding:
    return Finding(
        file=path,
        line=line,
        category="performance",
        severity="high",
        message="perf issue",
        cwe=None,
    )


def test_node_tags_source_and_reuses_shared_runner(monkeypatch):
    change = make_change(["for x in xs:", "    db.query(x)"], path="a.py")
    monkeypatch.setattr(
        reviewer,
        "review_performance",
        lambda c: ReviewResult([perf_finding(c.path, 2)], "claude-test", "v", 10, 2, False),
    )
    (result,) = analyze_performance({"file": change})["results"]
    assert result.source == "performance"
    assert result.status == "reviewed"
    assert result.findings[0].category == "performance"


def test_node_does_not_call_bug_or_security(monkeypatch):
    change = make_change(["for x in xs:", "    f(x)"], path="a.py")
    monkeypatch.setattr(
        reviewer,
        "review_performance",
        lambda c: ReviewResult([], "claude-test", "v", 10, 2, False),
    )
    monkeypatch.setattr(
        reviewer, "review_file", lambda *a, **k: pytest.fail("perf node called bug reviewer")
    )
    monkeypatch.setattr(
        reviewer, "review_security", lambda *a, **k: pytest.fail("perf node called security")
    )
    analyze_performance({"file": change})


def _competent_llm(change: FileChange) -> ReviewResult:
    line = PERF_GOLDEN.get(change.path)
    findings = [perf_finding(change.path, line)] if line else []
    return ReviewResult(findings, "claude-test", "v", 100, 20, False)


def test_golden_set_both_perf_issues_detected(perf_changes, monkeypatch):
    monkeypatch.setattr(reviewer, "review_performance", _competent_llm)
    detected = 0
    for path, line in PERF_GOLDEN.items():
        (result,) = analyze_performance({"file": perf_changes[path]})["results"]
        if any(f.line == line and f.category == "performance" for f in result.findings):
            detected += 1
    assert detected == len(PERF_GOLDEN)  # both N+1 and O(n^2) surfaced


def test_clean_baseline_stays_silent(monkeypatch):
    clean = make_change(["def clamp(x, lo, hi):", "    return max(lo, min(x, hi))"])
    monkeypatch.setattr(
        reviewer,
        "review_performance",
        lambda c: ReviewResult([], "claude-test", "v", 10, 2, False),
    )
    (result,) = analyze_performance({"file": clean})["results"]
    assert result.findings == []
