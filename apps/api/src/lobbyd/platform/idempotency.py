"""Idempotency keys: a retried request returns the first response and does nothing else.

See docs/adr/0003-idempotency-keys.md. The key row, the work, and the stored
response all commit in one transaction, so there is no state in which the work
happened but the key was not recorded, or the reverse.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lobbyd.platform.db import Base
from lobbyd.platform.errors import PROBLEM_JSON, ProblemError, problem_body
from oncely import IdempotencyKeys, KeyReused, Refused, fingerprint, is_valid_key

REPLAY_HEADER = "Idempotent-Replay"

# The mechanism lives in the oncely package (libs/oncely). This module is
# lobbyd's side of it: the header, the problem+json bodies, the HTTP response.
_keys = IdempotencyKeys(Base.metadata)


class IdempotencyKey(Base):
    """The keys table, mapped so tests and queries can use it like any model."""

    __table__ = _keys.table


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
    if not is_valid_key(key):
        raise ProblemError(
            400, "idempotency_key_invalid", "Idempotency-Key must be 8 to 255 printable characters."
        )
    return key


IdempotencyKeyHeader = Annotated[str, Depends(require_idempotency_key)]


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

    async def answer() -> tuple[int, str]:
        try:
            result = await work()
        except ProblemError as exc:
            if exc.status >= 500:
                raise
            refusal = problem_body(request.url.path, exc.status, exc.code, exc.detail, **exc.extra)
            raise Refused(exc.status, json.dumps(refusal)) from exc
        return success_status, result.model_dump_json()

    try:
        outcome = await _keys.run(
            session,
            scope=str(user_id),
            key=key,
            fingerprint=fingerprint(request.method, request.url.path, body.model_dump_json()),
            work=answer,
        )
    except KeyReused as exc:
        raise ProblemError(
            422,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request.",
        ) from exc
    return _response(outcome.status, outcome.body, replay=outcome.replayed)


async def prune(session: AsyncSession, *, older_than_hours: int = 24) -> int:
    """Delete keys past the retry window. Returns how many were removed."""
    return await _keys.prune(session, older_than=timedelta(hours=older_than_hours))
