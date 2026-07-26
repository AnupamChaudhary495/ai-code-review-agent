"""Review API: trigger a run, fetch one review, list history.

The webhook is the automated path; this is the human one. Three endpoints, all
thin — every one of them is a database read or a `claim_review` + schedule,
with the actual work happening in `pipeline.run_review` exactly as it does for
a webhook-triggered review. There is deliberately no second code path for
"manual" reviews.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db, pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reviews", tags=["reviews"])


class TriggerRequest(BaseModel):
    """A manual review request.

    `head_sha` is optional: omitted, the review is recorded against the SHA the
    caller believes is current, which they usually do not know. Supplying it
    makes the duplicate guard meaningful, so callers that have it should send it.
    """

    repo: str = Field(description='Repository as "owner/name"')
    pr_number: int = Field(gt=0)
    head_sha: str = Field(default="", description="Head commit SHA; enables the duplicate guard")
    installation_id: int | None = Field(
        default=None, description="Resolved from the repo when omitted"
    )
    force: bool = Field(
        default=False,
        description="Re-run even if a pending/running/completed review exists for this SHA",
    )


@router.post("", status_code=202)
async def trigger_review(
    request: TriggerRequest, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Schedule a review by hand — the retry/re-run path for a stale or failed run."""
    review_id = await run_in_threadpool(
        db.claim_review, request.repo, request.pr_number, request.head_sha, request.force
    )
    if review_id is None:
        # Not an error: the caller asked for something that already exists.
        # 409 tells them so without pretending a new run was scheduled, and
        # `force` is the documented way through.
        raise HTTPException(
            status_code=409,
            detail=(
                "a pending, running or completed review already exists for this head SHA; "
                "retry with force=true to run it again"
            ),
        )

    background_tasks.add_task(
        pipeline.run_review,
        review_id,
        request.repo,
        request.pr_number,
        request.installation_id,
    )
    logger.info(
        "review scheduled manually",
        extra={"review_id": review_id, "repo": request.repo, "pr_number": request.pr_number},
    )
    return JSONResponse(
        {"status": "review_scheduled", "review_id": review_id, "review_status": "pending"},
        status_code=202,
    )


@router.get("/{review_id}")
async def get_review(review_id: int) -> dict[str, Any]:
    """One review: its status, and its report once it has one."""
    review = await run_in_threadpool(db.get_review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return review


@router.get("")
async def list_reviews(
    repo: Annotated[str | None, Query(description="Filter to one repository")] = None,
    pr_number: Annotated[int | None, Query(gt=0)] = None,
    status: Annotated[str | None, Query(description="pending/running/completed/failed")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Review history, newest first.

    Reports are omitted from the listing (`has_report` says whether one is
    there); follow the id to `GET /reviews/{id}` for the full report.
    """
    rows = await run_in_threadpool(db.list_reviews, repo, pr_number, status, limit, offset)
    return {"count": len(rows), "limit": limit, "offset": offset, "reviews": rows}
