"""Application settings, read from environment variables prefixed with LOBBYD_."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only ever valid outside production; the validator below enforces that.
_DEV_JWT_SECRET = "dev-only-secret-change-me-dev-only-secret"  # noqa: S105


_DEV_PAYSIM_SECRET = "dev-only-paysim-webhook-secret"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOBBYD_", env_file=".env", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+asyncpg://lobbyd:lobbyd@localhost:5432/lobbyd"
    redis_url: str = "redis://localhost:6379/0"

    redis_max_connections: int = 50

    # Pool sizing is revisited with measurements in the performance phase.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # How long a request waits for a free connection before giving up with a
    # 503. Short, so an overloaded server sheds load instead of queueing it.
    db_pool_timeout_seconds: float = 5

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    # Ed25519 private key that signs ticket QR codes: 32 random bytes, base64url.
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Outside production a key is derived from jwt_secret, so tickets survive restarts.
    ticket_signing_key: SecretStr | None = None
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60

    # How long a hold keeps inventory off sale while the buyer checks out.
    hold_ttl_seconds: int = 10 * 60

    # How long a captain has to take up a slot offered from the waitlist.
    waitlist_offer_seconds: int = 30 * 60

    # Once checkout starts, the hold is extended so it cannot lapse mid-payment.
    payment_window_seconds: int = 5 * 60

    paysim_url: str = "http://localhost:8001"
    paysim_webhook_secret: SecretStr = SecretStr(_DEV_PAYSIM_SECRET)
    # Reject webhooks whose signed timestamp is older than this (replay protection).
    webhook_tolerance_seconds: int = 5 * 60

    # A failed event is retried after this long, this many times, then set aside.
    event_retry_after_ms: int = 10_000
    event_max_attempts: int = 5

    # How many queued buyers are let through to booking each second, and how
    # long their turn lasts. Set from the benchmark (docs/benchmarks.md).
    waiting_room_admit_per_second: float = 20
    admission_ttl_seconds: int = 10 * 60

    # How seats are claimed. "lock_then_claim" is the production choice; the
    # others exist to be measured against it (docs/benchmarks.md).
    claim_strategy: Literal["lock_then_claim", "claim_only", "skip_locked", "redis_lock"] = (
        "lock_then_claim"
    )
    # Off only for benchmarking what the cache is worth.
    cache_enabled: bool = True

    # How long each API process may remember a tournament's sale window.
    catalog_memo_seconds: float = 2
    # How long each API process may go without re-checking that a signed-in
    # user still exists and what their role is.
    auth_memo_seconds: float = 5

    rate_limit_enabled: bool = True
    # Set only when every request arrives through a proxy we run ourselves.
    trust_forwarded_for: bool = False

    log_json: bool = False

    # Where to send traces (an OTLP/HTTP collector such as Jaeger, e.g.
    # http://jaeger:4318). Empty: tracing is off and costs nothing.
    otlp_endpoint: str = ""
    # The worker has no HTTP server, so it serves its metrics here. 0 turns it off.
    worker_metrics_port: int = 9100

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, url: str) -> str:
        """Accept a connection string as hosting providers hand it out.

        They give `postgres://...?sslmode=require`. This app talks to Postgres
        through asyncpg, which wants its own scheme and spells TLS `ssl`.
        """
        scheme, separator, rest = url.partition("://")
        if scheme in ("postgres", "postgresql"):
            url = f"postgresql+asyncpg{separator}{rest}"
        base, question, query = url.partition("?")
        if not question:
            return url
        kept = []
        for part in query.split("&"):
            name, _, value = part.partition("=")
            if name == "sslmode":
                kept.append(f"ssl={value}")
            elif name != "channel_binding":  # libpq only
                kept.append(part)
        return f"{base}?{'&'.join(kept)}" if kept else base

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
            if self.ticket_signing_key is None:
                # A key derived from a secret that also signs logins would mean
                # one leak forges both. Production gets its own.
                raise ValueError("LOBBYD_TICKET_SIGNING_KEY must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
