"""Integration tests for the real SQL idempotency logic.

Requires a live PostgreSQL; set TEST_DATABASE_URL to run, e.g.:
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/review_agent_test
Skipped otherwise (the webhook-level semantics are covered via FakeStore).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; requires a running PostgreSQL",
)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    from review_agent.config import get_settings

    get_settings.cache_clear()
    from review_agent import db

    db.init_schema()
    with db._connect() as conn:
        conn.execute("TRUNCATE reviews RESTART IDENTITY")
    yield db
    get_settings.cache_clear()


def test_first_claim_wins_second_is_duplicate(store):
    first = store.claim_review("octo/demo", 1, "sha-1", "dlv-1")
    second = store.claim_review("octo/demo", 1, "sha-1", "dlv-2")
    assert first.outcome == "claimed"
    assert second.outcome == "duplicate_in_progress"
    assert second.review_id == first.review_id


def test_completed_claim_returns_stored_review(store):
    claim = store.claim_review("octo/demo", 2, "sha-2", "dlv-1")
    store.complete_review(claim.review_id, "app.py", "LGTM")

    duplicate = store.claim_review("octo/demo", 2, "sha-2", "dlv-2")
    assert duplicate.outcome == "duplicate_completed"
    assert duplicate.review_body == "LGTM"


def test_failed_review_is_reclaimable(store):
    claim = store.claim_review("octo/demo", 3, "sha-3", "dlv-1")
    store.fail_review(claim.review_id, "ConnectError: boom")

    retry = store.claim_review("octo/demo", 3, "sha-3", "dlv-2")
    assert retry.outcome == "claimed"
    assert retry.review_id == claim.review_id


def test_distinct_head_shas_are_distinct_reviews(store):
    first = store.claim_review("octo/demo", 4, "sha-a", "dlv-1")
    second = store.claim_review("octo/demo", 4, "sha-b", "dlv-2")
    assert first.outcome == "claimed"
    assert second.outcome == "claimed"
    assert first.review_id != second.review_id
