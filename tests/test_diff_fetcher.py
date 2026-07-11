"""Diff fetcher tests: recorded fixtures served through a mock transport.

Every request must carry the installation token minted by the (stubbed)
token provider — proving the fetcher authenticates as the GitHub App,
never with a personal access token.
"""

import httpx
import pytest

from helpers import load_fixture
from review_agent.diffing.models import ChangeType, SizeTier
from review_agent.github import client, diff_fetcher

INSTALLATION_TOKEN = "ghs_fixture_installation_token"
INSTALLATION_ID = 424242


@pytest.fixture
def provider_calls(monkeypatch):
    calls: list[int] = []

    class StubProvider:
        def token_for(self, installation_id: int) -> str:
            calls.append(installation_id)
            return INSTALLATION_TOKEN

    monkeypatch.setattr(client, "token_provider", StubProvider())
    return calls


def fixture_transport(
    meta: dict, files: list[dict], seen: list[httpx.Request]
) -> httpx.MockTransport:
    """Serve recorded fixtures the way GitHub would, asserting App auth on every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {INSTALLATION_TOKEN}"
        assert request.headers["Accept"] == "application/vnd.github+json"
        seen.append(request)
        if request.url.path.endswith("/files"):
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "100"))
            start = (page - 1) * per_page
            return httpx.Response(200, json=files[start : start + per_page])
        return httpx.Response(200, json=meta)

    return httpx.MockTransport(handler)


def fetch(name: str, provider_calls, repo="octo/fixture", pr=1):
    meta = load_fixture(f"{name}_meta.json")
    files = load_fixture(f"{name}_files.json")
    seen: list[httpx.Request] = []
    diff = diff_fetcher.fetch_pr_diff(
        repo, pr, INSTALLATION_ID, transport=fixture_transport(meta, files, seen)
    )
    assert provider_calls == [INSTALLATION_ID]  # token came from the provider
    return diff, seen


def test_small_pr(provider_calls):
    diff, seen = fetch("pr_small", provider_calls)
    assert diff.total_changed_files == 1
    assert not diff.truncated
    (change,) = diff.files
    assert change.path == "greet.py"
    assert change.language == "python"
    assert change.size_tier == SizeTier.SMALL
    assert len(change.hunks) == 1


def test_edge_pr_statuses(provider_calls):
    diff, _ = fetch("pr_edge", provider_calls)
    by_path = {f.path: f for f in diff.files}
    assert by_path["data.bin"].is_binary
    assert by_path["src/renamed.py"].change_type == ChangeType.RENAMED
    assert by_path["src/renamed.py"].old_path == "to_rename.py"
    assert not by_path["src/renamed.py"].is_binary
    assert by_path["to_delete.txt"].change_type == ChangeType.REMOVED
    assert len(by_path["long_module.py"].hunks) == 2
    assert not diff.truncated


def test_binary_only_pr(provider_calls):
    diff, _ = fetch("pr_binary", provider_calls)
    (change,) = diff.files
    assert change.is_binary
    assert change.hunks == []
    assert not diff.truncated


def test_truncated_pr_is_flagged_not_silently_dropped(provider_calls):
    diff, seen = fetch("pr_truncated", provider_calls)
    assert diff.total_changed_files == 3010  # what GitHub says the PR contains
    assert len(diff.files) == 3000  # what GitHub actually serves (its hard cap)
    assert diff.truncated is True
    # pagination really walked every page: 1 meta request + 31 file pages
    file_requests = [r for r in seen if r.url.path.endswith("/files")]
    assert len(file_requests) == 31
