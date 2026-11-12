# 0003. Idempotency keys stored in the same transaction as the effect

**Status:** Accepted

## Context

Networks fail after the server has done the work but before the client hears about it. The client retries. Without protection, a retry creates a second hold, a second order, or a second charge. The same applies in the other direction: the payment provider retries webhooks, and may deliver one twice.

## Decision

### Client requests

Every creating endpoint (`POST /holds`, `POST /orders`, team registration, waitlist) requires an `Idempotency-Key` header: a random value the client generates once per intended action and reuses on every retry.

1. Begin a transaction. Insert `(user_id, key, request_hash, 'in_progress')` into `idempotency_keys`.
2. If the insert conflicts:
   - stored row is `completed` and the request hash matches: return the stored response, do nothing else;
   - hash differs: `422`, the key is being reused for a different request;
   - row is `in_progress`: `409`, the first attempt is still running, retry shortly.
3. Otherwise do the work, store the response on the key row, and commit, all in the same transaction.

Because the key row and the effect commit together, there is no state where the work happened but the key was not recorded, or the reverse.

Only outcomes that are final are stored. A `409 seat taken` is stored (the answer will not change for that key). A `500` or a timeout rolls back the transaction, key row included, so the retry runs fresh.

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
- A crashed request leaves no `in_progress` row behind, because the row only exists inside the transaction that crashed.
