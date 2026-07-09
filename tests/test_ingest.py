"""Ingestion endpoint tests (signature, dedup, persistence) against the fake store."""

import json


def post_event(client, sign, payload, *, delivery, event="pull_request", secret=None):
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": sign(body, secret) if secret else sign(body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


def test_stores_new_delivery(client, sign, pr_event, fake_store):
    response = post_event(client, sign, pr_event(), delivery="guid-1")
    assert response.status_code == 200
    assert response.json() == {"status": "stored", "delivery_id": "guid-1"}
    stored = fake_store.webhook_events["guid-1"]
    assert stored["event"] == "pull_request"
    assert stored["action"] == "opened"


def test_replayed_delivery_is_noop(client, sign, pr_event, fake_store):
    first = post_event(client, sign, pr_event(), delivery="guid-dup")
    second = post_event(client, sign, pr_event(), delivery="guid-dup")
    assert first.json()["status"] == "stored"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(fake_store.webhook_events) == 1


def test_bad_signature_rejected(client, sign, pr_event, fake_store):
    response = post_event(client, sign, pr_event(), delivery="guid-bad", secret="wrong-secret")
    assert response.status_code == 401
    assert fake_store.webhook_events == {}


def test_missing_delivery_id_rejected(client, sign, pr_event):
    body = json.dumps(pr_event()).encode()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 400


def test_ping_event_is_stored_too(client, sign, fake_store):
    response = post_event(
        client, sign, {"zen": "Keep it simple."}, delivery="guid-ping", event="ping"
    )
    assert response.status_code == 200
    assert fake_store.webhook_events["guid-ping"]["event"] == "ping"
    assert fake_store.webhook_events["guid-ping"]["action"] is None
