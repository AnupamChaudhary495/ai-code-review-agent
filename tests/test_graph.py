"""Graph fan-out tests: three analysis nodes, performance gated by risk filter.

The LLM is mocked at reviewer.review_file / review_security / review_performance
(the nodes call them; we do not reimplement review logic). These assert the
orchestration contract: bug + security on every eligible file, performance only
on files the risk filter passes, correctly-tagged results, and failure
isolation within the retry bound.
"""

import time

import anthropic
import httpx
import pytest

from helpers import load_fixture
from review_agent import reviewer
from review_agent.agent import graph
from review_agent.agent.heuristics.perf_risk_filter import is_perf_worthwhile
from review_agent.agent.nodes import bug_analysis
from review_agent.agent.state import FileReviewResult
from review_agent.diffing import parser
from review_agent.reviewer import ReviewResult
from review_agent.schemas.finding import Finding


@pytest.fixture
def ten_files():
    entries = load_fixture("pr_eval_files.json")
    files = [parser.parse_file(e) for e in entries]
    assert len(files) == 11 and all(f.hunks for f in files)
    return files[:10]


def make_result(path: str, n_findings: int = 1, category: str = "bug") -> ReviewResult:
    findings = [
        Finding(file=path, line=1, category=category, severity="low", message=f"issue {i}")
        for i in range(n_findings)
    ]
    return ReviewResult(findings, "claude-test", "v", 100, 20, False)


@pytest.fixture
def mock_other_passes(monkeypatch):
    """Security + performance passes succeed by default, isolating bug tests."""
    sec: list[str] = []
    perf: list[str] = []
    monkeypatch.setattr(
        reviewer,
        "review_security",
        lambda c: sec.append(c.path) or make_result(c.path, 1, "security"),
    )
    monkeypatch.setattr(
        reviewer,
        "review_performance",
        lambda c: perf.append(c.path) or make_result(c.path, 1, "performance"),
    )
    return {"security": sec, "performance": perf}


def by_source(results, source):
    return [r for r in results if r.source == source]


def test_three_passes_bug_security_always_performance_when_worthy(
    ten_files, monkeypatch, mock_other_passes
):
    bug_calls: list[str] = []
    monkeypatch.setattr(
        reviewer,
        "review_file",
        lambda c: bug_calls.append(c.path) or make_result(c.path, 2),
    )

    results = graph.review_files(ten_files)

    worthy = [f.path for f in ten_files if is_perf_worthwhile(f)]
    unworthy = [f.path for f in ten_files if not is_perf_worthwhile(f)]
    assert worthy and unworthy  # the corpus exercises both branches

    # Bug and security run on every eligible file.
    assert sorted(bug_calls) == sorted(f.path for f in ten_files)
    assert sorted(mock_other_passes["security"]) == sorted(f.path for f in ten_files)
    # Performance node runs ONLY on worthy files.
    assert sorted(mock_other_passes["performance"]) == sorted(worthy)

    bugs, secs, perfs = (by_source(results, s) for s in ("bug", "security", "performance"))
    assert len(bugs) == 10
    assert len(secs) == 10
    # One performance OUTCOME per eligible file: reviewed if worthy, else skipped.
    assert len(perfs) == 10
    reviewed_perf = {r.path for r in perfs if r.status == "reviewed"}
    skipped_perf = {r.path for r in perfs if r.status == "skipped"}
    assert reviewed_perf == set(worthy)
    assert skipped_perf == set(unworthy)
    assert all("risk pre-filter" in r.reason for r in perfs if r.status == "skipped")


def test_single_bug_failure_isolated_from_run_and_other_passes(
    ten_files, monkeypatch, mock_other_passes
):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    target = ten_files[3].path
    attempts: dict[str, int] = {}

    def flaky_bug(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target:
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
        return make_result(change.path)

    monkeypatch.setattr(reviewer, "review_file", flaky_bug)

    results = graph.review_files(ten_files)
    bugs = {r.path: r for r in by_source(results, "bug")}

    assert attempts[target] == bug_analysis.MAX_ATTEMPTS
    assert bugs[target].status == "unavailable"
    assert bugs[target].error_count == bug_analysis.MAX_ATTEMPTS
    # Other passes and other files are intact.
    assert all(r.status == "reviewed" for r in by_source(results, "security"))
    assert all(r.status == "reviewed" for p, r in bugs.items() if p != target)


def test_transient_bug_failure_recovers_within_bound(ten_files, monkeypatch, mock_other_passes):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    target = ten_files[0].path
    attempts: dict[str, int] = {}

    def recovering_bug(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target and attempts[change.path] == 1:
            raise anthropic.RateLimitError(
                "slow down",
                response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
                body=None,
            )
        return make_result(change.path)

    monkeypatch.setattr(reviewer, "review_file", recovering_bug)

    results = graph.review_files(ten_files)
    recovered = next(r for r in by_source(results, "bug") if r.path == target)
    assert recovered.status == "reviewed"
    assert recovered.error_count == 1
    assert attempts[target] == 2


def test_ineligible_files_skipped_before_any_llm(monkeypatch, mock_other_passes):
    bug_calls: list[str] = []
    monkeypatch.setattr(
        reviewer,
        "review_file",
        lambda c: bug_calls.append(c.path) or make_result(c.path),
    )
    edge = parser.parse_files(load_fixture("pr_edge_files.json"))
    results = graph.review_files(edge)
    tagged = {(r.path, r.source): r for r in results}

    assert "data.bin" not in bug_calls
    assert "data.bin" not in mock_other_passes["security"]
    assert "data.bin" not in mock_other_passes["performance"]
    assert tagged[("data.bin", "skipped")].status == "skipped"
    assert tagged[("data.bin", "skipped")].reason == "binary file (no patch)"
    assert {r.path for r in results} == {f.path for f in edge}
    # Every eligible file has bug + security + a performance outcome.
    for f in edge:
        if f.hunks and not f.is_binary and not f.patch_omitted:
            sources = [r.source for r in results if r.path == f.path]
            assert sorted(sources) == ["bug", "performance", "security"]
    assert isinstance(results[0], FileReviewResult)
