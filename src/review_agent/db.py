"""Webhook event persistence and dedup (PostgreSQL).

The Phase 1 reviews/idempotency-claim table was retired with the synchronous
webhook-review endpoint; review persistence returns properly in Phase 10.
"""

import logging

import psycopg
from psycopg.types.json import Jsonb

from .config import get_settings

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = (
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


def _connect() -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url)


def init_schema() -> None:
    with _connect() as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
    logger.info("database schema ready")


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
