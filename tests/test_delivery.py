"""Delivery tests: posting logic in isolation, no real GitHub calls."""

import httpx
import pytest

from helpers import load_fixture
from review_agent.diffing import parser
from review_agent.github import client, delivery
from review_agent.schemas.finding import Finding

INSTALLATION_TOKEN = "ghs_fixture_installation_token"
INSTALLATION_ID = 424242


@pytest.fixture
def stub_provider(monkeypatch):
    class StubProvider:
        def token_for(self, installation_id: int) -> str:
            assert installation_id == INSTALLATION_ID
            return INSTALLATION_TOKEN

    monkeypatch.setattr(client, "token_provider", StubProvider())


@pytest.fixture
def change():
    entry = next(f for f in load_fixture("pr_edge_files.json") if f["filename"] == "long_module.py")
    return parser.parse_file(entry)


def finding(line: int | None, severity: str = "high") -> Finding:
    return Finding(
        file="long_module.py",
        line=line,
        category="bug",
        severity=severity,
        message="Something is wrong here.",
        suggestion="Fix it like this.",
    )


def capture_transport(captured: list[dict], statuses: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {INSTALLATION_TOKEN}"
        assert request.url.path == "/repos/octo/demo/pulls/7/reviews"
        import json

        captured.append(json.loads(request.content))
        status = statuses.pop(0)
        body = {"html_url": "https://github.com/octo/demo/pull/7#pullrequestreview-1"}
        return httpx.Response(status, json=body if status < 400 else {"message": "unproc"})

    return httpx.MockTransport(handler)


def test_anchorable_findings_become_inline_comments(stub_provider, change):
    anchor_line = min(change.hunks[0].new_lines())
    captured: list[dict] = []
    url = delivery.post_review(
        "octo/demo",
        7,
        INSTALLATION_ID,
        change,
        [finding(anchor_line)],
        transport=capture_transport(captured, [200]),
    )

    (payload,) = captured
    assert payload["event"] == "COMMENT"
    (comment,) = payload["comments"]
    assert comment["path"] == "long_module.py"
    assert comment["line"] == anchor_line
    assert comment["side"] == "RIGHT"
    assert "Something is wrong" in comment["body"]
    assert "1 finding" in payload["body"]
    assert url.endswith("pullrequestreview-1")


def test_unanchorable_finding_goes_to_body(stub_provider, change):
    captured: list[dict] = []
    delivery.post_review(
        "octo/demo",
        7,
        INSTALLATION_ID,
        change,
        [finding(999999), finding(None)],
        transport=capture_transport(captured, [200]),
    )

    (payload,) = captured
    assert payload["comments"] == []
    assert payload["body"].count("Something is wrong") == 2


def test_422_falls_back_to_body_only_review(stub_provider, change):
    anchor_line = min(change.hunks[0].new_lines())
    captured: list[dict] = []
    delivery.post_review(
        "octo/demo",
        7,
        INSTALLATION_ID,
        change,
        [finding(anchor_line)],
        transport=capture_transport(captured, [422, 200]),
    )

    assert len(captured) == 2
    assert captured[0]["comments"]  # first attempt tried inline placement
    assert captured[1]["comments"] == []  # fallback folded everything into the body
    assert "Something is wrong" in captured[1]["body"]


def test_empty_findings_posts_clean_bill(stub_provider, change):
    captured: list[dict] = []
    delivery.post_review(
        "octo/demo",
        7,
        INSTALLATION_ID,
        change,
        [],
        transport=capture_transport(captured, [200]),
    )

    (payload,) = captured
    assert payload["comments"] == []
    assert "no issues found" in payload["body"]
