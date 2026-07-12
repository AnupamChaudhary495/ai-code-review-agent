import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from review_agent import db
from review_agent.config import get_settings
from review_agent.main import app

TEST_SECRET = "test-webhook-secret"


class FakeStore:
    """In-memory stand-in for db.py's webhook-event persistence.

    The real SQL implementation is exercised by tests/test_ingest_integration.py
    against a live PostgreSQL.
    """

    def __init__(self) -> None:
        self.webhook_events: dict[str, dict] = {}

    def init_schema(self) -> None:
        pass

    def record_webhook_event(self, delivery_id, event, action, payload) -> bool:
        if delivery_id in self.webhook_events:
            return False
        self.webhook_events[delivery_id] = {"event": event, "action": action, "payload": payload}
        return True


@pytest.fixture
def fake_store(monkeypatch) -> FakeStore:
    store = FakeStore()
    monkeypatch.setattr(db, "init_schema", store.init_schema)
    monkeypatch.setattr(db, "record_webhook_event", store.record_webhook_event)
    return store


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(settings_env, fake_store):
    with TestClient(app) as test_client:  # lifespan runs; init_schema is the fake
        yield test_client


@pytest.fixture
def sign():
    def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    return _sign


@pytest.fixture
def pr_event():
    def _make(action="opened", repo="octo/demo", number=42, sha="abc123"):
        return {
            "action": action,
            "repository": {"full_name": repo},
            "pull_request": {"number": number, "head": {"sha": sha}},
        }

    return _make
