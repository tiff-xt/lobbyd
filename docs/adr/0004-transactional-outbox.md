# 0004. Transactional outbox for events

**Status:** Accepted and implemented (phase 4)

## Context

When an order is paid, other things must follow: a confirmation email, ticket PDFs, analytics, live availability updates, waitlist promotion. The naive version commits to Postgres and then publishes to a queue. If the process dies between the two, the order is paid and nobody is told. Publishing first has the opposite failure: an event for an order that was rolled back.

## Decision

Write the event to an `outbox` table **in the same transaction** as the state change. A separate relay process publishes outbox rows to Redis Streams and marks them published.

```sql
-- Relay loop. SKIP LOCKED lets several relays run without blocking each other.
SELECT id, topic, payload
  FROM outbox
 WHERE published_at IS NULL
 ORDER BY id
 LIMIT 100
   FOR UPDATE SKIP LOCKED;
-- publish each to the stream, then:
UPDATE outbox SET published_at = now() WHERE id = ANY(:ids);
```

**Delivery guarantee: at least once.** If the relay crashes after publishing but before marking, the rows are published again. So every consumer is safe to run twice, each in the way that fits it:

| Handler | Why running it twice is harmless |
| --- | --- |
| Create the payment at the provider | Our payment id is the provider's idempotency key; it returns the payment it already made |
| Request a refund | Same, with a refund key |
| Send an email | A unique constraint on (kind of message, order). The second insert does nothing and no second email is sent |

### Consuming

Workers read the stream through a consumer group. An event stays *pending* until the worker that received it acknowledges it, which it does only after every handler succeeded.

- **A handler fails:** the event is left pending and retried once it has been idle for the retry interval (10 seconds by default).
- **A worker dies mid-event:** the same mechanism. Another worker claims events that have been pending too long (`XAUTOCLAIM`).
- **An event keeps failing:** after five attempts it is copied to a dead-letter stream, acknowledged, and logged as an error, so one poison message cannot block the rest or retry forever.

### What is deliberately not an event

Anything that must be true before the buyer gets an answer happens in the request's transaction: claiming seats, recording the order, issuing tickets when payment succeeds. Events are for consequences that may lag: calling the provider, sending email. The provider call being an event is a design choice with a large payoff: checkout never holds a database transaction open across a network request, and it keeps working when the provider is slow or down.

No job-queue library is used. The outbox plus a consumer group covers what is needed, and keeps the delivery semantics in code that can be read in one sitting.

## Options not taken

- **Publish after commit, accept occasional loss.** Not acceptable for "your tickets" emails.
- **Change data capture** (logical replication, Debezium). The right tool at larger scale and it removes polling, but it is heavy infrastructure for one database.
- **`LISTEN/NOTIFY`.** Notifications are lost if nobody is listening at that moment. It may be added later purely as a wake-up signal to cut polling delay, with the table still the source of truth.
- **A broker with transactions spanning the database** (two-phase commit). Operationally fragile and unnecessary.

## Consequences

- An event is never lost and never describes something that did not happen.
- Events arrive a little late (the polling interval) and can arrive twice. Consumers are written for that.
- With several relays, strict global ordering is not guaranteed. Events carry the entity's id and a sequence, and consumers that care about order apply guarded transitions, as with webhooks.
- Outbox lag (age of the oldest unpublished row) is a key health metric.

## Later: extracted

The mechanism now lives in the [oncely](https://github.com/tiff-xt/oncely) package, with its own tests. lobbyd keeps a thin module over it for what is specific to this service.
