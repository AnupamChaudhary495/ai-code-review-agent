"""GitHub App auth tests: JWT signing and token exchange, no real key, no network."""

import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from review_agent.config import get_settings
from review_agent.github import client
from review_agent.github.auth import (
    InstallationToken,
    InstallationTokenProvider,
    MissingAppCredentialsError,
    make_app_jwt,
)


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def app_creds(monkeypatch, rsa_keypair):
    """Configure test App credentials, with the PEM as a \\n-escaped single line."""
    private_pem, public_pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", private_pem.replace("\n", "\\n"))
    get_settings.cache_clear()
    yield public_pem
    get_settings.cache_clear()


def test_app_jwt_is_valid_rs256(app_creds):
    token = make_app_jwt()
    claims = jwt.decode(token, app_creds, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] == 600  # 9 min TTL + 60s backdated iat
    assert claims["iat"] <= time.time()


def test_missing_credentials_raise_without_leaking_key_material(monkeypatch, rsa_keypair):
    private_pem, _ = rsa_keypair
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(MissingAppCredentialsError) as exc_info:
            make_app_jwt()
        assert "BEGIN" not in str(exc_info.value)
        assert private_pem not in str(exc_info.value)
    finally:
        get_settings.cache_clear()


def test_fetch_installation_token_exchanges_jwt(app_creds):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(
            201,
            json={
                "token": "ghs_testtoken",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            },
        )

    app_jwt = make_app_jwt()
    result = client.fetch_installation_token(app_jwt, 987, transport=httpx.MockTransport(handler))

    assert result.token == "ghs_testtoken"
    assert seen["authorization"] == f"Bearer {app_jwt}"
    assert seen["url"].endswith("/app/installations/987/access_tokens")
    assert result.expires_at > time.time() + 3000


def test_fetch_repo_installation_uses_app_jwt(app_creds):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        assert str(request.url).endswith("/repos/octo/demo/installation")
        return httpx.Response(200, json={"id": 5551212})

    installation_id = client.fetch_repo_installation(
        make_app_jwt(), "octo/demo", transport=httpx.MockTransport(handler)
    )
    assert installation_id == 5551212


def test_provider_caches_token_until_near_expiry(app_creds):
    calls: list[int] = []

    def fake_fetch(app_jwt: str, installation_id: int) -> InstallationToken:
        calls.append(installation_id)
        return InstallationToken(f"tok-{len(calls)}", time.time() + 3600)

    provider = InstallationTokenProvider(fake_fetch)
    assert provider.token_for(1) == "tok-1"
    assert provider.token_for(1) == "tok-1"  # served from cache
    assert calls == [1]

    assert provider.token_for(2) == "tok-2"  # distinct installation, distinct token
    assert calls == [1, 2]


def test_provider_refreshes_expiring_token(app_creds):
    calls: list[int] = []

    def fake_fetch(app_jwt: str, installation_id: int) -> InstallationToken:
        calls.append(installation_id)
        # Expires inside the refresh margin, so every call must re-fetch.
        return InstallationToken(f"tok-{len(calls)}", time.time() + 30)

    provider = InstallationTokenProvider(fake_fetch)
    assert provider.token_for(1) == "tok-1"
    assert provider.token_for(1) == "tok-2"
    assert calls == [1, 1]
