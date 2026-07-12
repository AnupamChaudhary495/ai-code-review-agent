"""Post review findings back to GitHub as a PR review, authenticated as the App.

Findings whose line anchors to the diff become inline review comments; the
rest are folded into the review body. If GitHub still rejects the inline
placement (422), the whole review is retried once with everything in the body
— a review must never be lost to comment-anchoring trivia.
"""

import logging
from typing import Any

import httpx

from ..diffing.models import FileChange
from ..schemas.finding import Finding
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


def post_review(
    repo: str,
    pr_number: int,
    installation_id: int,
    change: FileChange,
    findings: list[Finding],
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Post one PR review; returns the review's html_url."""
    token = client.token_provider.token_for(installation_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = _build_payload(change, findings)

    with httpx.Client(base_url=client.API_BASE, timeout=30, transport=transport) as http:
        response = http.post(
            f"/repos/{repo}/pulls/{pr_number}/reviews", headers=headers, json=payload
        )
        if response.status_code == 422 and payload["comments"]:
            # Anchoring rejected server-side; keep the review, drop inline placement.
            logger.warning(
                "inline comments rejected (422); refiling all findings in the review body",
                extra={"repo": repo, "pr_number": pr_number, "file": change.path},
            )
            fallback = _build_payload(change, findings, body_only=True)
            response = http.post(
                f"/repos/{repo}/pulls/{pr_number}/reviews", headers=headers, json=fallback
            )
        response.raise_for_status()
        review = response.json()

    logger.info(
        "review posted",
        extra={
            "repo": repo,
            "pr_number": pr_number,
            "file": change.path,
            "findings": len(findings),
            "review_url": review.get("html_url"),
        },
    )
    return str(review.get("html_url", ""))
