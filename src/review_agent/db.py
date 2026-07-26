"""Webhook event and review persistence (PostgreSQL).

Two tables:

- `webhook_events` (Phase 2) — every delivery, keyed by GitHub's delivery GUID.
- `reviews` (Phase 9) — one row per triggered review run, carrying its status
  and, once finished, the serialized `ReviewReport`. This is the minimum that
  makes "fetch review status/result" and "list history" answerable.

The `reviews` table is deliberately *minimal*. Status is a plain TEXT column
with no transition enforcement, and the guard against duplicate runs is a
`WHERE NOT EXISTS` check rather than a constraint — see ADR-0004. The real
state machine, the partial unique index on (repo, head_sha) and token/cost
tracking are Phase 10's job, not this table's.
"""

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import get_settings

logger = logging.getLogger(__name__)

# A review that exists in one of these states blocks a second run for the same
# head SHA. "failed" is absent on purpose: a failed review is exactly what the
# manual re-run endpoint is for.
BLOCKING_STATUSES = ("pending", "running", "completed")

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
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        head_sha TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        report JSONB,
        error TEXT,
        review_url TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    # History listing is always "this repo, newest first"; the guard always
    # looks up (repo, head_sha). Two indexes, both for queries that exist.
    "CREATE INDEX IF NOT EXISTS reviews_repo_created_idx ON reviews (repo, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS reviews_repo_head_sha_idx ON reviews (repo, head_sha);",
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


# ---------------------------------------------------------------------------
# Reviews (Phase 9)
# ---------------------------------------------------------------------------


def claim_review(repo: str, pr_number: int, head_sha: str, force: bool = False) -> int | None:
    """Create a pending review row, unless this head SHA already has one.

    Returns the new review id, or None when an existing pending/running/
    completed review for the same (repo, head_sha) blocks it.

    STOPGAP, not idempotency (ADR-0004). The guard is a `WHERE NOT EXISTS`
    inside the INSERT, which is one statement but still not race-proof: under
    READ COMMITTED two concurrent transactions can both see no existing row and
    both insert. Closing that needs the partial unique index Phase 10 adds. It
    is enough for the real case this phase has — GitHub redelivering an event,
    or `synchronize` firing twice for one push, seconds apart rather than
    simultaneously.

    `force=True` skips the guard entirely; it is how the manual endpoint
    re-runs a review that is stale or already completed.
    """
    sql: str
    params: tuple[Any, ...]
    if force:
        sql = """
            INSERT INTO reviews (repo, pr_number, head_sha, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING id
            """
        params = (repo, pr_number, head_sha)
    else:
        sql = """
            INSERT INTO reviews (repo, pr_number, head_sha, status)
            SELECT %s, %s, %s, 'pending'
            WHERE NOT EXISTS (
                SELECT 1 FROM reviews
                WHERE repo = %s AND head_sha = %s AND status = ANY(%s)
            )
            RETURNING id
            """
        params = (repo, pr_number, head_sha, repo, head_sha, list(BLOCKING_STATUSES))

    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else None


def set_review_status(review_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = %s, updated_at = now() WHERE id = %s",
            (status, review_id),
        )


def store_report(review_id: int, report: object) -> None:
    """Persist the synthesized report *before* delivery is attempted.

    Split from `complete_review` deliberately: the analysis is the expensive,
    unrepeatable part, and posting to GitHub is the part most likely to fail
    (network, permissions, a 422 on anchoring). Storing first means a delivery
    failure loses the comment, not the review. `report` is a JSON-ready dict.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET report = %s, updated_at = now() WHERE id = %s",
            (Jsonb(report), review_id),
        )


def complete_review(review_id: int, review_url: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE reviews
               SET status = 'completed', review_url = %s, error = NULL, updated_at = now()
             WHERE id = %s
            """,
            (review_url, review_id),
        )


def fail_review(review_id: int, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
            (error, review_id),
        )


def get_review(review_id: int) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM reviews WHERE id = %s", (review_id,)).fetchone()


def list_reviews(
    repo: str | None = None,
    pr_number: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Review history, newest first. The report body is omitted — a history
    listing that inlined every full report would be enormous; callers follow
    the id to `get_review` for the detail."""
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("repo", repo), ("pr_number", pr_number), ("status", status)):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params += [limit, offset]

    with _connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT id, repo, pr_number, head_sha, status, error, review_url,
                   created_at, updated_at,
                   (report IS NOT NULL) AS has_report
              FROM reviews {where}
             ORDER BY created_at DESC, id DESC
             LIMIT %s OFFSET %s
            """,
            params,
        ).fetchall()
