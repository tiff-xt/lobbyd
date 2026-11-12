# 0004. Transactional outbox for events

**Status:** Accepted

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

**Delivery guarantee: at least once.** If the relay crashes after publishing but before marking, the rows are published again. So every consumer is idempotent: it records the event id it has processed (or performs an action that is naturally safe to repeat) and skips repeats.

Workers read the stream through consumer groups, acknowledge after success, and messages that keep failing move to a dead-letter stream after a fixed number of attempts, with an alert.

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
