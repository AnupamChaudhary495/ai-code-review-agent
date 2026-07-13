"""Manual trigger: run the multi-file review graph over a real PR.

Fetches the PR diff as the GitHub App, fans out one bug-analysis per eligible
file through the LangGraph graph, and prints one result per file. No delivery,
no aggregation, no webhook — Phase 5 produces per-file results with a log
trail, nothing more.

Requires GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY (App installed on the repo)
and ANTHROPIC_API_KEY.

Usage:
    python scripts/review_pr.py owner/repo 123 [--installation-id N]
"""

import argparse
import os
import sys

from review_agent.agent.graph import review_pull_request
from review_agent.github import client
from review_agent.github.auth import make_app_jwt
from review_agent.github.diff_fetcher import fetch_pr_diff
from review_agent.logging_setup import configure_logging


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("repo", help='repository as "owner/name"')
    cli.add_argument("pr_number", type=int)
    cli.add_argument("--installation-id", type=int, default=None)
    args = cli.parse_args()

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))

    installation_id = args.installation_id
    if installation_id is None and os.environ.get("GITHUB_APP_INSTALLATION_ID"):
        installation_id = int(os.environ["GITHUB_APP_INSTALLATION_ID"])
    if installation_id is None:
        installation_id = client.fetch_repo_installation(make_app_jwt(), args.repo)

    diff = fetch_pr_diff(args.repo, args.pr_number, installation_id)
    print(f"{args.repo}#{args.pr_number}: {len(diff.files)} file(s) in the diff\n")

    results = review_pull_request(diff)
    reviewed = sum(1 for r in results if r.status == "reviewed")
    findings_total = sum(len(r.findings) for r in results)
    print(
        f"graph produced {len(results)} result(s): "
        f"{reviewed} reviewed, {findings_total} finding(s) total\n"
    )

    for r in results:
        header = f"[{r.status}] {r.path}"
        if r.status == "reviewed":
            header += f" — {len(r.findings)} finding(s), model={r.model}, retries={r.error_count}"
        elif r.reason:
            header += f" — {r.reason}"
        print(header)
        for f in r.findings:
            line = f"L{f.line}" if f.line else "file"
            print(f"    [{f.severity}/{f.category}] {r.path}:{line} — {f.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
