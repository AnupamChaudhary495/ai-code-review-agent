import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from review_agent import db
from review_agent.config import get_settings
from review_agent.db import Claim
from review_agent.main import app

TEST_SECRET = "test-webhook-secret"


class FakeStore:
    """In-memory stand-in for db.py with the same claim semantics.

    The real SQL implementation is exercised by tests/test_store_integration.py
    against a live PostgreSQL.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, str], dict] = {}
        self._next_id = 1

    def init_schema(self) -> None:
        pass

    def claim_review(self, repo, pr_number, head_sha, delivery_id) -> Claim:
        key = (repo, pr_number, head_sha)
        row = self.rows.get(key)
        if row is None:
            row = {"id": self._next_id, "status": "in_progress", "review_body": None}
            self._next_id += 1
            self.rows[key] = row
            return Claim("claimed", row["id"])
        if row["status"] == "failed":
            row["status"] = "in_progress"
            return Claim("claimed", row["id"])
        if row["status"] == "completed":
            return Claim("duplicate_completed", row["id"], row["review_body"])
        return Claim("duplicate_in_progress", row["id"])

    def complete_review(self, review_id, file_path, review_body) -> None:
        row = self._by_id(review_id)
        row.update(status="completed", file_path=file_path, review_body=review_body)

    def fail_review(self, review_id, error) -> None:
        row = self._by_id(review_id)
        row.update(status="failed", error=error)

    def _by_id(self, review_id) -> dict:
        for row in self.rows.values():
            if row["id"] == review_id:
                return row
        raise KeyError(review_id)


@pytest.fixture
def fake_store(monkeypatch) -> FakeStore:
    store = FakeStore()
    monkeypatch.setattr(db, "init_schema", store.init_schema)
    monkeypatch.setattr(db, "claim_review", store.claim_review)
    monkeypatch.setattr(db, "complete_review", store.complete_review)
    monkeypatch.setattr(db, "fail_review", store.fail_review)
    return store


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
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
