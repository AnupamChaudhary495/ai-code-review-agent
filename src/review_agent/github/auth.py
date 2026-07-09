"""GitHub App authentication: app JWTs and cached installation tokens.

App credentials come from env vars only (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY).
The private key is never logged and never appears in error messages.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt

from ..config import get_settings

logger = logging.getLogger(__name__)

# GitHub caps app JWTs at 10 minutes; stay under it and backdate iat to
# absorb clock skew between this host and GitHub.
_JWT_TTL_SECONDS = 540
_JWT_CLOCK_SKEW_SECONDS = 60

# Refresh cached installation tokens this long before they expire.
_REFRESH_MARGIN_SECONDS = 120


class MissingAppCredentialsError(Exception):
    """GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY are not configured."""


def _normalize_pem(value: str) -> str:
    # Allow the PEM to be provided as a single env-var line with \n escapes.
    return value.replace("\\n", "\n")


def make_app_jwt(now: float | None = None) -> str:
    """Create a short-lived RS256 JWT identifying the GitHub App."""
    settings = get_settings()
    app_id = settings.github_app_id
    private_key = _normalize_pem(settings.github_app_private_key.get_secret_value())
    if not app_id or not private_key:
        raise MissingAppCredentialsError("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY must be set")

    issued_at = int(now if now is not None else time.time())
    payload = {
        "iat": issued_at - _JWT_CLOCK_SKEW_SECONDS,
        "exp": issued_at + _JWT_TTL_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


@dataclass
class InstallationToken:
    token: str
    expires_at: float  # unix epoch


class InstallationTokenProvider:
    """Caches installation access tokens and refreshes them before expiry."""

    def __init__(self, fetch: Callable[[str, int], InstallationToken]):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._cache: dict[int, InstallationToken] = {}

    def token_for(self, installation_id: int) -> str:
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached and cached.expires_at - time.time() > _REFRESH_MARGIN_SECONDS:
                return cached.token
            fresh = self._fetch(make_app_jwt(), installation_id)
            self._cache[installation_id] = fresh
            logger.info(
                "installation token refreshed",
                extra={"installation_id": installation_id, "expires_at": fresh.expires_at},
            )
            return fresh.token
