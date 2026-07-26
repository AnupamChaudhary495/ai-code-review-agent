import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from review_agent import db, pipeline
from review_agent.config import Settings, get_settings
from review_agent.main import app

TEST_SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """Keep tests independent of a developer's local .env.

    Once the GitHub App is configured, .env holds real credentials; without
    this, tests that assert credentials are *absent* would read them from .env
    and fail locally while passing in CI (which has no .env). Disable env-file
    loading for the whole test session; env vars set via monkeypatch still win.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeStore:
    """In-memory stand-in for db.py's persistence.

    Mirrors the real semantics that tests depend on — delivery-id uniqueness,
    and `claim_review`'s blocking-status guard — so behavior tests are testing
    the rules rather than a mock that always says yes. The actual SQL is
    exercised by tests/test_ingest_integration.py and
    tests/test_reviews_integration.py against a live PostgreSQL.
    """

    def __init__(self) -> None:
        self.webhook_events: dict[str, dict] = {}
        self.reviews: dict[int, dict] = {}
        self._next_id = 1

    def init_schema(self) -> None:
        pass

    def record_webhook_event(self, delivery_id, event, action, payload) -> bool:
        if delivery_id in self.webhook_events:
            return False
        self.webhook_events[delivery_id] = {"event": event, "action": action, "payload": payload}
        return True

    # --- reviews -------------------------------------------------------
    def claim_review(self, repo, pr_number, head_sha, force=False) -> int | None:
        if not force and any(
            r["repo"] == repo and r["head_sha"] == head_sha and r["status"] in db.BLOCKING_STATUSES
            for r in self.reviews.values()
        ):
            return None
        review_id = self._next_id
        self._next_id += 1
        self.reviews[review_id] = {
            "id": review_id,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "status": "pending",
            "report": None,
            "error": None,
            "review_url": None,
        }
        return review_id

    def set_review_status(self, review_id, status) -> None:
        self.reviews[review_id]["status"] = status

    def store_report(self, review_id, report) -> None:
        self.reviews[review_id]["report"] = report

    def complete_review(self, review_id, review_url) -> None:
        self.reviews[review_id] |= {"status": "completed", "review_url": review_url, "error": None}

    def fail_review(self, review_id, error) -> None:
        self.reviews[review_id] |= {"status": "failed", "error": error}

    def get_review(self, review_id):
        return self.reviews.get(review_id)

    def list_reviews(self, repo=None, pr_number=None, status=None, limit=50, offset=0):
        rows = [
            {k: v for k, v in r.items() if k != "report"} | {"has_report": r["report"] is not None}
            for r in self.reviews.values()
            if (repo is None or r["repo"] == repo)
            and (pr_number is None or r["pr_number"] == pr_number)
            and (status is None or r["status"] == status)
        ]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[offset : offset + limit]


_FAKE_METHODS = (
    "init_schema",
    "record_webhook_event",
    "claim_review",
    "set_review_status",
    "store_report",
    "complete_review",
    "fail_review",
    "get_review",
    "list_reviews",
)


@pytest.fixture
def fake_store(monkeypatch) -> FakeStore:
    store = FakeStore()
    for name in _FAKE_METHODS:
        monkeypatch.setattr(db, name, getattr(store, name))
    return store


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def review_jobs(monkeypatch) -> list[dict]:
    """Capture scheduled review jobs instead of running them.

    `client` depends on this, so no HTTP test can accidentally kick off a real
    review — TestClient runs background tasks for real once the response is
    returned, and `run_review` would go out to GitHub and Anthropic.
    tests/test_pipeline.py calls the real `run_review` directly instead.
    """
    calls: list[dict] = []

    def fake_run_review(review_id, repo, pr_number, installation_id=None):
        calls.append(
            {
                "review_id": review_id,
                "repo": repo,
                "pr_number": pr_number,
                "installation_id": installation_id,
            }
        )

    monkeypatch.setattr(pipeline, "run_review", fake_run_review)
    return calls


@pytest.fixture
def client(settings_env, fake_store, review_jobs):
    with TestClient(app) as test_client:  # lifespan runs; init_schema is the fake
        yield test_client


@pytest.fixture
def sign():
    def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    return _sign


@pytest.fixture
def pr_event():
    def _make(action="opened", repo="octo/demo", number=42, sha="abc123", installation_id=99):
        payload = {
            "action": action,
            "repository": {"full_name": repo},
            "pull_request": {"number": number, "head": {"sha": sha}},
        }
        if installation_id is not None:
            payload["installation"] = {"id": installation_id}
        return payload

    return _make
