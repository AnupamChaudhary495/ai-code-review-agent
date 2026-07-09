"""Ingestion integration tests against real PostgreSQL persistence.

Requires TEST_DATABASE_URL (CI provides a postgres service container;
locally use an embedded/instance PostgreSQL). These are the Phase 2
completion-criteria tests: replayed delivery -> exactly one row; bad
signature -> 401 and nothing persisted.
"""

import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; requires a running PostgreSQL",
)

SECRET = "integration-webhook-secret"

PAYLOAD = json.dumps(
    {
        "action": "opened",
        "repository": {"full_name": "octo/it"},
        "pull_request": {"number": 7, "head": {"sha": "fff000"}},
    }
).encode()


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def pg_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    from review_agent.config import get_settings

    get_settings.cache_clear()
    from review_agent import db

    db.init_schema()
    with db._connect() as conn:
        conn.execute("TRUNCATE webhook_events RESTART IDENTITY")

    from review_agent.main import app

    with TestClient(app) as test_client:
        yield test_client, db
    get_settings.cache_clear()


def post_event(client, *, delivery: str, signature: str):
    return client.post(
        "/webhooks/github",
        content=PAYLOAD,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )


def test_replayed_delivery_persists_exactly_one_row(pg_client):
    client, db = pg_client
    delivery = "e5f6a7b8-guid"

    first = post_event(client, delivery=delivery, signature=_sig(PAYLOAD))
    second = post_event(client, delivery=delivery, signature=_sig(PAYLOAD))
    third = post_event(client, delivery=delivery, signature=_sig(PAYLOAD))

    assert first.json()["status"] == "stored"
    assert second.json()["status"] == "duplicate"
    assert third.json()["status"] == "duplicate"

    with db._connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM webhook_events WHERE delivery_id = %s", (delivery,)
        ).fetchone()[0]
        payload_action = conn.execute(
            "SELECT payload->>'action' FROM webhook_events WHERE delivery_id = %s", (delivery,)
        ).fetchone()[0]
    assert count == 1
    assert payload_action == "opened"


def test_bad_signature_rejected_and_nothing_persisted(pg_client):
    client, db = pg_client

    response = post_event(
        client, delivery="bad-sig-guid", signature=_sig(PAYLOAD, "attacker-secret")
    )

    assert response.status_code == 401
    with db._connect() as conn:
        count = conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0]
    assert count == 0
