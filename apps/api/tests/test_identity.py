import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd.config import Settings
from lobbyd.identity.deps import require_role
from lobbyd.identity.models import RefreshToken, User

EMAIL = "ada@example.com"
PASSWORD = "correct horse battery"


async def signup(
    client: AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> dict[str, Any]:
    response = await client.post(
        "/auth/signup", json={"email": email, "display_name": "Ada", "password": password}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def login(
    client: AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> dict[str, Any]:
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --- signup ---------------------------------------------------------------


async def test_signup_creates_a_user_and_never_returns_the_password(client: AsyncClient) -> None:
    user = await signup(client)
    assert user["email"] == EMAIL
    assert user["role"] == "user"
    assert "password" not in user
    assert "password_hash" not in user


async def test_passwords_are_stored_as_argon2_hashes(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(client)
    async with sessionmaker() as session:
        stored = await session.scalar(select(User.password_hash))
    assert stored is not None
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


async def test_email_is_unique_ignoring_case(client: AsyncClient) -> None:
    await signup(client)
    response = await client.post(
        "/auth/signup",
        json={"email": "ADA@Example.com", "display_name": "Other", "password": PASSWORD},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


async def test_simultaneous_signups_with_one_email_create_one_account(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    body = {"email": EMAIL, "display_name": "Ada", "password": PASSWORD}
    responses = await asyncio.gather(*(client.post("/auth/signup", json=body) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [201] + [409] * 7
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("email", "not-an-email"), ("password", "short"), ("display_name", "")],
)
async def test_signup_validation(client: AsyncClient, field: str, value: str) -> None:
    body = {"email": EMAIL, "display_name": "Ada", "password": PASSWORD, field: value}
    response = await client.post("/auth/signup", json=body)
    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_error"
    assert problem["errors"][0]["field"] == field
    # The rejected input is never echoed back.
    assert PASSWORD not in response.text


# --- login ----------------------------------------------------------------


async def test_login_then_me(client: AsyncClient) -> None:
    await signup(client)
    tokens = await login(client, email="Ada@Example.COM")  # case-insensitive
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 900

    me = await client.get("/users/me", headers=bearer(tokens))
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


async def test_wrong_password_and_unknown_email_look_identical(client: AsyncClient) -> None:
    await signup(client)
    wrong = await client.post("/auth/login", json={"email": EMAIL, "password": "nope-nope-nope"})
    unknown = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


# --- access tokens ---------------------------------------------------------


async def test_me_requires_a_token(client: AsyncClient) -> None:
    response = await client.get("/users/me")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_expired_tampered_and_foreign_tokens_are_rejected(
    client: AsyncClient, settings: Settings
) -> None:
    user = await signup(client)
    secret = settings.jwt_secret.get_secret_value()
    now = datetime.now(UTC)
    claims = {"sub": user["id"], "role": "user", "aud": "lobbyd-api", "iat": now}

    expired = jwt.encode({**claims, "exp": now - timedelta(seconds=1)}, secret, "HS256")
    wrong_key = jwt.encode({**claims, "exp": now + timedelta(minutes=5)}, "x" * 40, "HS256")
    wrong_audience = jwt.encode(
        {**claims, "aud": "someone-else", "exp": now + timedelta(minutes=5)}, secret, "HS256"
    )
    unsigned = jwt.encode({**claims, "exp": now + timedelta(minutes=5)}, "", "none")

    for token in (expired, wrong_key, wrong_audience, unsigned, "garbage"):
        response = await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, token
        assert response.json()["code"] == "invalid_token"


async def test_token_for_a_deleted_user_stops_working(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(client)
    tokens = await login(client)
    async with sessionmaker.begin() as session:
        await session.delete(await session.scalar(select(User)))

    response = await client.get("/users/me", headers=bearer(tokens))
    assert response.status_code == 401


# --- refresh tokens --------------------------------------------------------


async def test_refresh_rotates_the_token(client: AsyncClient) -> None:
    await signup(client)
    first = await login(client)

    second = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    assert second.json()["refresh_token"] != first["refresh_token"]
    assert (await client.get("/users/me", headers=bearer(second.json()))).status_code == 200


async def test_refresh_tokens_are_stored_hashed(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(client)
    tokens = await login(client)
    async with sessionmaker() as session:
        stored = await session.scalar(select(RefreshToken.token_hash))
    assert stored is not None
    assert tokens["refresh_token"].encode() not in stored


async def test_reusing_a_refresh_token_revokes_the_whole_session(client: AsyncClient) -> None:
    await signup(client)
    first = await login(client)
    second = (
        await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()

    # The first token was already exchanged. Presenting it again means it leaked.
    replay = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_refresh_token"

    # So the legitimate holder's newer token is revoked too.
    after = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401


async def test_reuse_in_one_session_leaves_other_sessions_alone(client: AsyncClient) -> None:
    await signup(client)
    phone = await login(client)
    laptop = await login(client)

    await client.post("/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    await client.post("/auth/refresh", json={"refresh_token": phone["refresh_token"]})

    response = await client.post("/auth/refresh", json={"refresh_token": laptop["refresh_token"]})
    assert response.status_code == 200


async def test_concurrent_refreshes_of_one_token_succeed_exactly_once(
    client: AsyncClient,
) -> None:
    await signup(client)
    tokens = await login(client)
    body = {"refresh_token": tokens["refresh_token"]}

    responses = await asyncio.gather(*(client.post("/auth/refresh", json=body) for _ in range(10)))
    assert [r.status_code for r in responses].count(200) == 1


async def test_expired_refresh_token_is_rejected(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await signup(client)
    tokens = await login(client)
    async with sessionmaker.begin() as session:
        await session.execute(
            update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    response = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


async def test_logout_ends_the_session(client: AsyncClient) -> None:
    await signup(client)
    tokens = await login(client)
    body = {"refresh_token": tokens["refresh_token"]}

    assert (await client.post("/auth/logout", json=body)).status_code == 204
    assert (await client.post("/auth/refresh", json=body)).status_code == 401
    # Logging out twice, or with a made-up token, is not an error.
    assert (await client.post("/auth/logout", json=body)).status_code == 204
    assert (await client.post("/auth/logout", json={"refresh_token": "x"})).status_code == 204


# --- roles ----------------------------------------------------------------


async def test_require_role(
    app: FastAPI, client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    @app.get("/_organizers-only", dependencies=[Depends(require_role("organizer"))])
    async def organizers_only() -> dict[str, bool]:
        return {"ok": True}

    try:
        await signup(client)
        tokens = await login(client)
        assert (await client.get("/_organizers-only")).status_code == 401
        forbidden = await client.get("/_organizers-only", headers=bearer(tokens))
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "forbidden"

        # Roles are read from the database on each request, so a promotion
        # applies to tokens that were issued before it.
        for role in ("organizer", "admin"):
            async with sessionmaker.begin() as session:
                await session.execute(update(User).values(role=role))
            allowed = await client.get("/_organizers-only", headers=bearer(tokens))
            assert allowed.status_code == 200, role
    finally:
        app.router.routes.pop()


# --- settings -------------------------------------------------------------


def test_production_refuses_development_secrets() -> None:
    real = {
        "jwt_secret": SecretStr("r" * 48),
        "paysim_webhook_secret": SecretStr("w" * 48),
        "ticket_signing_key": SecretStr("a" * 43),
    }
    Settings(environment="production", **real)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        Settings(environment="production")
    with pytest.raises(ValidationError):
        Settings(environment="production", **{**real, "jwt_secret": SecretStr("too-short")})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret=real["jwt_secret"])  # webhook secret unset
    with pytest.raises(ValidationError):
        Settings(environment="production", **{**real, "ticket_signing_key": None})  # type: ignore[arg-type]
