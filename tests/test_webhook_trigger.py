"""Webhook -> review trigger: what fires a review, what provably does not.

The scope rule this phase committed to is narrow and worth pinning from both
sides: only `pull_request` with action opened/synchronize/reopened starts a
review. Everything else is still stored — Phase 2's behavior, unchanged — and
starts nothing. A regression here is either a silent gap (a real push that
never gets reviewed) or a runaway (an LLM bill for every label change).
"""

import json

import pytest

from review_agent import pipeline
from test_ingest import post_event

TRIGGERING = ["opened", "synchronize", "reopened"]
NON_TRIGGERING = ["closed", "labeled", "unlabeled", "assigned", "edited", "review_requested"]


# ---------------------------------------------------------------------------
# The filter, as pure functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", TRIGGERING)
def test_pull_request_actions_that_trigger(action):
    assert pipeline.should_trigger("pull_request", action) is True


@pytest.mark.parametrize("action", NON_TRIGGERING)
def test_pull_request_actions_that_do_not_trigger(action):
    assert pipeline.should_trigger("pull_request", action) is False


@pytest.mark.parametrize(
    "event", ["ping", "push", "issues", "issue_comment", "pull_request_review", "installation"]
)
def test_other_event_types_never_trigger(event):
    # Even carrying an action a PR would trigger on.
    assert pipeline.should_trigger(event, "opened") is False
    assert pipeline.should_trigger(event, None) is False


def test_extract_trigger_reads_identity_and_installation():
    payload = {
        "repository": {"full_name": "octo/demo"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "installation": {"id": 99},
    }
    assert pipeline.extract_trigger(payload) == ("octo/demo", 42, "abc123", 99)


def test_extract_trigger_tolerates_a_missing_installation_block():
    payload = {
        "repository": {"full_name": "octo/demo"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
    }
    # None, not an error — run_review resolves it from the repo instead.
    assert pipeline.extract_trigger(payload)[3] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"repository": {"full_name": "octo/demo"}},
        {"repository": {}, "pull_request": {"number": 1, "head": {"sha": "x"}}},
        {"repository": {"full_name": "o/d"}, "pull_request": {"head": {"sha": "x"}}},
        {"repository": {"full_name": "o/d"}, "pull_request": {"number": 1}},
        {"repository": {"full_name": "o/d"}, "pull_request": {"number": "not-a-number"}},
    ],
)
def test_extract_trigger_rejects_unreadable_payloads(payload):
    with pytest.raises(pipeline.TriggerPayloadError):
        pipeline.extract_trigger(payload)


# ---------------------------------------------------------------------------
# End to end through the HTTP endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", TRIGGERING)
def test_triggering_event_returns_202_and_schedules_exactly_one_job(
    client, sign, pr_event, fake_store, review_jobs, action
):
    response = post_event(client, sign, pr_event(action=action), delivery=f"guid-{action}")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "review_scheduled"
    assert body["review_id"] == 1

    # The event is still stored — triggering did not replace ingestion.
    assert fake_store.webhook_events[f"guid-{action}"]["action"] == action
    # Exactly one job, carrying the identity pulled off the payload.
    assert review_jobs == [
        {"review_id": 1, "repo": "octo/demo", "pr_number": 42, "installation_id": 99}
    ]
    # And a row exists to track it.
    assert fake_store.reviews[1]["status"] == "pending"
    assert fake_store.reviews[1]["head_sha"] == "abc123"


@pytest.mark.parametrize("action", NON_TRIGGERING)
def test_non_triggering_action_stores_and_schedules_nothing(
    client, sign, pr_event, fake_store, review_jobs, action
):
    response = post_event(client, sign, pr_event(action=action), delivery=f"guid-{action}")

    assert response.status_code == 200
    assert response.json() == {"status": "stored", "delivery_id": f"guid-{action}"}
    assert fake_store.webhook_events[f"guid-{action}"]["action"] == action
    assert review_jobs == []
    assert fake_store.reviews == {}


def test_ping_event_stores_and_schedules_nothing(client, sign, fake_store, review_jobs):
    response = post_event(
        client, sign, {"zen": "Keep it simple."}, delivery="guid-ping", event="ping"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "stored"
    assert review_jobs == []


def test_duplicate_delivery_does_not_schedule_a_second_review(
    client, sign, pr_event, fake_store, review_jobs
):
    """GitHub redelivering an event must not cost a second review."""
    first = post_event(client, sign, pr_event(), delivery="guid-same")
    second = post_event(client, sign, pr_event(), delivery="guid-same")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(review_jobs) == 1
    assert len(fake_store.reviews) == 1


def test_second_delivery_for_the_same_head_sha_is_blocked_by_the_guard(
    client, sign, pr_event, fake_store, review_jobs
):
    """Different delivery ids, same commit — one review, not two.

    This is the `synchronize`-fires-twice case. The guard is a stopgap
    (ADR-0004), but it must at least hold for sequential deliveries.
    """
    first = post_event(client, sign, pr_event(action="opened"), delivery="guid-a")
    second = post_event(client, sign, pr_event(action="synchronize"), delivery="guid-b")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "review_exists"
    assert second.json()["head_sha"] == "abc123"

    # Both events stored; only one review.
    assert len(fake_store.webhook_events) == 2
    assert len(fake_store.reviews) == 1
    assert len(review_jobs) == 1


def test_a_new_commit_on_the_same_pr_does_get_its_own_review(
    client, sign, pr_event, fake_store, review_jobs
):
    """The guard keys on head SHA, so a genuine push is not swallowed by it."""
    post_event(client, sign, pr_event(sha="sha-one"), delivery="guid-1")
    response = post_event(
        client, sign, pr_event(action="synchronize", sha="sha-two"), delivery="guid-2"
    )

    assert response.status_code == 202
    assert len(review_jobs) == 2
    assert {r["head_sha"] for r in fake_store.reviews.values()} == {"sha-one", "sha-two"}


def test_a_failed_review_does_not_block_a_later_delivery(
    client, sign, pr_event, fake_store, review_jobs
):
    post_event(client, sign, pr_event(), delivery="guid-1")
    fake_store.fail_review(1, "APITimeoutError: boom")

    response = post_event(client, sign, pr_event(action="synchronize"), delivery="guid-2")

    assert response.status_code == 202
    assert len(review_jobs) == 2


def test_malformed_pull_request_payload_stores_but_does_not_schedule(
    client, sign, fake_store, review_jobs
):
    """A payload we cannot read is not GitHub's to retry — 200, and no job."""
    broken = {"action": "opened", "repository": {"full_name": "octo/demo"}}
    response = post_event(client, sign, broken, delivery="guid-broken")

    assert response.status_code == 200
    assert response.json()["status"] == "stored"
    assert fake_store.webhook_events["guid-broken"]["action"] == "opened"
    assert review_jobs == []
    assert fake_store.reviews == {}


def test_bad_signature_schedules_nothing(client, sign, pr_event, fake_store, review_jobs):
    body = json.dumps(pr_event()).encode()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, "attacker-secret"),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "guid-evil",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401
    assert review_jobs == []
    assert fake_store.reviews == {}
