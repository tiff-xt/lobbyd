# 0003. Idempotency keys stored in the same transaction as the effect

**Status:** Accepted. Client request keys implemented in phase 3; payment provider parts are phase 4

## Context

Networks fail after the server has done the work but before the client hears about it. The client retries. Without protection, a retry creates a second hold, a second order, or a second charge. The same applies in the other direction: the payment provider retries webhooks, and may deliver one twice.

## Decision

### Client requests

Every creating endpoint (`POST /holds`, `POST /orders`, later team registration and the waitlist) requires an `Idempotency-Key` header: a random value the client generates once per intended action and reuses on every retry.

1. In the request's transaction, insert `(user_id, key, request_hash)` into `idempotency_keys` with `ON CONFLICT DO NOTHING`.
2. If the insert returns no row, the key has been used before. Read the stored row:
   - request hash matches: return the stored response, with an `Idempotent-Replay: true` header, and do nothing else;
   - hash differs: `422`, the key is being reused for a different request.
3. Otherwise do the work inside a savepoint, store the response on the key row, and commit, all in the same transaction.

Because the key row and the effect commit together, there is no state where the work happened but the key was not recorded, or the reverse.

**Two copies arriving at once.** The second insert blocks on the first's uncommitted row, because Postgres has to know whether that row will exist. When the first commits, the second gets no row back and replays the response. If the first rolls back, the second gets the row and does the work itself. No "in progress" state is ever visible, so none is stored.

**Refusals are answers too.** A `409 seats_unavailable` is stored like a success: a retry with that key gets the same refusal even if the seat has since freed up, because a retry asks "what happened?", not "try again". The savepoint is what makes this possible: the refused work is rolled back, the key row is not. A new attempt uses a new key. Server errors are different: the whole transaction rolls back, key included, so the retry runs fresh.

**Byte for byte.** The response is stored as the exact text first sent, not as `jsonb`, which would reorder its keys. A replay is indistinguishable from the original apart from the header.

Keys are scoped to the user, so one user can never be handed another's response by guessing a key.

### Calls to the payment provider

The order id is sent as the provider's idempotency key, so a retried "create payment" call returns the existing payment.

### Webhooks from the payment provider

1. Verify the signature and reject stale timestamps.
2. Insert the provider's event id into `payment_events` (primary key). A conflict means it was already handled: return `200` and stop.
3. Apply the state change as a guarded transition, for example `UPDATE orders SET status = 'paid' WHERE id = :id AND status = 'pending'`. Guarded transitions make out-of-order delivery safe: a `failed` event arriving after `succeeded` matches no row and changes nothing.
4. All of the above in one transaction with the ticket issuing and the outbox write.

## Options not taken

- **Idempotency state in Redis.** Fast, but it cannot commit atomically with the Postgres transaction, which reintroduces the gap this exists to close.
- **Natural keys only** (for example "one active hold per user per tournament"). Useful as an extra constraint, and used, but it cannot return the original response to a retry and does not generalise.

## Consequences

- One extra insert per write request, on a table that is pruned after 24 hours.
- Clients must generate and persist keys properly. The API rejects creating requests without one, so the mistake is caught early.
- A crashed request leaves no key behind, because the row only exists inside the transaction that crashed.
- A duplicate that arrives while the original is still running waits for it instead of failing fast. The wait is bounded by how long the original takes.
