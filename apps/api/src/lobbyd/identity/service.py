"""Account and session logic. The only code that touches the identity tables."""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.config import Settings
from lobbyd.identity import security
from lobbyd.identity.models import RefreshToken, User
from lobbyd.identity.schemas import TokenPair
from lobbyd.platform.errors import ProblemError
from lobbyd.platform.ids import uuid7

log = structlog.get_logger()


def _invalid_credentials() -> ProblemError:
    # One message for unknown email and wrong password alike.
    return ProblemError(401, "invalid_credentials", "Email or password is incorrect.")


def _invalid_refresh_token() -> ProblemError:
    return ProblemError(401, "invalid_refresh_token", "The refresh token is not valid.")


async def create_user(
    session: AsyncSession, *, email: str, display_name: str, password: str
) -> User:
    user = User(
        email=email,
        display_name=display_name,
        password_hash=await security.hash_password(password),
        role="user",
    )
    session.add(user)
    try:
        # The unique constraint decides, not a prior SELECT: two simultaneous
        # signups with one email cannot both pass.
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ProblemError(
            409, "email_taken", "An account with this email already exists."
        ) from exc
    await session.refresh(user)
    log.info("user_created", user_id=str(user.id))
    return user


async def _issue_tokens(
    session: AsyncSession, settings: Settings, user: User, family_id: uuid.UUID
) -> TokenPair:
    refresh_token = security.new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id,
            token_hash=security.hash_refresh_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    await session.flush()
    return TokenPair(
        access_token=security.create_access_token(settings, user.id, user.role),
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_seconds,
    )


async def login(
    session: AsyncSession, settings: Settings, *, email: str, password: str
) -> TokenPair:
    user = await session.scalar(select(User).where(User.email == email))
    if not await security.verify_password(user.password_hash if user else None, password):
        raise _invalid_credentials()
    assert user is not None  # noqa: S101 - verify_password is False without a stored hash

    if security.needs_rehash(user.password_hash):
        # Hashing parameters were raised since this password was stored.
        user.password_hash = await security.hash_password(password)

    log.info("login", user_id=str(user.id))
    return await _issue_tokens(session, settings, user, family_id=uuid7())


async def refresh(session: AsyncSession, settings: Settings, *, refresh_token: str) -> TokenPair:
    token_hash = security.hash_refresh_token(refresh_token)

    # Claim the token in one conditional write. Of two requests racing with
    # the same token, exactly one gets a row back.
    claimed = (
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.used_at.is_(None),
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
            .values(used_at=datetime.now(UTC))
            .returning(RefreshToken.user_id, RefreshToken.family_id)
        )
    ).one_or_none()

    if claimed is None:
        existing = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if existing is not None and existing.used_at is not None:
            # A token that was already exchanged is being presented again.
            # Either the client or an attacker holds a copy; we cannot tell
            # which, so every token descended from that login is revoked.
            await _revoke_family(session, existing.family_id)
            # Commit now: the 401 below would otherwise roll the revocation back.
            await session.commit()
            log.warning("refresh_token_reuse", user_id=str(existing.user_id))
        raise _invalid_refresh_token()

    user = await session.get(User, claimed.user_id)
    if user is None:
        raise _invalid_refresh_token()
    return await _issue_tokens(session, settings, user, family_id=claimed.family_id)


async def logout(session: AsyncSession, *, refresh_token: str) -> None:
    """Ends the session this token belongs to. Unknown tokens are ignored."""
    family_id = await session.scalar(
        select(RefreshToken.family_id).where(
            RefreshToken.token_hash == security.hash_refresh_token(refresh_token)
        )
    )
    if family_id is not None:
        await _revoke_family(session, family_id)


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)
