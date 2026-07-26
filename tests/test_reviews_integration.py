"""Reviews-table persistence against real PostgreSQL.

Everything else in Phase 9 runs against conftest's in-memory FakeStore, which
mirrors these semantics but cannot prove the SQL. This module is where the
actual statements run: the `WHERE NOT EXISTS` guard, the JSONB report
round-trip, and the ordering/filtering the history endpoint depends on.

Requires TEST_DATABASE_URL (CI provides a postgres:16 service container).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; requires a running PostgreSQL",
)


@pytest.fixture
def pg_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    from review_agent.config import get_settings

    get_settings.cache_clear()
    from review_agent import db

    db.init_schema()
    with db._connect() as conn:
        conn.execute("TRUNCATE reviews RESTART IDENTITY")
    yield db
    get_settings.cache_clear()


def test_claim_review_creates_a_pending_row(pg_db):
    review_id = pg_db.claim_review("octo/demo", 7, "sha-a")

    assert review_id is not None
    row = pg_db.get_review(review_id)
    assert row["repo"] == "octo/demo"
    assert row["pr_number"] == 7
    assert row["head_sha"] == "sha-a"
    assert row["status"] == "pending"
    assert row["report"] is None
    assert row["error"] is None
    assert row["created_at"] is not None


@pytest.mark.parametrize("blocking_status", ["pending", "running", "completed"])
def test_guard_blocks_a_second_review_for_the_same_head_sha(pg_db, blocking_status):
    first = pg_db.claim_review("octo/demo", 7, "sha-a")
    pg_db.set_review_status(first, blocking_status)

    assert pg_db.claim_review("octo/demo", 7, "sha-a") is None
    assert len(pg_db.list_reviews()) == 1


def test_guard_does_not_block_after_a_failure(pg_db):
    first = pg_db.claim_review("octo/demo", 7, "sha-a")
    pg_db.fail_review(first, "APITimeoutError: boom")

    second = pg_db.claim_review("octo/demo", 7, "sha-a")

    assert second is not None and second != first
    assert len(pg_db.list_reviews()) == 2


def test_guard_is_scoped_to_repo_and_head_sha(pg_db):
    pg_db.claim_review("octo/demo", 7, "sha-a")

    # Same repo, different commit -> allowed (a real push must be reviewed).
    assert pg_db.claim_review("octo/demo", 7, "sha-b") is not None
    # Same commit SHA, different repo -> allowed (SHAs are not globally unique).
    assert pg_db.claim_review("other/repo", 1, "sha-a") is not None


def test_force_bypasses_the_guard(pg_db):
    pg_db.claim_review("octo/demo", 7, "sha-a")

    assert pg_db.claim_review("octo/demo", 7, "sha-a") is None
    assert pg_db.claim_review("octo/demo", 7, "sha-a", force=True) is not None


def test_report_round_trips_through_jsonb(pg_db):
    review_id = pg_db.claim_review("octo/demo", 7, "sha-a")
    report = {
        "schema_version": "1.0",
        "verdict": "blocking",
        "summary": "Reviewed 2 files — 1 critical.",
        "stats": {"findings_total": 1, "severity_counts": {"critical": 1}},
        "files": [{"path": "a.py", "findings": [{"line": 3, "cwe": "CWE-798", "message": "x"}]}],
    }

    pg_db.store_report(review_id, report)
    pg_db.complete_review(review_id, "https://github.com/octo/demo/pull/7#r1")

    row = pg_db.get_review(review_id)
    assert row["status"] == "completed"
    assert row["review_url"].endswith("#r1")
    # Nested structure survives intact, not stringified.
    assert row["report"] == report
    assert row["report"]["files"][0]["findings"][0]["cwe"] == "CWE-798"


def test_store_report_survives_a_later_failure(pg_db):
    """The analysis is kept even when delivery fails afterwards."""
    review_id = pg_db.claim_review("octo/demo", 7, "sha-a")
    pg_db.store_report(review_id, {"schema_version": "1.0", "verdict": "clean"})
    pg_db.fail_review(review_id, "HTTPStatusError: 403")

    row = pg_db.get_review(review_id)
    assert row["status"] == "failed"
    assert row["error"].startswith("HTTPStatusError")
    assert row["report"]["verdict"] == "clean"


def test_status_transitions_update_the_timestamp(pg_db):
    review_id = pg_db.claim_review("octo/demo", 7, "sha-a")
    created = pg_db.get_review(review_id)["updated_at"]

    pg_db.set_review_status(review_id, "running")

    assert pg_db.get_review(review_id)["updated_at"] >= created


def test_get_review_returns_none_for_an_unknown_id(pg_db):
    assert pg_db.get_review(999999) is None


def test_list_reviews_orders_newest_first_and_filters(pg_db):
    a = pg_db.claim_review("octo/demo", 1, "sha-1")
    pg_db.claim_review("octo/demo", 2, "sha-2")
    c = pg_db.claim_review("other/repo", 3, "sha-3")
    pg_db.fail_review(a, "boom")

    everything = pg_db.list_reviews()
    assert [r["id"] for r in everything] == [c, c - 1, a]

    by_repo = pg_db.list_reviews(repo="octo/demo")
    assert len(by_repo) == 2 and {r["repo"] for r in by_repo} == {"octo/demo"}

    by_pr = pg_db.list_reviews(repo="octo/demo", pr_number=2)
    assert len(by_pr) == 1 and by_pr[0]["pr_number"] == 2

    by_status = pg_db.list_reviews(status="failed")
    assert len(by_status) == 1 and by_status[0]["id"] == a


def test_list_reviews_omits_the_report_body_but_flags_it(pg_db):
    review_id = pg_db.claim_review("octo/demo", 7, "sha-a")
    pg_db.claim_review("octo/demo", 8, "sha-b")
    pg_db.store_report(review_id, {"schema_version": "1.0"})

    rows = {r["id"]: r for r in pg_db.list_reviews()}

    assert "report" not in rows[review_id]
    assert rows[review_id]["has_report"] is True
    assert rows[review_id + 1]["has_report"] is False


def test_list_reviews_paginates(pg_db):
    ids = [pg_db.claim_review("octo/demo", n, f"sha-{n}") for n in range(1, 6)]

    first = pg_db.list_reviews(limit=2)
    second = pg_db.list_reviews(limit=2, offset=2)

    assert [r["id"] for r in first] == ids[::-1][:2]
    assert [r["id"] for r in second] == ids[::-1][2:4]


def test_init_schema_is_idempotent(pg_db):
    review_id = pg_db.claim_review("octo/demo", 7, "sha-a")
    pg_db.init_schema()  # CREATE TABLE IF NOT EXISTS, twice
    assert pg_db.get_review(review_id) is not None
