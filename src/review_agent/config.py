"""Environment-driven configuration. No secrets are hardcoded or logged."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Secrets default to empty so the service can boot (e.g. /health) without them;
    # the webhook endpoint refuses to operate until github_webhook_secret is set.
    github_webhook_secret: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")

    # GitHub App credentials (Phase 2). The PEM may be provided as a single
    # line with \n escapes; it is normalized before use.
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")

    # Must be an adaptive-thinking-capable model (Claude 4.6 family or newer).
    llm_model: str = "claude-opus-4-8"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/review_agent"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
