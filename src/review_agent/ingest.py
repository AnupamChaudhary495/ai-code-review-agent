"""GitHub webhook ingestion (Phase 2): verify, dedup, persist. No processing.

Events land in the webhook_events table keyed by GitHub's delivery GUID;
a replayed delivery is a provable no-op (unique constraint, one row).
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import db
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
async def ingest_github_webhook(request: Request) -> JSONResponse:
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
    return JSONResponse({"status": "stored", "delivery_id": delivery_id})
