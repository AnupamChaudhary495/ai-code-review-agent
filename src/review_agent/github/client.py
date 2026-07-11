"""HTTP calls to GitHub's App API (installation token exchange)."""

import logging
from datetime import datetime

import httpx

from .auth import InstallationToken, InstallationTokenProvider

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


def fetch_installation_token(
    app_jwt: str, installation_id: int, transport: httpx.BaseTransport | None = None
) -> InstallationToken:
    """Exchange an app JWT for an installation access token.

    `transport` exists so tests can stub the HTTP exchange; production callers
    leave it unset.
    """
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.post(
            f"{API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        data = response.json()

    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
    return InstallationToken(token=data["token"], expires_at=expires_at)


def fetch_repo_installation(
    app_jwt: str, repo: str, transport: httpx.BaseTransport | None = None
) -> int:
    """Look up the App's installation ID for a repository ("owner/name")."""
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.get(
            f"{API_BASE}/repos/{repo}/installation",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return int(response.json()["id"])


# Process-wide provider: the entry point for authenticated API calls as the App.
token_provider = InstallationTokenProvider(fetch_installation_token)
