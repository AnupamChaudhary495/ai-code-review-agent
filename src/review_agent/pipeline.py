"""The end-to-end review job, and the rules deciding when to run one.

This is what finally consumes `webhook_events`: everything before Phase 9
stored an event and stopped. One function, `run_review`, walks the whole
pipeline that previously only `scripts/review_pr.py` could drive by hand:

    fetch_pr_diff -> graph.review_files -> report_for_pull_request -> post_report

It is deliberately a **plain synchronous function**. Every step in it is
blocking I/O against a sync client (psycopg, httpx, LangGraph's `invoke`), so
making it `async def` would buy nothing but the obligation to keep it off the
event loop by hand. The caller schedules it through FastAPI's `BackgroundTasks`,
which runs a sync callable in the threadpool — the event loop stays free and the
webhook response returns immediately. See ADR-0004 for why that, and not a
durable queue, is the right amount of machinery for this phase.

`run_review` never raises. A background task that raises has nowhere to report,
so every failure is recorded on the review row and logged instead.
"""

import logging
from typing import Any

from . import db
from .agent import graph
from .github import client, delivery
from .github.auth import make_app_jwt
from .github.diff_fetcher import fetch_pr_diff
from .reporting import render_dict, report_for_pull_request

logger = logging.getLogger(__name__)

# The only events that start a review. `closed`, `labeled`, `assigned` and
# every non-pull_request event are still stored by ingest — they just do not
# mean "there is new code to look at".
TRIGGER_EVENT = "pull_request"
TRIGGER_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


class TriggerPayloadError(ValueError):
    """A pull_request payload that should trigger a review but cannot be read."""


def should_trigger(event: str, action: str | None) -> bool:
    """Whether this delivery means "review this PR"."""
    return event == TRIGGER_EVENT and action in TRIGGER_ACTIONS


def extract_trigger(payload: dict[str, Any]) -> tuple[str, int, str, int | None]:
    """Pull (repo, pr_number, head_sha, installation_id) out of a PR payload.

    `installation_id` is None when the payload carries no `installation` block
    — which happens for deliveries from a webhook configured outside the App
    installation. `run_review` looks it up from the repo in that case rather
    than failing.
    """
    try:
        repo = payload["repository"]["full_name"]
        pull_request = payload["pull_request"]
        pr_number = int(pull_request["number"])
        head_sha = pull_request["head"]["sha"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TriggerPayloadError(f"unreadable pull_request payload: {exc}") from exc

    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    return repo, pr_number, head_sha, int(installation_id) if installation_id else None


def _resolve_installation_id(repo: str, installation_id: int | None) -> int:
    if installation_id is not None:
        return installation_id
    # No installation block on the delivery; ask the App which installation
    # covers this repo. Costs one extra API call, only on that path.
    logger.info("no installation id on payload; resolving from repo", extra={"repo": repo})
    return client.fetch_repo_installation(make_app_jwt(), repo)


def run_review(
    review_id: int,
    repo: str,
    pr_number: int,
    installation_id: int | None = None,
) -> None:
    """Run one full review and record the outcome. Never raises.

    Status walks pending -> running -> completed, or -> failed. The report is
    persisted as soon as it is synthesized, *before* delivery is attempted, so
    a GitHub failure costs the comment and not the analysis.
    """
    log = {"review_id": review_id, "repo": repo, "pr_number": pr_number}
    try:
        db.set_review_status(review_id, "running")

        resolved = _resolve_installation_id(repo, installation_id)
        diff = fetch_pr_diff(repo, pr_number, resolved)
        results = graph.review_files(diff.files)
        report = report_for_pull_request(diff, results)

        db.store_report(review_id, render_dict(report))
        logger.info(
            "review analysis complete",
            extra={
                **log,
                "files": report.stats.files_total,
                "findings": report.stats.findings_total,
                "verdict": report.verdict,
            },
        )

        review_url = delivery.post_report(repo, pr_number, resolved, report, diff.files)
        db.complete_review(review_id, review_url)
        logger.info("review delivered", extra={**log, "review_url": review_url})
    except Exception as exc:  # noqa: BLE001 - a background job must not escape
        # Deliberately broad: this runs detached from any request, so an
        # unhandled exception would vanish into the threadpool and leave the
        # row stuck at "running" forever. Record it and move on.
        logger.exception("review failed", extra={**log, "error": type(exc).__name__})
        try:
            db.fail_review(review_id, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            # The database is the thing that failed. Nothing left but the log.
            logger.exception("could not record review failure", extra=log)
