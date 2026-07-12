"""Manual trigger: fetch -> review -> deliver for ONE file of a real PR.

Runs the full Phase 4 loop and leaves a real review comment on the PR.
Webhook wiring is deliberately absent — this is the manual path per the
roadmap. Requires GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY (App installed on
the repo) and ANTHROPIC_API_KEY.

Usage:
    python scripts/manual_review_single_file.py owner/repo 123 [--file PATH]
        [--installation-id N] [--dry-run]
"""

import argparse
import os
import sys

from review_agent.github import client, delivery
from review_agent.github.auth import make_app_jwt
from review_agent.github.diff_fetcher import fetch_pr_diff
from review_agent.logging_setup import configure_logging
from review_agent.reviewer import review_file


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("repo", help='repository as "owner/name"')
    cli.add_argument("pr_number", type=int)
    cli.add_argument("--file", default=None, help="path to review (default: first reviewable)")
    cli.add_argument("--installation-id", type=int, default=None)
    cli.add_argument("--dry-run", action="store_true", help="review but do not post to GitHub")
    args = cli.parse_args()

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))

    installation_id = args.installation_id
    if installation_id is None and os.environ.get("GITHUB_APP_INSTALLATION_ID"):
        installation_id = int(os.environ["GITHUB_APP_INSTALLATION_ID"])
    if installation_id is None:
        installation_id = client.fetch_repo_installation(make_app_jwt(), args.repo)

    diff = fetch_pr_diff(args.repo, args.pr_number, installation_id)

    if args.file:
        try:
            change = next(f for f in diff.files if f.path == args.file)
        except StopIteration:
            print(f"error: {args.file} is not part of {args.repo}#{args.pr_number}")
            return 1
    else:
        try:
            change = next(f for f in diff.files if f.hunks)
        except StopIteration:
            print("error: PR has no file with reviewable hunks")
            return 1
    if not change.hunks:
        print(f"error: {change.path} has no reviewable hunks (binary or omitted patch)")
        return 1

    print(
        f"reviewing {change.path} ({change.language or 'unknown'}, {change.size_tier}, "
        f"{len(change.hunks)} hunk(s)) from {args.repo}#{args.pr_number}"
    )
    result = review_file(change)
    print(
        f"model={result.model} prompt={result.prompt_version} "
        f"tokens={result.input_tokens}in/{result.output_tokens}out "
        f"repair_used={result.repair_used}"
    )

    if not result.findings:
        print("findings: none (clean review)")
    for f in result.findings:
        line = f"L{f.line}" if f.line else "file"
        print(f"  [{f.severity}/{f.category}] {change.path}:{line} — {f.message}")
        if f.suggestion:
            print(f"      fix: {f.suggestion}")

    if args.dry_run:
        print("dry run: nothing posted")
        return 0

    url = delivery.post_review(args.repo, args.pr_number, installation_id, change, result.findings)
    print(f"\nposted review: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
