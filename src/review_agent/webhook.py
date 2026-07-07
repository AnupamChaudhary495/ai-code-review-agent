"""GitHub webhook endpoint — the Phase 1 vertical slice.

Flow: verify signature -> filter event -> claim idempotency key -> fetch one
file diff -> one LLM call -> persist and return the review comment.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import db, github_client, reviewer
from .config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

HANDLED_ACTIONS = {"opened", "synchronize", "reopened"}


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header (HMAC SHA-256, constant-time)."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@router.post("/webhook/github")
async def github_webhook(request: Request) -> JSONResponse:
    secret = get_settings().github_webhook_secret.get_secret_value()
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured; refusing webhook")
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("webhook signature verification failed")
        raise HTTPException(status_code=401, detail="invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery")
    if event != "pull_request":
        logger.info("ignoring webhook event", extra={"event": event})
        return JSONResponse({"status": "ignored", "reason": f"unhandled event type: {event}"})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    action = payload.get("action", "")
    if action not in HANDLED_ACTIONS:
        logger.info("ignoring pull_request action", extra={"action": action})
        return JSONResponse({"status": "ignored", "reason": f"unhandled action: {action}"})

    try:
        repo = payload["repository"]["full_name"]
        pr_number = int(payload["pull_request"]["number"])
        head_sha = payload["pull_request"]["head"]["sha"]
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("malformed pull_request payload", extra={"error": type(exc).__name__})
        raise HTTPException(status_code=400, detail="malformed pull_request payload") from exc

    # GitHub client, LLM call, and DB access are all synchronous; run off the
    # event loop so /health stays responsive during a review.
    return await run_in_threadpool(_process_pull_request, repo, pr_number, head_sha, delivery_id)


def _process_pull_request(
    repo: str, pr_number: int, head_sha: str, delivery_id: str | None
) -> JSONResponse:
    context = {"repo": repo, "pr_number": pr_number, "head_sha": head_sha}

    claim = db.claim_review(repo, pr_number, head_sha, delivery_id)
    if claim.outcome == "duplicate_completed":
        logger.info("duplicate delivery for completed review", extra=context)
        return JSONResponse(
            {
                "status": "duplicate",
                "detail": "review already completed",
                "review": claim.review_body,
                **context,
            }
        )
    if claim.outcome == "duplicate_in_progress":
        logger.info("duplicate delivery for in-progress review", extra=context)
        return JSONResponse(
            {"status": "duplicate", "detail": "review already in progress", **context}
        )

    logger.info("review claimed", extra={**context, "review_id": claim.review_id})
    try:
        file_diff = github_client.get_first_file_diff(repo, pr_number)
        review_body = reviewer.review_file_diff(file_diff.filename, file_diff.patch)
        db.complete_review(claim.review_id, file_diff.filename, review_body)
    except github_client.NoReviewableFileError as exc:
        db.fail_review(claim.review_id, str(exc))
        logger.warning("no reviewable file in PR", extra=context)
        return JSONResponse(
            {"status": "skipped", "detail": "no reviewable file in this pull request", **context}
        )
    except Exception as exc:
        db.fail_review(claim.review_id, f"{type(exc).__name__}: {exc}")
        logger.exception("review failed", extra=context)
        # Generic message only — internals and secrets stay in server logs.
        return JSONResponse(
            {"status": "error", "detail": "review failed; see server logs"}, status_code=500
        )

    logger.info("review completed", extra={**context, "file": file_diff.filename})
    return JSONResponse(
        {"status": "completed", "file": file_diff.filename, "review": review_body, **context}
    )
