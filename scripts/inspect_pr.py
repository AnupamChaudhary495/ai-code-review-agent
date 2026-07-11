"""Inspect a real PR as structured diff data (Phase 3 standalone check).

Authenticates as the GitHub App (installation token) — requires GITHUB_APP_ID
and GITHUB_APP_PRIVATE_KEY in the environment/.env. The installation ID is
taken from --installation-id, the GITHUB_APP_INSTALLATION_ID env var, or
discovered automatically for the target repo.

Usage:
    python scripts/inspect_pr.py owner/repo 123 [--installation-id N] [--json]
"""

import argparse
import dataclasses
import json
import os
import sys

from review_agent.github import client
from review_agent.github.auth import make_app_jwt
from review_agent.github.diff_fetcher import fetch_pr_diff


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("repo", help='repository as "owner/name"')
    cli.add_argument("pr_number", type=int)
    cli.add_argument("--installation-id", type=int, default=None)
    cli.add_argument("--json", action="store_true", help="dump full JSON instead of a table")
    args = cli.parse_args()

    installation_id = args.installation_id
    if installation_id is None and os.environ.get("GITHUB_APP_INSTALLATION_ID"):
        installation_id = int(os.environ["GITHUB_APP_INSTALLATION_ID"])
    if installation_id is None:
        installation_id = client.fetch_repo_installation(make_app_jwt(), args.repo)
        print(f"# discovered installation id {installation_id} for {args.repo}\n")

    diff = fetch_pr_diff(args.repo, args.pr_number, installation_id)

    if args.json:
        json.dump(dataclasses.asdict(diff), sys.stdout, indent=2)
        print()
        return 0

    print(f"{diff.repo}#{diff.pr_number} @ {diff.head_sha[:9]}")
    print(
        f"files served: {len(diff.files)} / {diff.total_changed_files} "
        f"(truncated by GitHub: {diff.truncated})\n"
    )
    header = f"{'change':<9} {'+':>5} {'-':>5} {'hunks':>5} {'lang':<10} {'tier':<6} path"
    print(header)
    print("-" * len(header))
    for f in diff.files:
        flags = " [binary]" if f.is_binary else (" [patch omitted]" if f.patch_omitted else "")
        rename = f"  (from {f.old_path})" if f.old_path else ""
        print(
            f"{f.change_type:<9} {f.additions:>5} {f.deletions:>5} {len(f.hunks):>5} "
            f"{f.language or '-':<10} {f.size_tier:<6} {f.path}{rename}{flags}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
