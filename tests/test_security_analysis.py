"""Security-analysis node tests: deterministic scan + LLM pass, merged & tagged.

The LLM security pass is mocked; the deterministic secret scan is real. Key
property under test: high-confidence secrets survive even when the LLM pass
fails, because they don't depend on it.
"""

import time

import anthropic
import httpx
import pytest

from review_agent import reviewer
from review_agent.agent.nodes.security_analysis import analyze_security
from review_agent.diffing.models import ChangeType, FileChange, Hunk, SizeTier
from review_agent.reviewer import ReviewOutputError, ReviewResult
from review_agent.schemas.finding import Finding

SECRET_LINE = 'API_SECRET = "live-secret-9f8e7d6c5b4a32100123456789abcdef"'


def make_change(lines: list[str], path: str = "x.py") -> FileChange:
    added = ["+" + ln for ln in lines]
    hunk = Hunk(0, 0, 1, len(added), "", added, len(added), 0)
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
        hunks=[hunk],
    )


def llm_result(path: str, findings: list[Finding]) -> ReviewResult:
    return ReviewResult(
        findings=findings,
        model="claude-test",
        prompt_version="security_review_v1",
        input_tokens=100,
        output_tokens=20,
        repair_used=False,
    )


def sec_finding(path: str, line: int, cwe: str = "CWE-89") -> Finding:
    return Finding(
        file=path,
        line=line,
        category="security",
        severity="high",
        message="llm-found weakness",
        cwe=cwe,
    )


def test_secret_scan_and_llm_findings_are_merged_and_tagged(monkeypatch):
    change = make_change([SECRET_LINE, 'q = "SELECT " + name'], path="a.py")
    monkeypatch.setattr(
        reviewer,
        "review_security",
        lambda c: llm_result(c.path, [sec_finding(c.path, 2)]),
    )

    (result,) = analyze_security({"file": change})["results"]

    assert result.source == "security"
    assert result.status == "reviewed"
    # Deterministic secret finding (line 1) + LLM finding (line 2).
    assert len(result.findings) == 2
    assert any(f.cwe == "CWE-798" and f.line == 1 for f in result.findings)  # secret
    assert any(f.cwe == "CWE-89" and f.line == 2 for f in result.findings)  # llm
    assert all(f.category == "security" for f in result.findings)


def test_secret_survives_llm_failure(monkeypatch):
    change = make_change([SECRET_LINE], path="a.py")

    def failing(c):
        raise ReviewOutputError("unparseable after repair")

    monkeypatch.setattr(reviewer, "review_security", failing)

    (result,) = analyze_security({"file": change})["results"]

    # LLM pass unavailable, but the high-confidence secret is still reported.
    assert result.status == "unavailable"
    assert len(result.findings) == 1
    assert result.findings[0].cwe == "CWE-798"
    assert result.findings[0].severity == "critical"


def test_secret_survives_exhausted_transient_retries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    change = make_change([SECRET_LINE], path="a.py")

    def always_timeout(c):
        raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(reviewer, "review_security", always_timeout)

    (result,) = analyze_security({"file": change})["results"]
    assert result.status == "unavailable"
    assert [f.cwe for f in result.findings] == ["CWE-798"]


def test_clean_file_yields_no_findings(monkeypatch):
    change = make_change(["def add(a, b):", "    return a + b"], path="a.py")
    monkeypatch.setattr(reviewer, "review_security", lambda c: llm_result(c.path, []))

    (result,) = analyze_security({"file": change})["results"]
    assert result.status == "reviewed"
    assert result.findings == []


def test_node_does_not_call_bug_review(monkeypatch):
    change = make_change(["x = 1"], path="a.py")
    monkeypatch.setattr(reviewer, "review_security", lambda c: llm_result(c.path, []))
    # review_file (bug pass) must not be invoked by the security node.
    monkeypatch.setattr(
        reviewer,
        "review_file",
        lambda *a, **k: pytest.fail("security node called the bug reviewer"),
    )
    analyze_security({"file": change})
