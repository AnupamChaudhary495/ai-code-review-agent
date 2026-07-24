"""Rendering tests: Markdown suitable for posting as-is, JSON that agrees with it.

The scenario every test here shares is a REAL graph run over the golden-set
fixture corpus (`pr_eval_files.json`) plus two unreviewable files. Only the LLM
calls are mocked — the router, the risk pre-filter, the retry runner and the
deterministic secret scanner are the production code paths. The findings the
mocks return are the ones a model would plausibly return for those exact lines,
including two deliberate cross-pass overlaps and two same-line-but-distinct
pairs, so the rendered artifact exercises merge and non-merge side by side.

"Well-formed and human-readable" is a judgment call, so these tests check the
structural properties that judgment depends on (heading hierarchy, balanced
<details>, every finding present and attributed, no stray markers) rather than
pretending a string comparison settles it. The rendered output was also read by
a human and is quoted in the Phase 8 write-up.
"""

import re
import time

import anthropic
import httpx
import pytest

from helpers import load_fixture
from review_agent import reviewer
from review_agent.agent import graph
from review_agent.diffing import parser
from review_agent.reporting import (
    build_report,
    parse_json,
    render_dict,
    render_json,
    render_markdown,
)
from review_agent.reporting.markdown import MARKER, _safe_block, _tidy_reason
from review_agent.reviewer import ReviewResult
from review_agent.schemas.finding import Finding

# --------------------------------------------------------------------------
# The scenario
# --------------------------------------------------------------------------


def f(file, line, category, severity, message, suggestion=None, cwe=None) -> Finding:
    return Finding(
        file=file,
        line=line,
        category=category,
        severity=severity,
        message=message,
        suggestion=suggestion,
        cwe=cwe,
    )


# Findings keyed by (source, path). Written against the real fixture source, so
# line numbers point at the code they describe.
PLANTED: dict[tuple[str, str], list[Finding]] = {
    # Cross-pass duplicate #1: one SQL injection, two passes, different lengths.
    ("bug", "eval/sql_query.py"): [
        f(
            "eval/sql_query.py",
            7,
            "bug",
            "high",
            "The username argument is interpolated directly into the SQL query string.",
        )
    ],
    ("security", "eval/sql_query.py"): [
        f(
            "eval/sql_query.py",
            7,
            "security",
            "critical",
            "The SQL query string is built by interpolating the username argument directly "
            "into it, which allows SQL injection.",
            suggestion=(
                "Use a parameterised query:\n\n"
                "```python\n"
                'cursor.execute("SELECT id, email FROM users WHERE username = ?", (username,))\n'
                "```"
            ),
            cwe="CWE-89",
        )
    ],
    # Cross-pass duplicate #2: the LLM security pass restates what the
    # deterministic secret scanner already found. Same source, so this exercises
    # the "reported N times" provenance branch rather than "flagged by A + B".
    ("security", "eval/api_keys.py"): [
        f(
            "eval/api_keys.py",
            3,
            "security",
            "critical",
            "The payment API secret is hardcoded in the module, so it is exposed to anyone "
            "with repository read access.",
            cwe="CWE-798",
        )
    ],
    ("bug", "eval/role_check.py"): [
        f(
            "eval/role_check.py",
            2,
            "bug",
            "critical",
            'The expression `role == "admin" or "superuser"` is always truthy, so '
            "`is_privileged` returns True for every role.",
            suggestion='Compare against a set: `role in {"admin", "superuser"}`.',
        )
    ],
    # Same line, genuinely different issues — both must survive.
    ("security", "eval/http_fetch.py"): [
        f(
            "eval/http_fetch.py",
            5,
            "security",
            "high",
            "TLS certificate verification is disabled with `verify=False`, so the connection "
            "can be intercepted.",
            suggestion="Drop `verify=False` and fix the underlying certificate trust issue.",
            cwe="CWE-295",
        )
    ],
    ("bug", "eval/http_fetch.py"): [
        f(
            "eval/http_fetch.py",
            5,
            "bug",
            "medium",
            "The request has no timeout, so a slow server can hang the caller indefinitely.",
            suggestion="Pass `timeout=10` to `requests.get`.",
        )
    ],
    # Same line again: an off-by-one and a throughput note are not one finding.
    ("bug", "eval/pagination.py"): [
        f(
            "eval/pagination.py",
            4,
            "bug",
            "high",
            "`range(len(pages) - 1)` stops one element early, so the last page is never rendered.",
            suggestion="Iterate the sequence directly: `for page in pages:`.",
        )
    ],
    ("performance", "eval/pagination.py"): [
        f(
            "eval/pagination.py",
            4,
            "performance",
            "low",
            "Each page is rendered one at a time inside the loop with no batching.",
        )
    ],
    ("bug", "eval/data_export.py"): [
        f(
            "eval/data_export.py",
            8,
            "bug",
            "critical",
            "`eval(row)` executes arbitrary code from the input rows.",
            suggestion="Use `json.loads(row)` if the rows are JSON.",
        )
    ],
    # File-level finding (no line) — exercises the file-level render path.
    ("performance", "eval/data_export.py"): [
        f(
            "eval/data_export.py",
            None,
            "performance",
            "medium",
            "The whole result set is accumulated in memory before returning, so peak memory "
            "grows with the input size.",
        )
    ],
    ("bug", "eval/error_handling.py"): [
        f(
            "eval/error_handling.py",
            8,
            "bug",
            "medium",
            "A bare `except:` swallows every error, including KeyboardInterrupt, and hides "
            "malformed-settings failures behind an empty dict.",
            suggestion=(
                "Catch what you can handle and let the rest propagate:\n\n"
                "```python\n"
                "except (OSError, json.JSONDecodeError):\n"
                "    logger.warning('settings unreadable', extra={'path': path})\n"
                "    return {}\n"
                "```"
            ),
        )
    ],
    ("bug", "eval/metrics.py"): [
        f(
            "eval/metrics.py",
            4,
            "bug",
            "medium",
            "`len(samples)` is zero for an empty sample list, raising ZeroDivisionError.",
            suggestion="Return 0.0 when `samples` is empty.",
        )
    ],
}

# The security pass on this file fails persistently, so the report must show
# partial coverage rather than an apparently clean file.
FAILING_SECURITY_PASS = "eval/metrics.py"


@pytest.fixture
def scenario_files():
    """The golden-set corpus plus two files nothing can review."""
    files = parser.parse_files(load_fixture("pr_eval_files.json"))
    edge = {c.path: c for c in parser.parse_files(load_fixture("pr_edge_files.json"))}
    variety = {c.path: c for c in parser.parse_files(load_fixture("pr_variety_files.json"))}
    files.append(edge["data.bin"])  # binary — no patch to review
    files.append(variety["giant_module.py"])  # patch omitted by GitHub
    return files


@pytest.fixture
def report(scenario_files, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    def pass_fn(source):
        def run(change):
            if source == "security" and change.path == FAILING_SECURITY_PASS:
                raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
            findings = PLANTED.get((source, change.path), [])
            return ReviewResult(list(findings), "claude-test-model", "v1", 900, 120, False)

        return run

    for name, source in (
        ("review_file", "bug"),
        ("review_security", "security"),
        ("review_performance", "performance"),
    ):
        monkeypatch.setattr(reviewer, name, pass_fn(source))

    results = graph.review_files(scenario_files)
    return build_report(
        results, scenario_files, repo="octo/demo", pr_number=128, head_sha="9f3c1ab7de45"
    )


# --------------------------------------------------------------------------
# The report itself
# --------------------------------------------------------------------------


def test_scenario_aggregates_into_one_coherent_report(report):
    assert report.stats.files_total == 13
    assert report.stats.files_not_reviewed == 2  # data.bin, giant_module.py
    assert report.verdict == "blocking"

    # Two duplicates merged: the cross-pass SQL injection, and the LLM security
    # finding restating the deterministic secret-scan hit.
    assert report.stats.duplicates_merged == 2
    assert report.stats.findings_before_dedup == report.stats.findings_total + 2

    sql = next(f for f in report.files if f.path == "eval/sql_query.py")
    injection = next(f for f in sql.findings if f.line == 7)
    assert injection.sources == ["bug", "security"]
    assert injection.severity == "critical" and injection.cwe == "CWE-89"

    secret = next(f for f in report.files if f.path == "eval/api_keys.py").findings[0]
    assert secret.duplicates_merged == 1 and secret.sources == ["security"]

    # Distinct findings sharing a line are NOT collapsed.
    http = next(f for f in report.files if f.path == "eval/http_fetch.py")
    assert len(http.findings) == 2 and {x.line for x in http.findings} == {5}
    pages = next(f for f in report.files if f.path == "eval/pagination.py")
    assert len(pages.findings) == 2 and {x.line for x in pages.findings} == {4}

    # A failed pass is recorded, not silently dropped.
    assert report.stats.passes_unavailable == 1


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_markdown_is_well_formed(report):
    md = render_markdown(report)

    assert md.startswith(MARKER)
    assert md.count(MARKER) == 1
    assert md.endswith("\n")
    assert "\n\n\n" not in md  # no runs of blank lines

    # One H2 title; files are H3 beneath it. Nothing else may claim a heading.
    headings = re.findall(r"^(#{1,6})\s", md, re.MULTILINE)
    assert headings[0] == "##"
    assert set(headings) == {"##", "###"}
    assert headings.count("##") == 1

    # Collapsible sections are balanced and each has a summary.
    assert md.count("<details>") == md.count("</details>") == 3
    assert md.count("<summary>") == md.count("</summary>") == 3

    # Markdown tables are rectangular.
    for line in md.splitlines():
        if line.startswith("|"):
            assert line.endswith("|"), line


def test_markdown_reads_like_a_review(report):
    md = render_markdown(report)

    assert "`octo/demo` #128" in md and "commit `9f3c1ab`" in md
    assert report.summary in md
    # Every file that has findings gets a heading and a factual "what changed".
    for file_report in report.files_with_findings():
        assert f"### `{file_report.path}`" in md
        assert f"*{file_report.what_changed}*" in md
    # Derived from the diff, not invented.
    assert "New file · +10 -0 · python" in md

    # Merge provenance is visible, so dedup is auditable in the artifact.
    assert "flagged by bug + security" in md
    assert "reported 2 times" in md

    # Clean, unreviewed and failed-pass detail is present but folded away.
    assert "Reviewed with no findings (3)" in md
    assert "Not reviewed (2)" in md
    assert "binary file (no patch)" in md
    assert "patch omitted by GitHub (oversized diff)" in md
    assert "Analysis coverage" in md
    assert "⚠️" in md  # the failed security pass on metrics.py

    # No object reprs or unrendered placeholders leaked into the prose.
    # (Braces and quotes are NOT a signal — suggestions legitimately contain
    # Python code such as `extra={'path': path}`.)
    for leak in ("ReportFinding(", "FileReport(", "PassOutcome(", "ChangeType.", "SizeTier."):
        assert leak not in md
    assert "None" not in md.replace("returns True for every role", "")


def test_markdown_lists_findings_worst_first(report):
    md = render_markdown(report)
    order = re.findall(r"\*\*(critical|high|medium|low) · ", md)
    ranks = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    # Within the report as a whole, severity never improves then regresses
    # across file sections; per file it is monotonically non-increasing.
    per_file = re.split(r"^### ", md, flags=re.MULTILINE)[1:]
    for section in per_file:
        sevs = [ranks[s] for s in re.findall(r"\*\*(critical|high|medium|low) · ", section)]
        assert sevs == sorted(sevs), section.splitlines()[0]
    assert order[0] == "critical"


def test_markdown_is_deterministic(report):
    assert render_markdown(report) == render_markdown(report)


def test_coverage_notes_group_repeated_reasons_and_keep_the_specific_one(report):
    """The risk pre-filter gives every excluded file the same reason.

    One line per file buried the single note that is actually specific — the
    pass that failed. Grouping keeps the failure legible.
    """
    md = render_markdown(report)
    notes = [ln for ln in md.splitlines() if ln.startswith("- ") and " · " in ln]

    grouped = [ln for ln in notes if "risk pre-filter" in ln]
    assert len(grouped) == 1, "the shared skip reason must appear once, not once per file"
    assert grouped[0].startswith("- performance · 6 files:")
    # The files are still named, just not repeated with the reason.
    assert "`eval/api_keys.py`" in md and "`eval/role_check.py`" in md

    failure = [ln for ln in notes if "metrics.py" in ln]
    assert len(failure) == 1 and "security:" in failure[0]


def test_raw_exception_text_is_trimmed_out_of_the_comment(report):
    """An SDK error string is several sentences and a docs URL. Logs, not a PR."""
    md = render_markdown(report)
    assert "APITimeoutError" in md  # the useful part survives
    assert "docs.anthropic.com" not in md  # the boilerplate does not
    assert "…" in md
    assert all(len(ln) < 400 for ln in md.splitlines() if ln.startswith("- "))


def test_tidy_reason_collapses_whitespace_and_leaves_short_reasons_alone():
    assert _tidy_reason("binary file (no patch)") == "binary file (no patch)"
    assert _tidy_reason("wrapped\n  reason   text") == "wrapped reason text"
    long = "x" * 500
    assert len(_tidy_reason(long)) == 160 and _tidy_reason(long).endswith("…")


def test_markdown_matches_delivery_finding_format(report):
    """The multi-file report must not regress on Phase 4's single-file rendering."""
    from review_agent.github import delivery

    md = render_markdown(report)
    finding = next(x for x in report.all_findings() if x.suggestion and x.severity == "high")
    single = delivery._render_finding(finding)

    # Same badge vocabulary, same "**severity · category**" lede, same fix label.
    assert delivery._SEVERITY_BADGES[finding.severity] in single
    assert f"**{finding.severity} · {finding.category}**" in md
    assert "**Suggested fix:**" in md and "**Suggested fix:**" in single
    # And strictly more context than the single-file version: location and file
    # heading, which delivery only has because it posts one file at a time.
    assert f"line {finding.line}" in md


@pytest.mark.parametrize(
    "raw,banned",
    [
        ("## Injected heading", "\n## "),
        ("---", "\n---\n"),
        ("***", "\n***\n"),
    ],
)
def test_model_text_cannot_break_the_report_structure(raw, banned):
    """Finding messages come from a model; they must not forge report structure."""
    safe = _safe_block(raw)
    assert not safe.lstrip().startswith(("#", "---", "***"))
    assert banned not in f"\n{safe}\n"


def test_code_spans_in_model_text_are_left_alone():
    text = "Use `dict.get()` instead of *indexing* — see `foo.py`."
    assert _safe_block(text) == text


def test_clean_report_renders_without_finding_sections():
    from review_agent.agent.state import FileReviewResult

    report = build_report(
        [FileReviewResult(path="a.py", status="reviewed", source="bug")],
        [],
    )
    md = render_markdown(report)
    assert "found no issues" in md
    assert "### " not in md
    assert md.count("<details>") == md.count("</details>")


# --------------------------------------------------------------------------
# JSON — must never disagree with the Markdown about what was found
# --------------------------------------------------------------------------


def test_json_round_trips_cleanly(report):
    restored = parse_json(render_json(report))

    assert restored.model_dump() == report.model_dump()
    assert restored.verdict == report.verdict
    assert restored.stats.findings_total == report.stats.findings_total
    # Round-tripping is stable, not merely successful once.
    assert render_json(restored) == render_json(report)
    # And the restored report renders to identical Markdown.
    assert render_markdown(restored) == render_markdown(report)


def test_json_contains_every_finding_the_markdown_shows(report):
    md = render_markdown(report)
    payload = render_dict(report)

    json_findings = [
        finding for file_report in payload["files"] for finding in file_report["findings"]
    ]
    assert len(json_findings) == report.stats.findings_total
    assert json_findings, "the scenario must actually produce findings"

    for finding in json_findings:
        # Every JSON finding is visible in the Markdown: its message, its file
        # heading, and its severity/category lede.
        assert finding["message"].splitlines()[0][:60] in md
        assert f"### `{finding['file']}`" in md
        assert f"**{finding['severity']} · {finding['category']}**" in md
        if finding["cwe"]:
            assert finding["cwe"] in md

    # And nothing appears in the Markdown that JSON does not carry.
    rendered_findings = len(re.findall(r"\*\*(?:critical|high|medium|low) · ", md))
    assert rendered_findings == len(json_findings)


def test_json_carries_the_full_report_contract(report):
    payload = render_dict(report)

    assert payload["schema_version"] == "1.0"
    assert payload["repo"] == "octo/demo" and payload["pr_number"] == 128
    assert payload["summary"] == report.summary
    assert payload["verdict"] == report.verdict
    assert payload["stats"]["duplicates_merged"] == 2

    # Provenance and per-file context survive serialization — a JSON consumer
    # sees exactly what a Markdown reader sees.
    sql = next(x for x in payload["files"] if x["path"] == "eval/sql_query.py")
    assert sql["what_changed"] and sql["language"] == "python"
    assert any(x["sources"] == ["bug", "security"] for x in sql["findings"])
    assert {p["source"] for p in sql["passes"]} == {"bug", "security", "performance"}

    unreviewed = next(x for x in payload["files"] if x["path"] == "data.bin")
    assert unreviewed["reviewed"] is False
    assert unreviewed["passes"][0]["reason"] == "binary file (no patch)"
