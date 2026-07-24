"""Aggregation tests: the dedup rule, the derived summary, and "what changed".

The bar these hold is two-sided. A rule that merges nothing is useless; a rule
that merges two genuinely different findings on one line silently loses a
review comment, which is worse than showing a duplicate. Every over-merge case
below is a finding that MUST survive.

Dedup rule under test (ADR-0003): bucket on exact file+line, cluster on message
token overlap (>=3 shared content words AND overlap coefficient >= 0.6, with a
negation-parity guard), category deliberately excluded from the identity test,
worst severity wins the merge.
"""

from datetime import datetime

import pytest

from helpers import load_fixture
from review_agent.agent.state import FileReviewResult
from review_agent.diffing import parser
from review_agent.diffing.models import ChangeType, FileChange, SizeTier
from review_agent.reporting.synthesis import (
    _content_tokens,
    build_report,
    deduplicate,
    describe_change,
    report_for_pull_request,
)
from review_agent.schemas.finding import Finding


def finding(
    message: str,
    *,
    file: str = "app/db.py",
    line: int | None = 42,
    category: str = "bug",
    severity: str = "high",
    suggestion: str | None = None,
    cwe: str | None = None,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        category=category,
        severity=severity,
        message=message,
        suggestion=suggestion,
        cwe=cwe,
    )


# ---------------------------------------------------------------------------
# The dedup rule fires
# ---------------------------------------------------------------------------


def test_same_issue_from_bug_and_security_merges_into_one():
    """The central case: two passes, one SQL injection, different wording."""
    merged = deduplicate(
        [
            (
                "bug",
                finding(
                    "User input is concatenated into the SQL query string, which allows injection.",
                    category="bug",
                    severity="high",
                ),
            ),
            (
                "security",
                finding(
                    "SQL query string is built by concatenating user input, allowing injection.",
                    category="security",
                    severity="critical",
                    cwe="CWE-89",
                ),
            ),
        ]
    )

    assert len(merged) == 1
    only = merged[0]
    # Worst severity wins — a merge must never downgrade.
    assert only.severity == "critical"
    # Security framing wins the tie; it carries the CWE.
    assert only.category == "security"
    assert only.cwe == "CWE-89"
    assert only.sources == ["bug", "security"]
    assert only.duplicates_merged == 1


def test_identical_messages_merge_regardless_of_category():
    merged = deduplicate(
        [
            ("bug", finding("Unbounded read of the uploaded file into memory.")),
            (
                "performance",
                finding(
                    "Unbounded read of the uploaded file into memory!",
                    category="performance",
                    severity="medium",
                ),
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].severity == "high"
    assert merged[0].sources == ["bug", "performance"]


def test_verbose_and_terse_wording_of_one_issue_still_merges():
    """Pins the overlap-coefficient choice (ADR-0003).

    The security pass writes longer messages than the bug pass. Under Jaccard
    this pair scored 0.55 — below any threshold that still keeps distinct
    findings apart — purely because one message is wordier. A metric that
    punishes a pass for being thorough is measuring the wrong thing.
    """
    terse = finding("Deserializes untrusted input with pickle.", severity="high")
    verbose = finding(
        "The payload is deserialized with pickle directly from untrusted input, which "
        "permits arbitrary code execution during unpickling.",
        category="security",
        severity="critical",
        cwe="CWE-502",
    )
    ta, tb = _content_tokens(terse.message), _content_tokens(verbose.message)
    shared = ta & tb
    assert len(shared) / len(ta | tb) < 0.6, "this pair must be a Jaccard miss to be a regression"

    merged = deduplicate([("bug", terse), ("security", verbose)])
    assert len(merged) == 1
    assert merged[0].severity == "critical"
    assert merged[0].sources == ["bug", "security"]


def test_three_way_merge_counts_every_collapsed_duplicate():
    body = "The database query runs inside the request loop, one query per iteration."
    merged = deduplicate(
        [
            ("bug", finding(body)),
            ("security", finding(body, category="security", severity="low")),
            ("performance", finding(body, category="performance", severity="medium")),
        ]
    )
    assert len(merged) == 1
    assert merged[0].duplicates_merged == 2
    assert merged[0].sources == ["bug", "performance", "security"]


def test_merge_backfills_suggestion_and_cwe_from_losers():
    """Merging must not destroy information the reviewer could act on."""
    merged = deduplicate(
        [
            (
                "security",
                finding(
                    "Password hash is compared with a plain equality check, leaking timing.",
                    severity="critical",
                    category="security",
                ),
            ),
            (
                "bug",
                finding(
                    "Password hash comparison uses plain equality, which leaks timing.",
                    severity="low",
                    suggestion="Use `hmac.compare_digest`.",
                    cwe="CWE-208",
                ),
            ),
        ]
    )
    assert len(merged) == 1
    # Winner had neither; both are recovered from the merged-away finding.
    assert merged[0].suggestion == "Use `hmac.compare_digest`."
    assert merged[0].cwe == "CWE-208"


def test_clustering_is_order_independent():
    """Parallel fan-out must not make the report depend on which node finished first."""
    a = ("bug", finding("Race condition on the shared cache dict during concurrent writes."))
    b = (
        "security",
        finding(
            "Concurrent writes to the shared cache dict are a race condition.",
            category="security",
            severity="critical",
        ),
    )
    c = ("performance", finding("Cache lookup is O(n) over a list.", category="performance"))

    forward = deduplicate([a, b, c])
    backward = deduplicate([c, b, a])
    assert [(f.severity, f.category, f.message) for f in forward] == [
        (f.severity, f.category, f.message) for f in backward
    ]
    assert len(forward) == 2


# ---------------------------------------------------------------------------
# The dedup rule does NOT over-merge — these findings must all survive
# ---------------------------------------------------------------------------


def test_distinct_issues_on_the_same_line_are_both_kept():
    """A null-deref and an authz gap can legitimately share one line."""
    merged = deduplicate(
        [
            ("bug", finding("`user` may be None here, so `.id` raises AttributeError.")),
            (
                "security",
                finding(
                    "No ownership check before returning the record; any authenticated "
                    "caller can read it.",
                    category="security",
                    severity="critical",
                ),
            ),
        ]
    )
    assert len(merged) == 2
    assert {f.category for f in merged} == {"bug", "security"}
    assert all(f.duplicates_merged == 0 for f in merged)


def test_negation_is_not_stripped_so_opposite_claims_stay_separate():
    """ "is sanitised" and "is not sanitised" are opposite claims, not duplicates."""
    merged = deduplicate(
        [
            ("bug", finding("The path argument is not sanitised before use.")),
            (
                "security",
                finding("The path argument is sanitised before use.", category="security"),
            ),
        ]
    )
    assert len(merged) == 2


def test_short_similar_messages_do_not_merge_on_token_noise():
    merged = deduplicate(
        [
            ("bug", finding("Unused import.")),
            ("quality", finding("Unused variable.", category="quality")),
        ]
    )
    assert len(merged) == 2


def test_same_issue_on_different_lines_is_two_findings():
    body = "Response body is read without a timeout, so the call can hang forever."
    merged = deduplicate(
        [
            ("bug", finding(body, line=10)),
            ("security", finding(body, line=88, category="security")),
        ]
    )
    assert len(merged) == 2
    assert {f.line for f in merged} == {10, 88}


def test_same_issue_in_different_files_is_two_findings():
    body = "Response body is read without a timeout, so the call can hang forever."
    merged = deduplicate(
        [
            ("bug", finding(body, file="a.py")),
            ("bug", finding(body, file="b.py")),
        ]
    )
    assert len(merged) == 2
    assert {f.file for f in merged} == {"a.py", "b.py"}


def test_file_level_findings_bucket_separately_from_line_findings():
    body = "Module imports a deprecated crypto backend."
    merged = deduplicate(
        [
            ("security", finding(body, line=None, category="security")),
            ("security", finding(body, line=3, category="security")),
        ]
    )
    assert len(merged) == 2
    assert {f.line for f in merged} == {None, 3}


def test_findings_are_ordered_worst_severity_first_then_line():
    merged = deduplicate(
        [
            ("bug", finding("A low-priority naming nit on this call.", severity="low", line=5)),
            (
                "bug",
                finding(
                    "Critical unchecked deserialization of the payload.",
                    severity="critical",
                    line=9,
                ),
            ),
            ("bug", finding("A medium concern about the retry budget.", severity="medium", line=2)),
        ]
    )
    assert [f.severity for f in merged] == ["critical", "medium", "low"]


# ---------------------------------------------------------------------------
# "What changed" — restated from diff metadata, never invented
# ---------------------------------------------------------------------------


def make_change(**kwargs) -> FileChange:
    defaults = dict(
        path="app/db.py",
        old_path=None,
        change_type=ChangeType.MODIFIED,
        additions=12,
        deletions=3,
        is_binary=False,
        patch_omitted=False,
        language="Python",
        size_tier=SizeTier.SMALL,
        hunks=[],
    )
    defaults.update(kwargs)
    return FileChange(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change,expected",
    [
        (make_change(), "Modified · +12 -3 · Python"),
        (make_change(change_type=ChangeType.ADDED, deletions=0), "New file · +12 -0 · Python"),
        (
            make_change(change_type=ChangeType.REMOVED, additions=0, deletions=40),
            "Deleted file · +0 -40 · Python",
        ),
        (
            make_change(change_type=ChangeType.RENAMED, old_path="app/old_db.py"),
            "Renamed from `app/old_db.py` · +12 -3 · Python",
        ),
        (
            make_change(path="logo.png", is_binary=True, language=None),
            "Modified · +12 -3 · unknown type · binary",
        ),
        (
            make_change(patch_omitted=True),
            "Modified · +12 -3 · Python · patch omitted by GitHub (oversized diff)",
        ),
    ],
)
def test_describe_change_restates_diff_metadata(change, expected):
    assert describe_change(change) == expected


def test_what_changed_degrades_instead_of_inventing_when_metadata_is_absent():
    report = build_report([FileReviewResult(path="ghost.py", status="reviewed", source="bug")])
    assert report.files[0].what_changed == "Change details unavailable."
    assert report.files[0].additions == 0


# ---------------------------------------------------------------------------
# Assembly: stats, verdict, summary
# ---------------------------------------------------------------------------


def result(path, source, status="reviewed", findings=(), **kw) -> FileReviewResult:
    return FileReviewResult(path=path, status=status, source=source, findings=list(findings), **kw)


def test_stats_count_dedup_honestly():
    body = "The query is executed once per row of the outer result set."
    results = [
        result("a.py", "bug", findings=[finding(body, file="a.py")]),
        result(
            "a.py", "performance", findings=[finding(body, file="a.py", category="performance")]
        ),
        result("a.py", "security"),
    ]
    report = build_report(results, [make_change(path="a.py")])

    assert report.stats.findings_before_dedup == 2
    assert report.stats.findings_total == 1
    assert report.stats.duplicates_merged == 1
    assert report.stats.files_with_findings == 1
    assert report.stats.passes_total == 3


@pytest.mark.parametrize(
    "severity,verdict",
    [("critical", "blocking"), ("high", "attention"), ("medium", "advisory"), ("low", "advisory")],
)
def test_verdict_is_derived_from_the_worst_severity(severity, verdict):
    report = build_report(
        [result("a.py", "bug", findings=[finding("An issue.", file="a.py", severity=severity)])],
        [make_change(path="a.py")],
    )
    assert report.verdict == verdict


def test_clean_review_verdict_and_summary():
    report = build_report(
        [result("a.py", "bug"), result("a.py", "security")], [make_change(path="a.py")]
    )
    assert report.verdict == "clean"
    assert report.summary == "Reviewed 1 file and found no issues."


def test_summary_is_deterministic_prose_over_the_counts():
    results = [
        result(
            "a.py",
            "security",
            findings=[
                finding(
                    "Hardcoded API token committed to the repository.",
                    file="a.py",
                    category="security",
                    severity="critical",
                ),
            ],
        ),
        result(
            "a.py",
            "bug",
            findings=[finding("Off-by-one in the slice bound.", file="a.py", severity="medium")],
        ),
        result("b.py", "bug"),
        result("logo.png", "skipped", status="skipped", reason="binary file (no patch)"),
    ]
    report = build_report(
        results,
        [make_change(path="a.py"), make_change(path="b.py"), make_change(path="logo.png")],
    )

    assert report.summary == (
        "Reviewed 2 files and found 2 issues across 1 file — 1 critical, 1 medium. "
        "The critical finding needs attention before merging. "
        "1 file was not reviewed (binary, oversized, or no reviewable changes)."
    )
    assert report.verdict == "blocking"


def test_summary_reports_partial_coverage_when_a_pass_failed():
    results = [
        result("a.py", "bug"),
        result("a.py", "security", status="unavailable", reason="LLM timeout", error_count=3),
    ]
    report = build_report(results, [make_change(path="a.py")])
    assert "1 analysis pass did not complete, so coverage is partial." in report.summary
    assert report.stats.passes_unavailable == 1


def test_empty_pull_request_summarizes_without_crashing():
    report = build_report([], [])
    assert report.summary == "No files were changed in this pull request."
    assert report.verdict == "clean"
    assert report.files == []


def test_files_are_ordered_worst_first():
    results = [
        result("low.py", "bug", findings=[finding("Nit.", file="low.py", severity="low")]),
        result("crit.py", "bug", findings=[finding("Boom.", file="crit.py", severity="critical")]),
        result("clean.py", "bug"),
        result("mid.py", "bug", findings=[finding("Hmm.", file="mid.py", severity="medium")]),
    ]
    report = build_report(
        results, [make_change(path=p) for p in ("low.py", "crit.py", "clean.py", "mid.py")]
    )
    assert [f.path for f in report.files] == ["crit.py", "mid.py", "low.py", "clean.py"]


def test_report_for_pull_request_carries_pr_identity():
    files = parser.parse_files(load_fixture("pr_small_files.json"))
    diff = type(
        "Diff",
        (),
        {"repo": "octo/demo", "pr_number": 42, "head_sha": "deadbeefcafe", "files": files},
    )()
    stamp = datetime(2026, 7, 24, 9, 30)
    report = report_for_pull_request(diff, [result(files[0].path, "bug")], generated_at=stamp)

    assert report.repo == "octo/demo"
    assert report.pr_number == 42
    assert report.head_sha == "deadbeefcafe"
    assert report.generated_at == stamp
    assert report.schema_version == "1.0"
