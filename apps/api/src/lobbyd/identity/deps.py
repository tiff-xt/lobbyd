"""Authentication and authorisation dependencies for other modules to use."""

from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lobbyd.identity import security, service
from lobbyd.identity.models import User
from lobbyd.platform.db import SessionDep
from lobbyd.platform.deps import SettingsDep
from lobbyd.platform.errors import ProblemError

_bearer = HTTPBearer(auto_error=False)

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

    # Load the user so a deleted account or a changed role takes effect
    # immediately, not when the token expires.
    user = await service.get_user(session, claims.user_id)
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
