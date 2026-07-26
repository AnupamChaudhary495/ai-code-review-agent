"""GitHub webhook ingestion: verify, dedup, persist — and, since Phase 9, trigger.

Events land in the webhook_events table keyed by GitHub's delivery GUID;
a replayed delivery is a provable no-op (unique constraint, one row).

Phase 9 adds exactly one thing on top: a stored, non-duplicate delivery that
is a `pull_request` open/synchronize/reopen schedules a background review and
answers 202. Everything else — duplicates, other actions, other event types —
keeps behaving precisely as it did, storing the event and returning 200.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import db, pipeline
from .config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header (HMAC SHA-256, constant-time)."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@router.post("/webhooks/github")
async def ingest_github_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    secret = get_settings().github_webhook_secret.get_secret_value()
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured; refusing webhook")
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("webhook signature verification failed")
        raise HTTPException(status_code=401, detail="invalid signature")

    delivery_id = request.headers.get("X-GitHub-Delivery")
    if not delivery_id:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")
    event = request.headers.get("X-GitHub-Event", "unknown")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    action = payload.get("action") if isinstance(payload, dict) else None

    inserted = await run_in_threadpool(db.record_webhook_event, delivery_id, event, action, payload)
    if not inserted:
        logger.info("duplicate webhook delivery ignored", extra={"event": event})
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id})

    logger.info("webhook event stored", extra={"event": event, "action": action})

    if not pipeline.should_trigger(event, action):
        # Stored, but there is no new code to look at. Phase 2's answer, intact.
        return JSONResponse({"status": "stored", "delivery_id": delivery_id})

    try:
        repo, pr_number, head_sha, installation_id = pipeline.extract_trigger(payload)
    except pipeline.TriggerPayloadError as exc:
        # The event is already stored; a malformed payload is not GitHub's
        # problem to retry, so this is still a 200 — it just triggers nothing.
        logger.error("could not read pull_request payload", extra={"error": str(exc)})
        return JSONResponse({"status": "stored", "delivery_id": delivery_id})

    review_id = await run_in_threadpool(db.claim_review, repo, pr_number, head_sha)
    if review_id is None:
        # A pending/running/completed review already covers this commit.
        logger.info(
            "review already exists for head sha; not scheduling another",
            extra={"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
        )
        return JSONResponse(
            {"status": "review_exists", "delivery_id": delivery_id, "head_sha": head_sha}
        )

    # Sync callable -> Starlette runs it in the threadpool, so the blocking
    # fetch/LLM/delivery work never touches the event loop.
    background_tasks.add_task(pipeline.run_review, review_id, repo, pr_number, installation_id)
    logger.info(
        "review scheduled",
        extra={"review_id": review_id, "repo": repo, "pr_number": pr_number, "action": action},
    )
    return JSONResponse(
        {"status": "review_scheduled", "delivery_id": delivery_id, "review_id": review_id},
        status_code=202,
    )
