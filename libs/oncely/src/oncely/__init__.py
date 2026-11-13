"""Idempotency keys and a transactional outbox for async SQLAlchemy and Postgres.

Two small tools for the same problem: a network that retries.

- `IdempotencyKeys` runs a piece of work once per key and gives every retry
  the first answer.
- `Outbox` records an event in the same transaction as the change it
  describes, and a relay publishes it afterwards, at least once.

`oncely.streams` adds a Redis Streams publisher and a consumer with
retries and a dead-letter stream. It needs the `redis` extra.
"""

from oncely.idempotency import (
    IdempotencyKeys,
    KeyReused,
    Outcome,
    Refused,
    fingerprint,
    is_valid_key,
)
from oncely.outbox import Event, Outbox, Publisher

__all__ = [
    "Event",
    "IdempotencyKeys",
    "KeyReused",
    "Outbox",
    "Outcome",
    "Publisher",
    "Refused",
    "fingerprint",
    "is_valid_key",
]
