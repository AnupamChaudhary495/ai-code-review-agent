"""Review API: manual trigger (the re-run path), status/result, history."""

import pytest


def trigger(client, **body):
    payload = {"repo": "octo/demo", "pr_number": 7, "head_sha": "abc123"} | body
    return client.post("/reviews", json=payload)


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def test_manual_trigger_schedules_a_review_and_returns_202(client, fake_store, review_jobs):
    response = trigger(client, installation_id=99)

    assert response.status_code == 202
    assert response.json() == {
        "status": "review_scheduled",
        "review_id": 1,
        "review_status": "pending",
    }
    assert review_jobs == [
        {"review_id": 1, "repo": "octo/demo", "pr_number": 7, "installation_id": 99}
    ]
    assert fake_store.reviews[1]["status"] == "pending"


def test_manual_trigger_uses_the_same_job_as_the_webhook(client, sign, pr_event, review_jobs):
    """One code path. A manual review is not a different kind of review."""
    from test_ingest import post_event

    post_event(client, sign, pr_event(sha="sha-hook"), delivery="guid-hook")
    trigger(client, head_sha="sha-manual", installation_id=99)

    assert len(review_jobs) == 2
    assert {j["repo"] for j in review_jobs} == {"octo/demo"}


def test_duplicate_trigger_is_refused_with_409_not_a_silent_no_op(client, review_jobs):
    first = trigger(client)
    second = trigger(client)

    assert first.status_code == 202
    assert second.status_code == 409
    assert "force=true" in second.json()["detail"]
    assert len(review_jobs) == 1


def test_force_re_runs_a_completed_review(client, fake_store, review_jobs):
    trigger(client)
    fake_store.complete_review(1, "https://github.com/octo/demo/pull/7#r1")

    blocked = trigger(client)
    forced = trigger(client, force=True)

    assert blocked.status_code == 409
    assert forced.status_code == 202
    assert forced.json()["review_id"] == 2
    assert len(review_jobs) == 2


def test_a_failed_review_can_be_re_run_without_force(client, fake_store, review_jobs):
    """`failed` is deliberately not a blocking status — retry is the normal case."""
    trigger(client)
    fake_store.fail_review(1, "APITimeoutError: boom")

    retried = trigger(client)

    assert retried.status_code == 202
    assert retried.json()["review_id"] == 2
    assert len(review_jobs) == 2


def test_trigger_without_head_sha_is_accepted(client, review_jobs):
    """head_sha is optional; callers that lack it lose the guard, not the review."""
    response = client.post("/reviews", json={"repo": "octo/demo", "pr_number": 7})
    assert response.status_code == 202
    assert len(review_jobs) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"pr_number": 7},  # no repo
        {"repo": "octo/demo"},  # no pr_number
        {"repo": "octo/demo", "pr_number": 0},  # pr_number must be > 0
        {"repo": "octo/demo", "pr_number": -1},
        {"repo": "octo/demo", "pr_number": "seven"},
    ],
)
def test_invalid_trigger_bodies_are_rejected(client, body, review_jobs):
    response = client.post("/reviews", json=body)
    assert response.status_code == 422
    assert review_jobs == []


# ---------------------------------------------------------------------------
# Fetch one
# ---------------------------------------------------------------------------


def test_get_review_reports_pending_before_the_job_runs(client):
    review_id = trigger(client).json()["review_id"]

    response = client.get(f"/reviews/{review_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["report"] is None
    assert body["repo"] == "octo/demo" and body["pr_number"] == 7


def test_get_review_returns_the_report_once_completed(client, fake_store):
    review_id = trigger(client).json()["review_id"]
    fake_store.store_report(review_id, {"schema_version": "1.0", "verdict": "blocking"})
    fake_store.complete_review(review_id, "https://github.com/octo/demo/pull/7#r1")

    body = client.get(f"/reviews/{review_id}").json()

    assert body["status"] == "completed"
    assert body["report"]["verdict"] == "blocking"
    assert body["review_url"].endswith("#r1")


def test_get_review_surfaces_the_error_on_a_failed_run(client, fake_store):
    review_id = trigger(client).json()["review_id"]
    fake_store.fail_review(review_id, "APITimeoutError: request timed out")

    body = client.get(f"/reviews/{review_id}").json()

    assert body["status"] == "failed"
    assert "APITimeoutError" in body["error"]


def test_unknown_review_is_404(client):
    assert client.get("/reviews/9999").status_code == 404


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@pytest.fixture
def history(client):
    trigger(client, repo="octo/demo", pr_number=1, head_sha="a1")
    trigger(client, repo="octo/demo", pr_number=2, head_sha="b2")
    trigger(client, repo="other/repo", pr_number=3, head_sha="c3")
    return client


def test_list_returns_every_review_newest_first(history):
    body = history.get("/reviews").json()

    assert body["count"] == 3
    assert [r["id"] for r in body["reviews"]] == [3, 2, 1]
    # The listing omits report bodies; `has_report` says whether one exists.
    assert all("report" not in r for r in body["reviews"])
    assert all(r["has_report"] is False for r in body["reviews"])


def test_list_filters_by_repo(history):
    body = history.get("/reviews", params={"repo": "octo/demo"}).json()

    assert body["count"] == 2
    assert {r["repo"] for r in body["reviews"]} == {"octo/demo"}


def test_list_filters_by_repo_and_pr_number(history):
    body = history.get("/reviews", params={"repo": "octo/demo", "pr_number": 2}).json()

    assert body["count"] == 1
    assert body["reviews"][0]["pr_number"] == 2


def test_list_filters_by_status(history, fake_store):
    fake_store.fail_review(1, "boom")

    failed = history.get("/reviews", params={"status": "failed"}).json()
    pending = history.get("/reviews", params={"status": "pending"}).json()

    assert failed["count"] == 1 and failed["reviews"][0]["id"] == 1
    assert pending["count"] == 2


def test_list_paginates(history):
    first = history.get("/reviews", params={"limit": 2}).json()
    second = history.get("/reviews", params={"limit": 2, "offset": 2}).json()

    assert [r["id"] for r in first["reviews"]] == [3, 2]
    assert [r["id"] for r in second["reviews"]] == [1]
    assert first["limit"] == 2 and second["offset"] == 2


def test_unknown_repo_lists_empty_rather_than_erroring(history):
    body = history.get("/reviews", params={"repo": "nobody/nothing"}).json()
    assert body["count"] == 0 and body["reviews"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"pr_number": 0}])
def test_invalid_list_parameters_are_rejected(client, params):
    assert client.get("/reviews", params=params).status_code == 422
