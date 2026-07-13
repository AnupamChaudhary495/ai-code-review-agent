"""Graph fan-out tests: 10-file run and single-file-failure chaos.

The LLM is mocked at reviewer.review_file (the node calls it; we do not
reimplement review logic). These assert the orchestration contract:
one node invocation per eligible file, one result per file, and that a single
file's failure is isolated within its retry bound.
"""

import time

import anthropic
import httpx
import pytest

from helpers import load_fixture
from review_agent.agent import graph
from review_agent.agent.nodes import bug_analysis
from review_agent.agent.state import FileReviewResult
from review_agent.diffing import parser
from review_agent.reviewer import ReviewResult
from review_agent.schemas.finding import Finding


@pytest.fixture
def ten_files():
    """The eval corpus PR: 11 recorded real files (all eligible python)."""
    entries = load_fixture("pr_eval_files.json")
    files = [parser.parse_file(e) for e in entries]
    assert len(files) == 11 and all(f.hunks for f in files)
    return files[:10]  # take exactly ten for the 10-file assertions


def make_result(path: str, n_findings: int = 1) -> ReviewResult:
    findings = [
        Finding(file=path, line=1, category="bug", severity="low", message=f"issue {i}")
        for i in range(n_findings)
    ]
    return ReviewResult(
        findings=findings,
        model="claude-test",
        prompt_version="bug_review_v1",
        input_tokens=100,
        output_tokens=20,
        repair_used=False,
    )


def test_ten_files_produce_ten_results_one_call_each(ten_files, monkeypatch):
    calls: list[str] = []

    def fake_review_file(change):
        calls.append(change.path)
        return make_result(change.path, n_findings=2)

    monkeypatch.setattr(bug_analysis.reviewer, "review_file", fake_review_file)

    results = graph.review_files(ten_files)

    # Exactly one node invocation (one review_file call) per file.
    assert sorted(calls) == sorted(f.path for f in ten_files)
    assert len(calls) == 10
    # One result per file, all reviewed, from a single graph invocation.
    assert len(results) == 10
    assert {r.status for r in results} == {"reviewed"}
    assert all(len(r.findings) == 2 for r in results)
    # Results cover exactly the input files.
    assert {r.path for r in results} == {f.path for f in ten_files}


def test_single_file_failure_is_isolated_within_iteration_bound(ten_files, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # no real backoff waits
    target = ten_files[3].path
    attempts: dict[str, int] = {}

    def flaky_review_file(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target:
            # Persistent transient failure — every attempt times out.
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
        return make_result(change.path, n_findings=1)

    monkeypatch.setattr(bug_analysis.reviewer, "review_file", flaky_review_file)

    results = graph.review_files(ten_files)
    by_path = {r.path: r for r in results}

    # The graph still completed with a result for every file.
    assert len(results) == 10
    # The failing file is retried up to the hard ceiling, then reported unavailable.
    assert attempts[target] == bug_analysis.MAX_ATTEMPTS
    failed = by_path[target]
    assert failed.status == "unavailable"
    assert failed.error_count == bug_analysis.MAX_ATTEMPTS
    assert "exhausted" in failed.reason
    # The other nine files are intact and correct.
    intact = [r for p, r in by_path.items() if p != target]
    assert len(intact) == 9
    assert all(r.status == "reviewed" and len(r.findings) == 1 for r in intact)


def test_transient_failure_recovers_within_bound(ten_files, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    target = ten_files[0].path
    attempts: dict[str, int] = {}

    def recovering_review_file(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target and attempts[change.path] == 1:
            raise anthropic.RateLimitError(
                "slow down",
                response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
                body=None,
            )
        return make_result(change.path, n_findings=1)

    monkeypatch.setattr(bug_analysis.reviewer, "review_file", recovering_review_file)

    results = graph.review_files(ten_files)
    recovered = next(r for r in results if r.path == target)
    assert recovered.status == "reviewed"
    assert recovered.error_count == 1  # one transient failure recorded, then success
    assert attempts[target] == 2


def test_non_retryable_output_error_does_not_spin(ten_files, monkeypatch):
    from review_agent.reviewer import ReviewOutputError

    target = ten_files[2].path
    attempts: dict[str, int] = {}

    def bad_output(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target:
            raise ReviewOutputError("unparseable after repair")
        return make_result(change.path, n_findings=1)

    monkeypatch.setattr(bug_analysis.reviewer, "review_file", bad_output)

    results = graph.review_files(ten_files)
    by_path = {r.path: r for r in results}
    # Persistent parse failure is recorded immediately — NOT retried to the ceiling.
    assert attempts[target] == 1
    assert by_path[target].status == "unavailable"
    assert all(r.status == "reviewed" for p, r in by_path.items() if p != target)


def test_ineligible_files_skipped_before_llm(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        bug_analysis.reviewer,
        "review_file",
        lambda change: calls.append(change.path) or make_result(change.path),
    )
    edge = parser.parse_files(load_fixture("pr_edge_files.json"))
    # pr_edge has a binary file (data.bin) and renames with no hunks.
    results = graph.review_files(edge)
    by_path = {r.path: r for r in results}

    assert "data.bin" not in calls  # binary never reached the LLM
    assert by_path["data.bin"].status == "skipped"
    assert by_path["data.bin"].reason == "binary file (no patch)"
    # Every input file still has a result.
    assert {r.path for r in results} == {f.path for f in edge}
    assert isinstance(results[0], FileReviewResult)
