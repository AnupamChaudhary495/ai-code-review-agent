import json

import httpx

from review_agent import github_client, reviewer
from review_agent.github_client import FileDiff, NoReviewableFileError

DIFF = FileDiff(filename="app.py", patch="@@ -1 +1 @@\n-x = 1\n+x = 2", status="modified")


def post_webhook(client, sign, payload, *, event="pull_request", secret=None, delivery="dlv-1"):
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": sign(body, secret) if secret else sign(body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    return client.post("/webhook/github", content=body, headers=headers)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("X-Correlation-ID")


def test_rejects_bad_signature(client, sign, pr_event):
    response = post_webhook(client, sign, pr_event(), secret="wrong-secret")
    assert response.status_code == 401


def test_rejects_missing_signature(client, pr_event):
    body = json.dumps(pr_event()).encode()
    response = client.post(
        "/webhook/github", content=body, headers={"X-GitHub-Event": "pull_request"}
    )
    assert response.status_code == 401


def test_ignores_non_pr_events(client, sign):
    response = post_webhook(client, sign, {"zen": "Design for failure."}, event="ping")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_ignores_unhandled_actions(client, sign, pr_event):
    response = post_webhook(client, sign, pr_event(action="closed"))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_malformed_payload_is_rejected(client, sign):
    response = post_webhook(client, sign, {"action": "opened", "repository": {}})
    assert response.status_code == 400


def test_reviews_pr_end_to_end(client, sign, pr_event, fake_store, monkeypatch):
    monkeypatch.setattr(github_client, "get_first_file_diff", lambda repo, pr: DIFF)
    monkeypatch.setattr(reviewer, "review_file_diff", lambda name, patch: "Looks solid. LGTM.")

    response = post_webhook(client, sign, pr_event())
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["file"] == "app.py"
    assert data["review"] == "Looks solid. LGTM."

    row = fake_store.rows[("octo/demo", 42, "abc123")]
    assert row["status"] == "completed"


def test_duplicate_delivery_does_not_rereview(client, sign, pr_event, fake_store, monkeypatch):
    calls = []

    def fake_review(name, patch):
        calls.append(name)
        return "One-time review."

    monkeypatch.setattr(github_client, "get_first_file_diff", lambda repo, pr: DIFF)
    monkeypatch.setattr(reviewer, "review_file_diff", fake_review)

    first = post_webhook(client, sign, pr_event(), delivery="dlv-1")
    second = post_webhook(client, sign, pr_event(), delivery="dlv-2")  # GitHub redelivery

    assert first.json()["status"] == "completed"
    assert second.json()["status"] == "duplicate"
    assert second.json()["review"] == "One-time review."
    assert calls == ["app.py"]  # the LLM ran exactly once


def test_failed_review_is_retryable(client, sign, pr_event, fake_store, monkeypatch):
    attempts = []

    def flaky_review(name, patch):
        attempts.append(name)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused")
        return "Second attempt succeeded."

    monkeypatch.setattr(github_client, "get_first_file_diff", lambda repo, pr: DIFF)
    monkeypatch.setattr(reviewer, "review_file_diff", flaky_review)

    first = post_webhook(client, sign, pr_event(), delivery="dlv-1")
    assert first.status_code == 500
    assert fake_store.rows[("octo/demo", 42, "abc123")]["status"] == "failed"

    second = post_webhook(client, sign, pr_event(), delivery="dlv-2")
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert len(attempts) == 2


def test_error_responses_leak_no_internals(client, sign, pr_event, fake_store, monkeypatch):
    def boom(repo, pr):
        raise httpx.HTTPStatusError(
            "secret-internal-detail",
            request=httpx.Request("GET", "https://api.github.com"),
            response=httpx.Response(500),
        )

    monkeypatch.setattr(github_client, "get_first_file_diff", boom)

    response = post_webhook(client, sign, pr_event())
    assert response.status_code == 500
    assert "secret-internal-detail" not in response.text
    assert response.json() == {"status": "error", "detail": "review failed; see server logs"}


def test_pr_without_reviewable_file_is_skipped(client, sign, pr_event, fake_store, monkeypatch):
    def no_file(repo, pr):
        raise NoReviewableFileError("binary only")

    monkeypatch.setattr(github_client, "get_first_file_diff", no_file)

    response = post_webhook(client, sign, pr_event())
    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
    assert fake_store.rows[("octo/demo", 42, "abc123")]["status"] == "failed"
