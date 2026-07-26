"""The background review job: the real `run_review`, only its edges stubbed.

Everything between the edges is production code — the LangGraph fan-out, the
risk pre-filter, the retry runner, the deterministic secret scanner, Phase 8's
synthesis and Markdown rendering. What is stubbed is exactly what leaves the
process: the GitHub diff fetch, the three LLM calls, and the review POST.

The properties that matter here are about the *row*, because that row is the
only thing a restarted process or an API caller can see: status walks
pending -> running -> completed, a failure is recorded rather than raised, and
an analysis that succeeded is never lost to a delivery that did not.
"""

import time

import anthropic
import httpx
import pytest

from helpers import load_fixture
from review_agent import db, pipeline, reviewer
from review_agent.diffing import parser
from review_agent.github import delivery
from review_agent.github.diff_fetcher import PullRequestDiff
from review_agent.reviewer import ReviewResult
from review_agent.schemas.finding import Finding

REVIEW_URL = "https://github.com/octo/demo/pull/7#pullrequestreview-9001"


@pytest.fixture
def diff():
    files = parser.parse_files(load_fixture("pr_eval_files.json"))[:4]
    return PullRequestDiff(
        repo="octo/demo",
        pr_number=7,
        head_sha="cafe1234",
        total_changed_files=len(files),
        files=files,
        truncated=False,
    )


@pytest.fixture
def wired(monkeypatch, fake_store, diff):
    """Stub only the process boundaries; return what crossed them."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls: dict = {"fetch": [], "post": []}

    def fake_fetch(repo, pr_number, installation_id, transport=None):
        calls["fetch"].append((repo, pr_number, installation_id))
        return diff

    def fake_post(repo, pr_number, installation_id, report, files, transport=None):
        calls["post"].append({"repo": repo, "report": report, "files": files})
        return REVIEW_URL

    def review(change):
        return ReviewResult(
            [
                Finding(
                    file=change.path,
                    line=1,
                    category="bug",
                    severity="medium",
                    message=f"Something worth noting in {change.path}.",
                )
            ],
            "claude-test-model",
            "v1",
            900,
            120,
            False,
        )

    monkeypatch.setattr(pipeline, "fetch_pr_diff", fake_fetch)
    monkeypatch.setattr(delivery, "post_report", fake_post)
    for name in ("review_file", "review_security", "review_performance"):
        monkeypatch.setattr(reviewer, name, review)
    return calls


def test_happy_path_walks_pending_to_completed_and_stores_the_report(wired, fake_store):
    review_id = db.claim_review("octo/demo", 7, "cafe1234")
    assert fake_store.reviews[review_id]["status"] == "pending"

    pipeline.run_review(review_id, "octo/demo", 7, installation_id=99)

    row = fake_store.reviews[review_id]
    assert row["status"] == "completed"
    assert row["review_url"] == REVIEW_URL
    assert row["error"] is None

    # The stored report is the real Phase 8 artifact, JSON-ready.
    report = row["report"]
    assert report["schema_version"] == "1.0"
    assert report["repo"] == "octo/demo" and report["pr_number"] == 7
    assert report["head_sha"] == "cafe1234"
    assert report["summary"]

    # 4 stubbed LLM findings (one per file, line 1) + 1 from the REAL
    # deterministic secret scanner on api_keys.py line 3. That fifth finding is
    # the proof that the job runs the production security path and not just the
    # mocked LLM: nothing in this test plants it.
    assert report["stats"]["findings_total"] == 5
    secret = next(
        f
        for file_report in report["files"]
        for f in file_report["findings"]
        if f["cwe"] == "CWE-798"
    )
    assert secret["file"] == "eval/api_keys.py" and secret["severity"] == "critical"

    assert wired["fetch"] == [("octo/demo", 7, 99)]
    assert len(wired["post"]) == 1


def test_status_is_running_while_the_analysis_is_in_flight(wired, fake_store, monkeypatch):
    """A caller polling mid-review must see `running`, not a stale `pending`."""
    review_id = db.claim_review("octo/demo", 7, "cafe1234")
    seen: list[str] = []
    original = reviewer.review_file

    def observing_review(change):
        seen.append(fake_store.reviews[review_id]["status"])
        return original(change)

    monkeypatch.setattr(reviewer, "review_file", observing_review)
    pipeline.run_review(review_id, "octo/demo", 7, installation_id=99)

    assert seen and set(seen) == {"running"}
    assert fake_store.reviews[review_id]["status"] == "completed"


def test_diff_fetch_failure_is_recorded_not_raised(monkeypatch, fake_store):
    def boom(repo, pr_number, installation_id, transport=None):
        raise httpx.ConnectError("github unreachable")

    monkeypatch.setattr(pipeline, "fetch_pr_diff", boom)
    review_id = db.claim_review("octo/demo", 7, "cafe1234")

    pipeline.run_review(review_id, "octo/demo", 7, installation_id=99)  # must not raise

    row = fake_store.reviews[review_id]
    assert row["status"] == "failed"
    assert "ConnectError" in row["error"]
    assert row["report"] is None


def test_delivery_failure_keeps_the_report_it_already_paid_for(wired, monkeypatch, fake_store):
    """The analysis is expensive and unrepeatable; the POST is what usually breaks."""

    def refuse(repo, pr_number, installation_id, report, files, transport=None):
        raise httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("POST", "https://api.github.com"),
            response=httpx.Response(403),
        )

    monkeypatch.setattr(delivery, "post_report", refuse)
    review_id = db.claim_review("octo/demo", 7, "cafe1234")

    pipeline.run_review(review_id, "octo/demo", 7, installation_id=99)

    row = fake_store.reviews[review_id]
    assert row["status"] == "failed"
    assert "HTTPStatusError" in row["error"]
    # The point of the test: the review survived the delivery failure.
    assert row["report"] is not None
    assert row["report"]["stats"]["findings_total"] == 5
    assert row["review_url"] is None


def test_a_failing_llm_pass_still_produces_a_completed_review(wired, monkeypatch, fake_store):
    """Per-file resilience is Phase 5/6's; the job must not turn it into a failure."""

    def timeout(change):
        raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(reviewer, "review_security", timeout)
    review_id = db.claim_review("octo/demo", 7, "cafe1234")

    pipeline.run_review(review_id, "octo/demo", 7, installation_id=99)

    row = fake_store.reviews[review_id]
    assert row["status"] == "completed"
    # And the partial coverage is visible in the report rather than hidden.
    assert row["report"]["stats"]["passes_unavailable"] == 4


def test_missing_installation_id_is_resolved_from_the_repo(wired, monkeypatch, fake_store):
    resolved: list[str] = []

    def fake_lookup(app_jwt, repo, transport=None):
        resolved.append(repo)
        return 4242

    monkeypatch.setattr(pipeline.client, "fetch_repo_installation", fake_lookup)
    monkeypatch.setattr(pipeline, "make_app_jwt", lambda: "jwt-token")

    review_id = db.claim_review("octo/demo", 7, "cafe1234")
    pipeline.run_review(review_id, "octo/demo", 7, installation_id=None)

    assert resolved == ["octo/demo"]
    assert wired["fetch"] == [("octo/demo", 7, 4242)]
    assert fake_store.reviews[review_id]["status"] == "completed"


def test_a_database_failure_while_recording_a_failure_does_not_raise(monkeypatch, fake_store):
    """Last-resort path: the DB is what broke. Nothing left but the log."""

    def boom(*_a, **_k):
        raise httpx.ConnectError("github unreachable")

    def db_down(*_a, **_k):
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(pipeline, "fetch_pr_diff", boom)
    monkeypatch.setattr(db, "fail_review", db_down)
    review_id = fake_store.claim_review("octo/demo", 7, "cafe1234")

    pipeline.run_review(review_id, "octo/demo", 7, installation_id=99)  # must not raise
