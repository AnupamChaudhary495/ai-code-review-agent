"""Paginated PR diff acquisition, authenticated as the GitHub App.

All requests carry an installation access token from github.client's
token provider — never a personal access token. GitHub's pulls/files
endpoint returns at most 3000 files; the fetcher compares what it received
against the PR's changed_files count and flags truncation instead of
silently dropping files.
"""

import logging
from dataclasses import dataclass

import httpx

from ..diffing import parser
from ..diffing.models import FileChange
from . import client

logger = logging.getLogger(__name__)

_PER_PAGE = 100


@dataclass
class PullRequestDiff:
    repo: str
    pr_number: int
    head_sha: str
    total_changed_files: int  # from PR metadata; authoritative even when truncated
    files: list[FileChange]
    truncated: bool  # True when GitHub withheld entries (>3000-file PR)


def fetch_pr_diff(
    repo: str,
    pr_number: int,
    installation_id: int,
    transport: httpx.BaseTransport | None = None,
) -> PullRequestDiff:
    """Fetch and parse every changed file GitHub will serve for a PR."""
    token = client.token_provider.token_for(installation_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(base_url=client.API_BASE, timeout=30, transport=transport) as http:
        response = http.get(f"/repos/{repo}/pulls/{pr_number}", headers=headers)
        response.raise_for_status()
        pr = response.json()
        total_changed_files = int(pr["changed_files"])
        head_sha = pr["head"]["sha"]

        raw_files: list[dict] = []
        page = 1
        while True:
            response = http.get(
                f"/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": _PER_PAGE, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            raw_files.extend(batch)
            if len(batch) < _PER_PAGE:
                break
            page += 1

    truncated = len(raw_files) < total_changed_files
    if truncated:
        logger.warning(
            "GitHub truncated the PR file list",
            extra={
                "repo": repo,
                "pr_number": pr_number,
                "files_served": len(raw_files),
                "changed_files": total_changed_files,
            },
        )

    files = parser.parse_files(raw_files)
    logger.info(
        "fetched PR diff",
        extra={
            "repo": repo,
            "pr_number": pr_number,
            "files": len(files),
            "truncated": truncated,
        },
    )
    return PullRequestDiff(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        total_changed_files=total_changed_files,
        files=files,
        truncated=truncated,
    )
