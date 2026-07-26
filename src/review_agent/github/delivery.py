"""Post review findings back to GitHub as a PR review, authenticated as the App.

Findings whose line anchors to the diff become inline review comments; the
rest are folded into the review body. If GitHub still rejects the inline
placement (422), the whole review is retried once with everything in the body
— a review must never be lost to comment-anchoring trivia.

Two entry points:

- `post_review` (Phase 4) — one file, one review. Kept because it is the
  narrow, well-tested primitive the eval scripts use.
- `post_report` (Phase 9) — one whole `ReviewReport`, one review. This is what
  the automated pipeline posts: the Phase 8 Markdown as the review body, plus
  inline comments on every finding that anchors to the diff. Both share the
  anchoring and 422-fallback logic below.
"""

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from ..diffing.models import FileChange
from ..reporting.markdown import render_markdown
from ..schemas.finding import Finding
from ..schemas.review_report import ReviewReport
from . import client

logger = logging.getLogger(__name__)

_SEVERITY_BADGES = {"critical": "🟥", "high": "🟧", "medium": "🟨", "low": "🟩"}
_MARKER = "<!-- review-agent -->"


def _render_finding(finding: Finding, with_location: bool = False) -> str:
    badge = _SEVERITY_BADGES.get(finding.severity, "▫️")
    location = ""
    if with_location:
        location = f" — `{finding.file}`" + (f" line {finding.line}" if finding.line else "")
    text = f"{badge} **{finding.severity} · {finding.category}**{location}\n\n{finding.message}"
    if finding.suggestion:
        text += f"\n\n**Suggested fix:** {finding.suggestion}"
    return text


def _build_payload(
    change: FileChange, findings: list[Finding], body_only: bool = False
) -> dict[str, Any]:
    anchorable: set[int] = set()
    for hunk in change.hunks:
        anchorable |= hunk.new_lines()

    inline = [f for f in findings if not body_only and f.line is not None and f.line in anchorable]
    in_body = [f for f in findings if f not in inline]

    if findings:
        body_parts = [
            f"{_MARKER}\n**AI review** of `{change.path}` — "
            f"{len(findings)} finding{'s' if len(findings) != 1 else ''}."
        ]
        body_parts.extend(_render_finding(f, with_location=True) for f in in_body)
        body = "\n\n---\n\n".join(body_parts)
    else:
        body = f"{_MARKER}\n**AI review** of `{change.path}` — no issues found. ✅"

    return {
        "event": "COMMENT",
        "body": body,
        "comments": [
            {"path": change.path, "line": f.line, "side": "RIGHT", "body": _render_finding(f)}
            for f in inline
        ],
    }


def _submit(
    repo: str,
    pr_number: int,
    installation_id: int,
    payload: dict[str, Any],
    fallback: dict[str, Any],
    transport: httpx.BaseTransport | None,
    log_extra: dict[str, Any],
) -> str:
    """POST a review, retrying body-only once if GitHub rejects the anchoring."""
    token = client.token_provider.token_for(installation_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    path = f"/repos/{repo}/pulls/{pr_number}/reviews"

    with httpx.Client(base_url=client.API_BASE, timeout=30, transport=transport) as http:
        response = http.post(path, headers=headers, json=payload)
        if response.status_code == 422 and payload["comments"]:
            # Anchoring rejected server-side; keep the review, drop inline placement.
            logger.warning(
                "inline comments rejected (422); refiling all findings in the review body",
                extra={"repo": repo, "pr_number": pr_number, **log_extra},
            )
            response = http.post(path, headers=headers, json=fallback)
        response.raise_for_status()
        review = response.json()

    logger.info(
        "review posted",
        extra={
            "repo": repo,
            "pr_number": pr_number,
            "review_url": review.get("html_url"),
            **log_extra,
        },
    )
    return str(review.get("html_url", ""))


def post_review(
    repo: str,
    pr_number: int,
    installation_id: int,
    change: FileChange,
    findings: list[Finding],
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Post one PR review for a single file; returns the review's html_url."""
    return _submit(
        repo,
        pr_number,
        installation_id,
        _build_payload(change, findings),
        _build_payload(change, findings, body_only=True),
        transport,
        {"file": change.path, "findings": len(findings)},
    )


def _build_report_payload(
    report: ReviewReport, files: Sequence[FileChange], body_only: bool = False
) -> dict[str, Any]:
    """One review for the whole PR: the rendered report as the body, plus
    inline comments wherever a finding anchors to the diff.

    A finding that anchors appears twice on purpose — inline where it is
    actionable, and in the body, which stays the complete record of what the
    run found. Rendering a second, finding-subtracted variant of the report to
    avoid that would mean a second rendering mode and two ways for the Markdown
    to be wrong.
    """
    anchorable: dict[str, set[int]] = {}
    for change in files:
        lines: set[int] = set()
        for hunk in change.hunks:
            lines |= hunk.new_lines()
        anchorable[change.path] = lines

    comments: list[dict[str, Any]] = []
    if not body_only:
        for file_report in report.files:
            for finding in file_report.findings:
                if finding.line is not None and finding.line in anchorable.get(
                    file_report.path, set()
                ):
                    comments.append(
                        {
                            "path": file_report.path,
                            "line": finding.line,
                            "side": "RIGHT",
                            "body": _render_finding(finding),
                        }
                    )

    return {"event": "COMMENT", "body": render_markdown(report), "comments": comments}


def post_report(
    repo: str,
    pr_number: int,
    installation_id: int,
    report: ReviewReport,
    files: Sequence[FileChange],
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Post an aggregated `ReviewReport` as ONE PR review; returns its html_url.

    This is the Phase 9 pipeline's delivery step. `files` is the same
    `FileChange` list the review ran over — it supplies the hunks that decide
    which findings can be anchored inline.
    """
    return _submit(
        repo,
        pr_number,
        installation_id,
        _build_report_payload(report, files),
        _build_report_payload(report, files, body_only=True),
        transport,
        {"files": len(report.files), "findings": report.stats.findings_total},
    )
