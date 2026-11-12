"""Password hashing and token primitives."""

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from lobbyd.config import Settings

_hasher = PasswordHasher()  # Argon2id with the library's recommended parameters
_JWT_ALGORITHM = "HS256"
_JWT_AUDIENCE = "lobbyd-api"

# Checked when the email is unknown, so "no such account" takes as long as
# "wrong password" and response time does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash("lobbyd-dummy-password")


async def hash_password(password: str) -> str:
    # Argon2 is deliberately slow and CPU bound: keep it off the event loop.
    return await asyncio.to_thread(_hasher.hash, password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str | None, password: str) -> bool:
    ok = await asyncio.to_thread(_verify, password_hash or _DUMMY_HASH, password)
    return ok and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str


class InvalidTokenError(Exception):
    pass


def create_access_token(settings: Settings, user_id: uuid.UUID, role: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "aud": _JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=_JWT_ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_JWT_ALGORITHM],
            audience=_JWT_AUDIENCE,
            options={"require": ["sub", "role", "exp", "aud"]},
        )
        return AccessClaims(user_id=uuid.UUID(payload["sub"]), role=str(payload["role"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidTokenError from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> bytes:
    # A fast hash is right here: the token is 384 random bits, not a guessable password.
    return hashlib.sha256(token.encode()).digest()
