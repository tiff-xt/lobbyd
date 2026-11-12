"""Idempotency keys: a retried request returns the first response and does nothing else.

See docs/adr/0003-idempotency-keys.md. The key row, the work, and the stored
response all commit in one transaction, so there is no state in which the work
happened but the key was not recorded, or the reverse.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import Integer, LargeBinary, Text, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from lobbyd.platform.db import Base
from lobbyd.platform.errors import PROBLEM_JSON, ProblemError, problem_body

REPLAY_HEADER = "Idempotent-Replay"
_KEY_FORMAT = re.compile(r"^[\x21-\x7e]{8,255}$")  # printable ASCII, no spaces


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    # The same key with a different request is a client bug, not a retry.
    request_hash: Mapped[bytes] = mapped_column(LargeBinary)
    response_code: Mapped[int | None] = mapped_column(Integer)
    # Stored as text, exactly as first sent, so a replay is byte for byte the
    # same response (jsonb would reorder the keys).
    response_body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), index=True
    )


def require_idempotency_key(
    key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "A random value (a UUID is ideal) generated once per intended action "
                "and sent again unchanged on every retry of it."
            ),
        ),
    ] = None,
) -> str:
    if key is None:
        raise ProblemError(
            400, "idempotency_key_required", "This request needs an Idempotency-Key header."
        )
    if not _KEY_FORMAT.match(key):
        raise ProblemError(
            400, "idempotency_key_invalid", "Idempotency-Key must be 8 to 255 printable characters."
        )
    return key


IdempotencyKeyHeader = Annotated[str, Depends(require_idempotency_key)]


def _fingerprint(request: Request, body: BaseModel) -> bytes:
    canonical = f"{request.method} {request.url.path}\n{body.model_dump_json()}"
    return hashlib.sha256(canonical.encode()).digest()


def _response(status: int, body: str, *, replay: bool) -> Response:
    return Response(
        body,
        status_code=status,
        media_type=PROBLEM_JSON if status >= 400 else "application/json",
        headers={REPLAY_HEADER: "true"} if replay else None,
    )


async def run_idempotent(
    session: AsyncSession,
    request: Request,
    *,
    user_id: uuid.UUID,
    key: str,
    body: BaseModel,
    success_status: int,
    work: Callable[[], Awaitable[BaseModel]],
) -> Response:
    """Run `work` once per (user, key) and return its response on every call.

    Client errors (a seat was taken, the hold expired) are final answers and
    are stored like successes. Server errors are not: the transaction rolls
    back, the key with it, and a retry runs from scratch.
    """
    fingerprint = _fingerprint(request, body)

    # If another request with this key is in flight, this insert waits for it
    # to finish. Then either it committed (we get no row back and replay its
    # response) or it rolled back (we get the row and do the work ourselves).
    claimed = await session.execute(
        pg_insert(IdempotencyKey)
        .values(user_id=user_id, key=key, request_hash=fingerprint)
        .on_conflict_do_nothing()
        .returning(IdempotencyKey.key)
    )
    if claimed.first() is None:
        stored = (
            await session.execute(
                select(
                    IdempotencyKey.request_hash,
                    IdempotencyKey.response_code,
                    IdempotencyKey.response_body,
                ).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            )
        ).one()
        if stored.request_hash != fingerprint:
            raise ProblemError(
                422,
                "idempotency_key_reused",
                "This Idempotency-Key was already used for a different request.",
            )
        if stored.response_code is None or stored.response_body is None:
            # Cannot happen: the row and its response commit together.
            raise RuntimeError("idempotency key stored without a response")
        return _response(stored.response_code, stored.response_body, replay=True)

    try:
        # A savepoint: if the work is refused, its partial writes are undone
        # but the key row survives to record the refusal.
        async with session.begin_nested():
            result = await work()
        status, response_body = success_status, result.model_dump_json()
    except ProblemError as exc:
        if exc.status >= 500:
            raise
        status = exc.status
        response_body = json.dumps(
            problem_body(request.url.path, exc.status, exc.code, exc.detail, **exc.extra)
        )

    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        .values(response_code=status, response_body=response_body)
    )
    return _response(status, response_body, replay=False)


async def prune(session: AsyncSession, *, older_than_hours: int = 24) -> int:
    """Delete keys past the retry window. Returns how many were removed."""
    deleted = await session.execute(
        delete(IdempotencyKey)
        .where(
            IdempotencyKey.created_at
            < func.now() - text(f"interval '{int(older_than_hours)} hours'")
        )
        .returning(IdempotencyKey.key)
    )
    return len(deleted.all())
