"""Application settings, read from environment variables prefixed with LOBBYD_."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only ever valid outside production; the validator below enforces that.
_DEV_JWT_SECRET = "dev-only-secret-change-me-dev-only-secret"  # noqa: S105


_DEV_PAYSIM_SECRET = "dev-only-paysim-webhook-secret"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOBBYD_", env_file=".env", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://lobbyd:lobbyd@localhost:5432/lobbyd"
    redis_url: str = "redis://localhost:6379/0"

    # Pool sizing is revisited with measurements in the performance phase.
    db_pool_size: int = 10
    db_max_overflow: int = 10

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60

    # How long a hold keeps inventory off sale while the buyer checks out.
    hold_ttl_seconds: int = 10 * 60

    # Once checkout starts, the hold is extended so it cannot lapse mid-payment.
    payment_window_seconds: int = 5 * 60

    paysim_url: str = "http://localhost:8001"
    paysim_webhook_secret: SecretStr = SecretStr(_DEV_PAYSIM_SECRET)
    # Reject webhooks whose signed timestamp is older than this (replay protection).
    webhook_tolerance_seconds: int = 5 * 60

    # A failed event is retried after this long, this many times, then set aside.
    event_retry_after_ms: int = 10_000
    event_max_attempts: int = 5

    log_json: bool = False

    @model_validator(mode="after")
    def _require_real_secret_in_production(self) -> Self:
        if self.environment == "production":
            secret = self.jwt_secret.get_secret_value()
            if secret == _DEV_JWT_SECRET or len(secret) < 32:
                raise ValueError(
                    "LOBBYD_JWT_SECRET must be set to a random value of at least "
                    "32 characters in production"
                )
            if self.paysim_webhook_secret.get_secret_value() == _DEV_PAYSIM_SECRET:
                raise ValueError("LOBBYD_PAYSIM_WEBHOOK_SECRET must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
