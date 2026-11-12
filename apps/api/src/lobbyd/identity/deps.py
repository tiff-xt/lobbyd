"""Authentication and authorisation dependencies for other modules to use."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lobbyd.identity import security, service
from lobbyd.identity.models import User
from lobbyd.platform import ratelimit
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.memo import Memo
from lobbyd.platform.ratelimit import Limit

_bearer = HTTPBearer(auto_error=False)

_users: Memo[uuid.UUID, User] = Memo(ttl_seconds=5, max_entries=100_000)


def configure_user_memo(seconds: float) -> None:
    """How long a signed-in user may be remembered between lookups. Zero turns it off."""
    _users.ttl = seconds
    _users.clear()


_CHALLENGE = {"WWW-Authenticate": "Bearer"}


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None:
        raise ProblemError(401, "not_authenticated", "Sign in to continue.", headers=_CHALLENGE)
    try:
        claims = security.decode_access_token(settings, credentials.credentials)
    except security.InvalidTokenError as exc:
        raise ProblemError(
            401, "invalid_token", "The access token is invalid or has expired.", headers=_CHALLENGE
        ) from exc

    # The token alone is not trusted for who the user is now: the account
    # may have been deleted or its role changed since it was issued. So the
    # user is looked up, and remembered per process for a few seconds, because
    # this runs on every signed-in request. A change therefore takes effect
    # within those seconds, not at the token's expiry fifteen minutes later.
    user = _users.get(claims.user_id)
    if user is None:
        user = await service.get_user(session, claims.user_id)
        if user is not None:
            session.expunge(user)  # a plain value now, safe to share between requests
            _users.put(claims.user_id, user)
    if user is None:
        raise ProblemError(
            401, "invalid_token", "The access token is invalid or has expired.", headers=_CHALLENGE
        )
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User | None:
    """For public endpoints that show more to a signed-in user. A bad token is still an error."""
    if credentials is None:
        return None
    return await get_current_user(session, settings, credentials)


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def require_role(*roles: str) -> Callable[[User], Awaitable[User]]:
    """Dependency factory: the current user must hold one of `roles`. Admins always pass."""

    async def dependency(user: CurrentUser) -> User:
        if user.role != "admin" and user.role not in roles:
            raise ProblemError(403, "forbidden", "You do not have permission to do this.")
        return user

    return dependency


def limit_per_user(limit: Limit) -> Callable[[Request, User], Awaitable[None]]:
    """Dependency: one rate-limit bucket per signed-in user."""

    async def dependency(request: Request, user: CurrentUser) -> None:
        await ratelimit.take(request, limit, str(user.id))

    return dependency
