"""Review persistence and idempotency claims (PostgreSQL)."""

import logging
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from .config import get_settings

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        head_sha TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
        file_path TEXT,
        review_body TEXT,
        error TEXT,
        delivery_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (repo, pr_number, head_sha)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS webhook_events (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        delivery_id TEXT NOT NULL UNIQUE,
        event TEXT NOT NULL,
        action TEXT,
        payload JSONB NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
)


@dataclass
class Claim:
    outcome: str  # "claimed" | "duplicate_in_progress" | "duplicate_completed"
    review_id: int
    review_body: str | None = None


def _connect() -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url)


def init_schema() -> None:
    with _connect() as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
    logger.info("database schema ready")


def claim_review(repo: str, pr_number: int, head_sha: str, delivery_id: str | None) -> Claim:
    """Atomically claim the review for (repo, pr_number, head_sha).

    Duplicate webhook deliveries land on the unique constraint and are reported
    as duplicates instead of triggering a second review. A previously failed
    review may be reclaimed, so GitHub's webhook redelivery acts as a retry.
    """
    with _connect() as conn:
        inserted = conn.execute(
            """
            INSERT INTO reviews (repo, pr_number, head_sha, status, delivery_id)
            VALUES (%s, %s, %s, 'in_progress', %s)
            ON CONFLICT (repo, pr_number, head_sha) DO NOTHING
            RETURNING id
            """,
            (repo, pr_number, head_sha, delivery_id),
        ).fetchone()
        if inserted:
            return Claim("claimed", inserted[0])

        reclaimed = conn.execute(
            """
            UPDATE reviews
            SET status = 'in_progress', delivery_id = %s, error = NULL, updated_at = now()
            WHERE repo = %s AND pr_number = %s AND head_sha = %s AND status = 'failed'
            RETURNING id
            """,
            (delivery_id, repo, pr_number, head_sha),
        ).fetchone()
        if reclaimed:
            return Claim("claimed", reclaimed[0])

        existing = conn.execute(
            """
            SELECT id, status, review_body FROM reviews
            WHERE repo = %s AND pr_number = %s AND head_sha = %s
            """,
            (repo, pr_number, head_sha),
        ).fetchone()
        if existing is None:  # pragma: no cover - insert conflicted, row must exist
            raise RuntimeError("claim conflict but no existing review row")
        review_id, status, review_body = existing
        if status == "completed":
            return Claim("duplicate_completed", review_id, review_body)
        return Claim("duplicate_in_progress", review_id)


def complete_review(review_id: int, file_path: str, review_body: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE reviews
            SET status = 'completed', file_path = %s, review_body = %s, updated_at = now()
            WHERE id = %s
            """,
            (file_path, review_body, review_id),
        )


def fail_review(review_id: int, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
            (error, review_id),
        )


def record_webhook_event(delivery_id: str, event: str, action: str | None, payload: object) -> bool:
    """Persist a webhook delivery keyed by GitHub's delivery GUID.

    Returns False when the delivery ID was already stored — a replayed or
    redelivered event is a no-op enforced by the unique constraint.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO webhook_events (delivery_id, event, action, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING id
            """,
            (delivery_id, event, action, Jsonb(payload)),
        ).fetchone()
    return row is not None
