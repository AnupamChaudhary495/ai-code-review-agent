"""Minimal GitHub REST client: fetch the first reviewable file diff of a PR."""

import logging
from dataclasses import dataclass

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


class NoReviewableFileError(Exception):
    """The PR contains no file with a text patch (e.g. binary-only changes)."""


@dataclass
class FileDiff:
    filename: str
    patch: str
    status: str


def get_first_file_diff(repo: str, pr_number: int) -> FileDiff:
    """Return the first changed file that carries a text patch.

    Phase 1 deliberately reviews a single file only; multi-file handling is a
    later phase. Files without a `patch` field (binary or oversized diffs on
    GitHub's side) are skipped.
    """
    token = get_settings().github_token.get_secret_value()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{API_BASE}/repos/{repo}/pulls/{pr_number}/files"
    with httpx.Client(timeout=30) as client:
        response = client.get(url, headers=headers, params={"per_page": 30})
        response.raise_for_status()
        files = response.json()

    for file in files:
        if file.get("patch"):
            logger.info(
                "fetched file diff",
                extra={"repo": repo, "pr_number": pr_number, "file": file["filename"]},
            )
            return FileDiff(
                filename=file["filename"],
                patch=file["patch"],
                status=file.get("status", ""),
            )
    raise NoReviewableFileError(f"PR {repo}#{pr_number} has no file with a text patch")
