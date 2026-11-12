# 0003. Idempotency keys stored in the same transaction as the effect

**Status:** Accepted and implemented (client keys in phase 3, provider calls and webhooks in phase 4)

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

Our payment id is sent as the provider's idempotency key, so a retried "create payment" call returns the existing payment. Refunds use `refund-<payment id>` the same way. This is what lets the worker simply retry a provider call that timed out without knowing whether it got through.

### Webhooks from the payment provider

1. Verify the signature: HMAC-SHA256 over `<timestamp>.<body>`, compared in constant time. Reject timestamps older than five minutes, so a captured delivery cannot be replayed later.
2. Insert the provider's event id into `payment_events` (primary key) with `ON CONFLICT DO NOTHING`. No row back means it was already handled: return `200` and stop.
3. Apply the change as a **guarded transition**. A payment may only move forward:

   ```
   created -> processing -> succeeded -> refund_pending -> refunded
                        \-> failed
   ```

   A `processing` or `failed` event arriving after `succeeded` matches no allowed step and changes nothing. Orders are guarded the same way under their row lock.
4. All of the above in one transaction with the order update, ticket issuing, and outbox write.

Steps 2 and 3 are each sufficient to make a duplicate harmless. Both are kept: the event table is also the audit trail, and the guard also covers news that arrives by polling instead of by webhook.

**Finding the payment.** The webhook is matched to a payment by *our* id, which the provider echoes back as its `reference`, not by the provider's id. The worker creates the payment at the provider and then stores the provider's id, and the provider's webhook can win that race. Matching on our own id means it is still recognised.

**Webhooks are the fast path, not the only one.** A payment that has been quiet for a minute is looked up at the provider and the result goes through the same guarded transition. A lost webhook therefore delays an order; it cannot leave a buyer charged with no tickets.

## Options not taken

- **Idempotency state in Redis.** Fast, but it cannot commit atomically with the Postgres transaction, which reintroduces the gap this exists to close.
- **Natural keys only** (for example "one active hold per user per tournament"). Useful as an extra constraint, and used, but it cannot return the original response to a retry and does not generalise.

## Consequences

- One extra insert per write request, on a table that is pruned after 24 hours.
- Clients must generate and persist keys properly. The API rejects creating requests without one, so the mistake is caught early.
- A crashed request leaves no key behind, because the row only exists inside the transaction that crashed.
- A duplicate that arrives while the original is still running waits for it instead of failing fast. The wait is bounded by how long the original takes.
