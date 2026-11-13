"""Idempotency keys: a retried request gets the first response and does nothing else.

The key row, the work and the stored response all commit in one transaction,
so there is no state in which the work happened but the key was not recorded,
or the reverse.
"""

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

_KEY_FORMAT = re.compile(r"^[\x21-\x7e]{8,255}$")  # printable ASCII, no spaces


def is_valid_key(key: str) -> bool:
    """Whether a client-supplied key is usable: 8 to 255 printable characters."""
    return _KEY_FORMAT.match(key) is not None


def fingerprint(*parts: str | bytes) -> bytes:
    """A digest of what the request asks for, e.g. method, path and body.

    Stored with the key, so the same key arriving with a different request is
    recognised as a client bug instead of being answered as a retry.
    """
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        # Length-prefixed, so ("ab", "c") and ("a", "bc") differ.
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.digest()


@dataclass(frozen=True, slots=True)
class Outcome:
    """The response for a key: the status and body first produced for it."""

    status: int
    body: str
    #: True when this call did no work and is repeating an earlier answer.
    replayed: bool


class Refused(Exception):
    """Raise from the work to give a final answer that is not a success.

    "That seat is taken" is as final as "here is your order": the work's
    writes are undone, the refusal is stored, and every retry gets it again.
    Any other exception is treated as a crash: nothing is stored, and a retry
    runs the work from scratch.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"refused with status {status}")
        self.status = status
        self.body = body


class KeyReused(Exception):
    """The key was already used for a different request."""


class IdempotencyKeys:
    """A table of keys, attached to your metadata so your migrations create it.

    `scope` separates one caller's keys from another's (a user id, a tenant, an
    API client), so two callers choosing the same key never see each other's
    responses.
    """

    def __init__(self, metadata: MetaData, *, table_name: str = "idempotency_keys") -> None:
        self.table = Table(
            table_name,
            metadata,
            Column("scope", Text, primary_key=True),
            Column("key", Text, primary_key=True),
            Column("request_hash", LargeBinary, nullable=False),
            Column("response_code", Integer),
            # Text, exactly as first sent, so a replay is byte for byte the
            # same response (jsonb would reorder the keys).
            Column("response_body", Text),
            Column(
                "created_at",
                TIMESTAMP(timezone=True),
                server_default=func.now(),
                nullable=False,
                index=True,
            ),
        )

    async def run(
        self,
        session: AsyncSession,
        *,
        scope: str,
        key: str,
        fingerprint: bytes,
        work: Callable[[], Awaitable[tuple[int, str]]],
    ) -> Outcome:
        """Run `work` once per (scope, key) and return its answer on every call.

        `work` returns (status, body). The caller owns the transaction: commit
        it and the answer is permanent, roll it back and the key is free again.
        Raises `KeyReused` if the key was first seen with another fingerprint.
        """
        t = self.table
        mine = (t.c.scope == scope) & (t.c.key == key)

        # If another request with this key is in flight, this insert waits for
        # it to finish. Then either it committed (no row comes back and its
        # response is replayed) or it rolled back (the row is ours, and so is
        # the work).
        claimed = await session.execute(
            pg_insert(t)
            .values(scope=scope, key=key, request_hash=fingerprint)
            .on_conflict_do_nothing()
            .returning(t.c.key)
        )
        if claimed.first() is None:
            stored = (
                await session.execute(
                    select(t.c.request_hash, t.c.response_code, t.c.response_body).where(mine)
                )
            ).one()
            if bytes(stored.request_hash) != fingerprint:
                raise KeyReused(key)
            if stored.response_code is None or stored.response_body is None:
                # Cannot happen: the row and its response commit together.
                raise RuntimeError("idempotency key stored without a response")
            return Outcome(stored.response_code, stored.response_body, replayed=True)

        try:
            # A savepoint: if the work is refused, its partial writes are
            # undone but the key row survives to record the refusal.
            async with session.begin_nested():
                status, body = await work()
        except Refused as refusal:
            status, body = refusal.status, refusal.body

        await session.execute(
            update(t).where(mine).values(response_code=status, response_body=body)
        )
        return Outcome(status, body, replayed=False)

    async def prune(self, session: AsyncSession, *, older_than: timedelta) -> int:
        """Delete keys past the retry window. Returns how many were removed."""
        t = self.table
        deleted = await session.execute(
            delete(t).where(t.c.created_at < func.now() - older_than).returning(t.c.key)
        )
        return len(deleted.all())
