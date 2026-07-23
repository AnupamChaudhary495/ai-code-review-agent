"""Graph fan-out tests: two analysis nodes per file, failure isolation.

The LLM is mocked at reviewer.review_file / reviewer.review_security (the nodes
call them; we do not reimplement review logic). These assert the orchestration
contract: one invocation of each analysis pass per eligible file, correctly
tagged results, and that a single pass's failure is isolated within its retry
bound without taking the run — or the other pass — down.
"""

import time

import anthropic
import httpx
import pytest

from helpers import load_fixture
from review_agent import reviewer
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


def make_result(path: str, n_findings: int = 1, category: str = "bug") -> ReviewResult:
    findings = [
        Finding(file=path, line=1, category=category, severity="low", message=f"issue {i}")
        for i in range(n_findings)
    ]
    return ReviewResult(
        findings=findings,
        model="claude-test",
        prompt_version="v",
        input_tokens=100,
        output_tokens=20,
        repair_used=False,
    )


@pytest.fixture
def mock_security(monkeypatch):
    """Security pass succeeds cleanly by default so bug-failure tests are isolated."""
    calls: list[str] = []

    def fake_security(change):
        calls.append(change.path)
        return make_result(change.path, n_findings=1, category="security")

    monkeypatch.setattr(reviewer, "review_security", fake_security)
    return calls


def bug_results(results):
    return [r for r in results if r.source == "bug"]


def security_results(results):
    return [r for r in results if r.source == "security"]


def test_each_eligible_file_gets_a_bug_and_a_security_pass(ten_files, monkeypatch, mock_security):
    bug_calls: list[str] = []

    def fake_bug(change):
        bug_calls.append(change.path)
        return make_result(change.path, n_findings=2)

    monkeypatch.setattr(reviewer, "review_file", fake_bug)

    results = graph.review_files(ten_files)

    # Exactly one bug pass and one security pass per file, from one invocation.
    assert sorted(bug_calls) == sorted(f.path for f in ten_files)
    assert sorted(mock_security) == sorted(f.path for f in ten_files)
    assert len(results) == 20  # 10 files x 2 passes
    assert len(bug_results(results)) == 10
    assert len(security_results(results)) == 10
    assert {r.status for r in results} == {"reviewed"}
    # Each pass's findings are tagged with the right category.
    assert all(f.category == "bug" for r in bug_results(results) for f in r.findings)
    assert all(f.category == "security" for r in security_results(results) for f in r.findings)


def test_single_bug_failure_is_isolated_from_run_and_security_pass(
    ten_files, monkeypatch, mock_security
):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    target = ten_files[3].path
    attempts: dict[str, int] = {}

    def flaky_bug(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target:
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
        return make_result(change.path, n_findings=1)

    monkeypatch.setattr(reviewer, "review_file", flaky_bug)

    results = graph.review_files(ten_files)
    bugs = {r.path: r for r in bug_results(results)}
    secs = {r.path: r for r in security_results(results)}

    # Run completed with both passes for every file.
    assert len(results) == 20
    # The failing bug pass retried to the ceiling, then reported unavailable.
    assert attempts[target] == bug_analysis.MAX_ATTEMPTS
    assert bugs[target].status == "unavailable"
    assert bugs[target].error_count == bug_analysis.MAX_ATTEMPTS
    assert "exhausted" in bugs[target].reason
    # The OTHER pass on the same file is unaffected.
    assert secs[target].status == "reviewed"
    # The other nine files' bug passes are intact.
    assert all(r.status == "reviewed" for p, r in bugs.items() if p != target)
    assert all(r.status == "reviewed" for r in secs.values())


def test_transient_bug_failure_recovers_within_bound(ten_files, monkeypatch, mock_security):
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
        return make_result(change.path, n_findings=1)

    monkeypatch.setattr(reviewer, "review_file", recovering_bug)

    results = graph.review_files(ten_files)
    recovered = next(r for r in bug_results(results) if r.path == target)
    assert recovered.status == "reviewed"
    assert recovered.error_count == 1
    assert attempts[target] == 2


def test_non_retryable_output_error_does_not_spin(ten_files, monkeypatch, mock_security):
    from review_agent.reviewer import ReviewOutputError

    target = ten_files[2].path
    attempts: dict[str, int] = {}

    def bad_output(change):
        attempts[change.path] = attempts.get(change.path, 0) + 1
        if change.path == target:
            raise ReviewOutputError("unparseable after repair")
        return make_result(change.path, n_findings=1)

    monkeypatch.setattr(reviewer, "review_file", bad_output)

    results = graph.review_files(ten_files)
    bugs = {r.path: r for r in bug_results(results)}
    # Persistent parse failure is recorded immediately — NOT retried to the ceiling.
    assert attempts[target] == 1
    assert bugs[target].status == "unavailable"
    assert all(r.status == "reviewed" for p, r in bugs.items() if p != target)


def test_ineligible_files_skipped_before_llm(monkeypatch, mock_security):
    bug_calls: list[str] = []
    monkeypatch.setattr(
        reviewer,
        "review_file",
        lambda change: bug_calls.append(change.path) or make_result(change.path),
    )
    edge = parser.parse_files(load_fixture("pr_edge_files.json"))
    results = graph.review_files(edge)
    by_path_status = {(r.path, r.source): r for r in results}

    # Binary never reached either LLM pass.
    assert "data.bin" not in bug_calls
    assert "data.bin" not in mock_security
    assert by_path_status[("data.bin", "skipped")].status == "skipped"
    assert by_path_status[("data.bin", "skipped")].reason == "binary file (no patch)"
    # Every input file still appears in the results.
    assert {r.path for r in results} == {f.path for f in edge}
    # Eligible files carry both a bug and a security result.
    eligible = [f for f in edge if f.hunks and not f.is_binary and not f.patch_omitted]
    for f in eligible:
        sources = {r.source for r in results if r.path == f.path}
        assert sources == {"bug", "security"}
    assert isinstance(results[0], FileReviewResult)
