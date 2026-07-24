"""Aggregate per-node, per-file results into one ReviewReport.

This is a plain function, not a graph node. There is no fan-out and nothing to
parallelise here — one call, one output — so making it a node would buy state
plumbing and a reducer in exchange for nothing. It runs after
`graph.review_files()` returns.

Everything here is deterministic: dedup by token overlap, counts by counting,
summary by string assembly, "what changed" by reading the diff metadata the
Phase 3 parser already produced. No LLM call.

The dedup rule and its rationale live in
docs/design-decisions/0003-cross-pass-finding-dedup.md; the short version is at
`_is_duplicate` below.
"""

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime

from ..agent.state import FileReviewResult
from ..diffing.models import ChangeType, FileChange
from ..github.diff_fetcher import PullRequestDiff
from ..schemas.finding import Finding
from ..schemas.review_report import (
    PASS_ORDER,
    SEVERITY_RANK,
    FileReport,
    PassOutcome,
    ReportFinding,
    ReviewReport,
    ReviewStats,
    SeverityCounts,
    Verdict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

# When two findings on the same file+line collapse, the surviving framing is
# picked by severity first, then by this precedence. Security wins ties because
# it carries the CWE and the more actionable framing; "quality" loses to
# everything because it is the least specific.
_CATEGORY_PRECEDENCE: dict[str, int] = {"security": 0, "bug": 1, "performance": 2, "quality": 3}

_TOKEN = re.compile(r"[a-z0-9_]+")

_STOPWORDS = frozenset(
    """
    a an the is are was were be been being this that these those it its of in on at to for from
    with within and or but if then than as by can could may might will would should
    which when where while into over under via using use used there their they you your we our
    has have had do does did
    """.split()
)

# Grammatical negations, checked separately from similarity (see `_negated`).
# They are excluded from the stopword list above so they also survive as
# content tokens.
_NEGATIONS = frozenset({"not", "no", "never", "cannot", "without", "neither", "nor"})

# Two messages on the same file+line are "the same issue" when the shorter
# one's content words are largely contained in the longer one's — the overlap
# coefficient, |A∩B| / min(|A|,|B|), not Jaccard.
#
# Jaccard was tried first and rejected on evidence: it punishes two passes for
# describing one issue at different lengths, which is precisely what they do.
# The security pass writes longer messages than the bug pass, so one real SQL
# injection reported by both scored 0.55 Jaccard (below any usable threshold)
# while scoring 0.75 overlap. Penalising a pass for being more thorough is the
# opposite of what this metric is for.
_SIMILARITY_THRESHOLD = 0.6
# Guards short messages, which the overlap coefficient would otherwise treat
# generously: three shared content words is a low bar for prose, but it stops
# "Unused import." and "Unused variable." from merging on token noise.
_MIN_SHARED_TOKENS = 3


def _normalized(message: str) -> str:
    return " ".join(_TOKEN.findall(message.lower()))


def _content_tokens(message: str) -> frozenset[str]:
    return frozenset(
        t for t in _TOKEN.findall(message.lower()) if len(t) > 1 and t not in _STOPWORDS
    )


def _negated(message: str) -> bool:
    """Whether the message makes a negated claim."""
    return bool(_NEGATIONS & set(_TOKEN.findall(message.lower())))


def _is_duplicate(a: Finding, b: Finding) -> bool:
    """True when two findings on the SAME file+line describe the same issue.

    Category is deliberately *not* part of the identity test. The whole point
    of cross-pass aggregation is that the bug pass and the security pass can
    both report one SQL injection under different categories; keying on
    category would preserve exactly the duplicate this phase exists to remove.
    Distinctness comes from the message instead.
    """
    if _normalized(a.message) == _normalized(b.message):
        return True
    # Opposite claims share almost every content word: "the path is sanitised"
    # vs "the path is not sanitised" scores 1.0 on any overlap metric. Word
    # counting cannot see the difference, so negation is checked separately.
    # Parity of presence, not equality of markers, so "no timeout is set" and
    # "the call runs without a timeout" — both negated, different words — are
    # still allowed to merge.
    if _negated(a.message) != _negated(b.message):
        return False
    ta, tb = _content_tokens(a.message), _content_tokens(b.message)
    if not ta or not tb:
        return False
    shared = ta & tb
    if len(shared) < _MIN_SHARED_TOKENS:
        return False
    return len(shared) / min(len(ta), len(tb)) >= _SIMILARITY_THRESHOLD


def _cluster_sort_key(item: tuple[str, Finding]) -> tuple[int, int, str, str]:
    """Order within a file+line bucket. The head of a cluster is its winner."""
    source, finding = item
    return (
        SEVERITY_RANK[finding.severity],
        _CATEGORY_PRECEDENCE.get(finding.category, 99),
        source,
        _normalized(finding.message),
    )


def _merge_cluster(cluster: list[tuple[str, Finding]]) -> ReportFinding:
    """Collapse one cluster of duplicate findings into a single ReportFinding.

    The cluster is pre-sorted worst-first, so the head is the survivor: highest
    severity, then best category precedence. Severity is therefore never
    downgraded by a duplicate that a less confident pass rated lower. A missing
    `suggestion` or `cwe` on the winner is backfilled from the others rather
    than lost — merging must not destroy information.
    """
    _, winner = cluster[0]
    findings = [f for _, f in cluster]
    return ReportFinding(
        file=winner.file,
        line=winner.line,
        category=winner.category,
        severity=winner.severity,
        message=winner.message,
        suggestion=next((f.suggestion for f in findings if f.suggestion), None),
        cwe=next((f.cwe for f in findings if f.cwe), None),
        sources=sorted({source for source, _ in cluster}),
        duplicates_merged=len(cluster) - 1,
    )


def deduplicate(tagged: Iterable[tuple[str, Finding]]) -> list[ReportFinding]:
    """Merge same-file, same-line, same-issue findings across analysis passes.

    Input is (source, finding) pairs so provenance survives the merge. Output
    is ordered worst-severity first, then by line, then by category.
    """
    buckets: dict[tuple[str, int | None], list[tuple[str, Finding]]] = {}
    for source, finding in tagged:
        buckets.setdefault((finding.file, finding.line), []).append((source, finding))

    merged: list[ReportFinding] = []
    for key in sorted(buckets, key=lambda k: (k[0], k[1] if k[1] is not None else -1)):
        # Sorting first makes the greedy clustering below order-independent:
        # the same input set always produces the same clusters regardless of
        # which node happened to finish first.
        items = sorted(buckets[key], key=_cluster_sort_key)
        clusters: list[list[tuple[str, Finding]]] = []
        for item in items:
            for cluster in clusters:
                if _is_duplicate(cluster[0][1], item[1]):
                    cluster.append(item)
                    break
            else:
                clusters.append([item])
        merged.extend(_merge_cluster(c) for c in clusters)

    merged.sort(
        key=lambda f: (
            SEVERITY_RANK[f.severity],
            f.line if f.line is not None else -1,
            _CATEGORY_PRECEDENCE.get(f.category, 99),
            f.message,
        )
    )
    return merged


# ---------------------------------------------------------------------------
# "What changed" — derived from diff metadata, never invented
# ---------------------------------------------------------------------------

_CHANGE_VERBS: dict[ChangeType, str] = {
    ChangeType.ADDED: "New file",
    ChangeType.REMOVED: "Deleted file",
    ChangeType.RENAMED: "Renamed",
    ChangeType.COPIED: "Copied",
    ChangeType.MODIFIED: "Modified",
    ChangeType.CHANGED: "Modified",
    ChangeType.UNCHANGED: "Unchanged",
}

_NO_METADATA = "Change details unavailable."


def describe_change(file: FileChange) -> str:
    """A one-line plain-language summary of one file's diff metadata.

    Strictly a restatement of what the parser already knows — change type,
    added/removed line counts, detected language, and the two flags that
    explain a missing patch. Nothing is inferred about intent.
    """
    verb = _CHANGE_VERBS.get(file.change_type, "Changed")
    if file.change_type in (ChangeType.RENAMED, ChangeType.COPIED) and file.old_path:
        verb = f"{verb} from `{file.old_path}`"
    parts = [verb, f"+{file.additions} -{file.deletions}", file.language or "unknown type"]
    if file.is_binary:
        parts.append("binary")
    if file.patch_omitted:
        parts.append("patch omitted by GitHub (oversized diff)")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Summary + verdict — arithmetic, not prose generation
# ---------------------------------------------------------------------------


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


def _verdict(counts: SeverityCounts) -> Verdict:
    if counts.critical:
        return "blocking"
    if counts.high:
        return "attention"
    if counts.medium or counts.low:
        return "advisory"
    return "clean"


def _summarize(stats: ReviewStats) -> str:
    """Build the overall summary deterministically from the counts."""
    if stats.files_total == 0:
        return "No files were changed in this pull request."

    sentences: list[str] = []
    if stats.files_reviewed == 0:
        sentences.append(
            f"None of the {stats.files_total} changed "
            f"{_plural(stats.files_total, 'file')} could be reviewed."
        )
    elif stats.findings_total == 0:
        sentences.append(
            f"Reviewed {stats.files_reviewed} "
            f"{_plural(stats.files_reviewed, 'file')} and found no issues."
        )
    else:
        breakdown = ", ".join(f"{n} {sev}" for sev, n in stats.severity_counts.nonzero())
        sentences.append(
            f"Reviewed {stats.files_reviewed} "
            f"{_plural(stats.files_reviewed, 'file')} and found {stats.findings_total} "
            f"{_plural(stats.findings_total, 'issue')} across "
            f"{stats.files_with_findings} {_plural(stats.files_with_findings, 'file')} "
            f"— {breakdown}."
        )
        counts = stats.severity_counts
        if counts.critical:
            needs = _plural(counts.critical, "critical finding needs", "critical findings need")
            sentences.append(f"The {needs} attention before merging.")
        elif counts.high:
            sentences.append("No critical issues, but the high-severity findings are worth a look.")

    if stats.files_not_reviewed:
        were = _plural(stats.files_not_reviewed, "file was", "files were")
        sentences.append(
            f"{stats.files_not_reviewed} {were} not reviewed "
            "(binary, oversized, or no reviewable changes)."
        )
    if stats.passes_unavailable:
        sentences.append(
            f"{stats.passes_unavailable} analysis "
            f"{_plural(stats.passes_unavailable, 'pass', 'passes')} did not complete, so coverage "
            "is partial."
        )
    if stats.duplicates_merged:
        sentences.append(
            f"{stats.duplicates_merged} duplicate "
            f"{_plural(stats.duplicates_merged, 'finding')} reported by more than one pass "
            f"{_plural(stats.duplicates_merged, 'was', 'were')} merged."
        )
    return " ".join(sentences)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _pass_sort_key(outcome: PassOutcome) -> tuple[int, str]:
    try:
        return (PASS_ORDER.index(outcome.source), outcome.source)
    except ValueError:
        return (len(PASS_ORDER), outcome.source)


def _file_sort_key(report: FileReport) -> tuple[int, int, str]:
    worst = report.worst_severity
    return (
        SEVERITY_RANK[worst] if worst is not None else len(SEVERITY_RANK),
        -len(report.findings),
        report.path,
    )


def build_report(
    results: Sequence[FileReviewResult],
    files: Sequence[FileChange] = (),
    *,
    repo: str | None = None,
    pr_number: int | None = None,
    head_sha: str | None = None,
    generated_at: datetime | None = None,
) -> ReviewReport:
    """Aggregate a graph run's results into one report.

    `results` is what `graph.review_files()` returns: several results per file,
    one per analysis pass, undeduplicated. `files` is the same `FileChange`
    list that went in — it is the *only* source of the per-file "what changed"
    line, and passing it is what keeps that line factual. Omitting it degrades
    gracefully (the line reads "Change details unavailable.") rather than
    inventing a description.
    """
    by_path: dict[str, FileChange] = {f.path: f for f in files}
    grouped: dict[str, list[FileReviewResult]] = {}
    for result in results:
        grouped.setdefault(result.path, []).append(result)

    file_reports: list[FileReport] = []
    stats = ReviewStats()

    for path in sorted(grouped):
        path_results = grouped[path]
        change = by_path.get(path)

        tagged: list[tuple[str, Finding]] = [
            (r.source, f) for r in path_results for f in r.findings
        ]
        stats.findings_before_dedup += len(tagged)
        findings = deduplicate(tagged)

        outcomes = sorted(
            (
                PassOutcome(
                    source=r.source,
                    status=r.status,
                    reason=r.reason,
                    model=r.model,
                    input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens,
                )
                for r in path_results
            ),
            key=_pass_sort_key,
        )

        file_reports.append(
            FileReport(
                path=path,
                what_changed=describe_change(change) if change else _NO_METADATA,
                change_type=str(change.change_type) if change else None,
                additions=change.additions if change else 0,
                deletions=change.deletions if change else 0,
                language=change.language if change else None,
                reviewed=any(r.status == "reviewed" for r in path_results),
                findings=findings,
                passes=outcomes,
            )
        )

    for file_report in file_reports:
        stats.files_total += 1
        if file_report.reviewed:
            stats.files_reviewed += 1
        else:
            stats.files_not_reviewed += 1
        if file_report.findings:
            stats.files_with_findings += 1
        stats.findings_total += len(file_report.findings)
        stats.duplicates_merged += sum(f.duplicates_merged for f in file_report.findings)
        for severity, count in file_report.severity_counts.items():
            setattr(
                stats.severity_counts,
                severity,
                int(getattr(stats.severity_counts, severity)) + count,
            )
        for outcome in file_report.passes:
            stats.passes_total += 1
            if outcome.status == "reviewed":
                stats.passes_reviewed += 1
            elif outcome.status == "skipped":
                stats.passes_skipped += 1
            elif outcome.status == "unavailable":
                stats.passes_unavailable += 1
            stats.input_tokens += outcome.input_tokens
            stats.output_tokens += outcome.output_tokens

    file_reports.sort(key=_file_sort_key)

    report = ReviewReport(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        generated_at=generated_at,
        summary=_summarize(stats),
        verdict=_verdict(stats.severity_counts),
        stats=stats,
        files=file_reports,
    )
    logger.info(
        "review report built",
        extra={
            "repo": repo,
            "pr_number": pr_number,
            "files": stats.files_total,
            "findings": stats.findings_total,
            "duplicates_merged": stats.duplicates_merged,
            "verdict": report.verdict,
        },
    )
    return report


def report_for_pull_request(
    diff: PullRequestDiff,
    results: Sequence[FileReviewResult],
    generated_at: datetime | None = None,
) -> ReviewReport:
    """`build_report` with the PR identity and diff metadata filled in."""
    return build_report(
        results,
        diff.files,
        repo=diff.repo,
        pr_number=diff.pr_number,
        head_sha=diff.head_sha,
        generated_at=generated_at,
    )
