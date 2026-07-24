"""Render a ReviewReport as GitHub-flavoured Markdown, suitable for posting as-is.

Template-based and deterministic — the same report always renders to the same
bytes. No LLM is involved in producing a single character of this.

The finding block deliberately mirrors `github/delivery.py`'s Phase 4 single-
file rendering (severity badge, `**severity · category**`, message, then
`**Suggested fix:**`), so the multi-file report reads as the same product
rather than a second style. What it adds on top: an overall summary, a severity
tally, per-file "what changed" lines, merge provenance on deduplicated
findings, and collapsed sections for clean/unreviewed files and pass coverage
— detail that matters but should not dominate the comment.
"""

import re

from ..schemas.review_report import (
    PASS_ORDER,
    SEVERITY_BADGES,
    FileReport,
    ReportFinding,
    ReviewReport,
)

# Same marker github/delivery.py writes, so a posted report stays identifiable
# (Phase 10 idempotency will look for it).
MARKER = "<!-- review-agent -->"

_STATUS_ICONS = {"reviewed": "✅", "skipped": "⏭️", "unavailable": "⚠️"}

# Message text comes from a model. A line that is a bare horizontal rule or an
# ATX heading would break the report's own structure when inlined, so those two
# constructs are neutralised. Everything else (emphasis, code spans, lists) is
# left alone — a model writing a code span in its explanation is a good thing.
_HEADING_LINE = re.compile(r"^(\s*)(#{1,6})(\s)")
_RULE_LINE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _safe_block(text: str) -> str:
    lines = []
    for line in text.strip().splitlines():
        if _RULE_LINE.match(line):
            line = line.replace("-", "\\-").replace("*", "\\*").replace("_", "\\_")
        else:
            line = _HEADING_LINE.sub(r"\1\\\2\3", line)
        lines.append(line)
    return "\n".join(lines)


_MAX_REASON_CHARS = 160


def _tidy_reason(reason: str) -> str:
    """Make a machine-generated reason fit in human-facing prose.

    Skip reasons are ours and already short; "unavailable" reasons carry a raw
    exception string, which for an SDK error is multiple sentences plus a docs
    URL. That belongs in the logs, not in a PR comment — the reader needs the
    error type and the fact that the pass did not complete.
    """
    collapsed = " ".join(reason.split())
    if len(collapsed) <= _MAX_REASON_CHARS:
        return collapsed
    return collapsed[: _MAX_REASON_CHARS - 1].rstrip(" .,;:") + "…"


def _severity_tally(report: ReviewReport) -> str:
    return " · ".join(
        f"{SEVERITY_BADGES.get(sev, '▫️')} {n} {sev}"
        for sev, n in report.stats.severity_counts.nonzero()
    )


def _render_finding(finding: ReportFinding) -> str:
    badge = SEVERITY_BADGES.get(finding.severity, "▫️")
    meta = []
    if finding.line is not None:
        meta.append(f"line {finding.line}")
    else:
        meta.append("file-level")
    if finding.cwe:
        meta.append(finding.cwe)
    # Only surface provenance where it says something: more than one pass found
    # this, or duplicates collapsed into it. Makes the dedup auditable in the
    # output instead of quietly discarding the evidence.
    if len(finding.sources) > 1:
        meta.append(f"flagged by {' + '.join(finding.sources)}")
    elif finding.duplicates_merged:
        merged = finding.duplicates_merged
        meta.append(f"reported {merged + 1} times")

    block = [f"{badge} **{finding.severity} · {finding.category}** — {' · '.join(meta)}"]
    block.append(_safe_block(finding.message))
    if finding.suggestion:
        suggestion = _safe_block(finding.suggestion)
        if "\n" in suggestion:
            block.append(f"**Suggested fix:**\n\n{suggestion}")
        else:
            block.append(f"**Suggested fix:** {suggestion}")
    return "\n\n".join(block)


def _render_file(file_report: FileReport) -> str:
    parts = [f"### `{file_report.path}`", f"*{file_report.what_changed}*"]
    parts.extend(_render_finding(f) for f in file_report.findings)
    return "\n\n".join(parts)


def _render_clean_files(report: ReviewReport) -> list[str]:
    clean = report.files_clean()
    if not clean:
        return []
    lines = [
        "<details>",
        f"<summary>Reviewed with no findings ({len(clean)})</summary>",
        "",
    ]
    lines += [f"- `{f.path}` — {f.what_changed}" for f in clean]
    lines += ["", "</details>"]
    return ["\n".join(lines)]


def _render_unreviewed_files(report: ReviewReport) -> list[str]:
    unreviewed = report.files_unreviewed()
    if not unreviewed:
        return []
    lines = [
        "<details>",
        f"<summary>Not reviewed ({len(unreviewed)})</summary>",
        "",
        "| File | Why |",
        "| --- | --- |",
    ]
    for f in unreviewed:
        reason = f.skip_reason or next(
            (p.reason for p in f.passes if p.reason), "no analysis pass ran"
        )
        lines.append(f"| `{f.path}` | {_tidy_reason(reason)} |")
    lines += ["", "</details>"]
    return ["\n".join(lines)]


def _render_coverage(report: ReviewReport) -> list[str]:
    """Per-file × per-pass outcome grid.

    This is what makes "no performance comment on this file" provably a
    pre-filter decision (ADR-0002) rather than a silent gap, and what makes a
    failed pass visible instead of looking like a clean result.
    """
    covered = [f for f in report.files if f.reviewed]
    if not covered:
        return []
    header = " | ".join(p.capitalize() for p in PASS_ORDER)
    lines = [
        "<details>",
        f"<summary>Analysis coverage ({len(covered)} files × {len(PASS_ORDER)} passes)</summary>",
        "",
        f"| File | {header} |",
        "| --- | " + " | ".join(":-:" for _ in PASS_ORDER) + " |",
    ]
    # Grouped by (pass, reason) rather than one line per file: the risk
    # pre-filter gives every file it excludes the *same* reason, and repeating
    # it once per file buries the one note that is actually specific — a pass
    # that failed.
    notes: dict[tuple[str, str], list[str]] = {}
    for f in covered:
        by_source = {p.source: p for p in f.passes}
        cells = []
        for source in PASS_ORDER:
            outcome = by_source.get(source)
            cells.append(_STATUS_ICONS.get(outcome.status, "▫️") if outcome else "—")
            if outcome and outcome.status != "reviewed" and outcome.reason:
                notes.setdefault((source, _tidy_reason(outcome.reason)), []).append(f.path)
        lines.append(f"| `{f.path}` | " + " | ".join(cells) + " |")
    lines += ["", "✅ reviewed · ⏭️ skipped · ⚠️ unavailable · — pass not dispatched"]
    if notes:
        lines.append("")
        for (source, reason), paths in sorted(notes.items()):
            if len(paths) == 1:
                lines.append(f"- `{paths[0]}` · {source}: {reason}")
            else:
                # File list on a continuation line — the reason already
                # contains an em dash, and reusing it as a separator reads as
                # part of the sentence.
                listed = ", ".join(f"`{p}`" for p in sorted(paths))
                lines.append(f"- {source} · {len(paths)} files: {reason}\n  {listed}")
    lines += ["", "</details>"]
    return ["\n".join(lines)]


def render_markdown(report: ReviewReport) -> str:
    """Render the whole report. Deterministic: same report in, same bytes out."""
    heading = "## 🤖 AI code review"
    sections: list[str] = [f"{MARKER}\n{heading}"]

    subtitle_bits = []
    if report.repo and report.pr_number is not None:
        subtitle_bits.append(f"`{report.repo}` #{report.pr_number}")
    if report.head_sha:
        subtitle_bits.append(f"commit `{report.head_sha[:7]}`")
    if report.generated_at:
        subtitle_bits.append(report.generated_at.strftime("%Y-%m-%d %H:%M UTC"))
    if subtitle_bits:
        sections.append("<sub>" + " · ".join(subtitle_bits) + "</sub>")

    sections.append(report.summary)
    tally = _severity_tally(report)
    if tally:
        sections.append(tally)

    with_findings = report.files_with_findings()
    if with_findings:
        sections.append("---")
        sections.extend(_render_file(f) for f in with_findings)

    tail = _render_clean_files(report) + _render_unreviewed_files(report) + _render_coverage(report)
    if tail:
        sections.append("---")
        sections.extend(tail)

    sections.append(
        "<sub>Automated review — findings are suggestions, not merge blockers. "
        "Report generated deterministically from the analysis passes; no model wrote this "
        "summary.</sub>"
    )
    return "\n\n".join(sections) + "\n"
