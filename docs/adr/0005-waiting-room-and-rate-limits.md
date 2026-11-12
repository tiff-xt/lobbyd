# 0005. Waiting room and rate limits in Redis

**Status:** Accepted and implemented (phase 5)

## Context

At the moment a popular sale opens, demand is many times what the write path can serve, and most of it is doomed: there are 12,000 seats and 100,000 people. Letting everyone hit the hold endpoint at once produces lock queues, timeouts, and retries that make it worse. It is also unfair, because the outcome depends on who retries hardest.

## Decision

Two layers, both in Redis.

### 1. Waiting room, per tournament

- Enabled by the organizer for sales expected to be oversubscribed (`waiting_room` on the tournament).
- Before sales open, arrivals wait in a **lobby** (a Redis set). When sales open, the lobby is drawn into the queue in **random order**, so arriving early or having a fast connection earns a place in the draw, not a head start. Later arrivals join the back.
- The draw and every arrival racing with it run inside one Lua script, so they happen in a definite order: nobody who waited in the lobby can end up behind someone who arrived after opening. The shuffle is `SPOP` until empty, which removes a random member each time.
- The queue is a sorted set. An **admission cursor** says how many from the front have been let through.
- A worker advances the cursor **by the clock**: `rate x seconds elapsed`, computed inside a script. Because it is time-based and not "N per call", several workers can run it without admitting anyone faster, and a tick that is late catches up exactly.
- The cursor may run slightly ahead of the queue, so a lone visitor to a quiet sale is admitted at once. It is capped at one second's worth, so an idle hour does not bank thousands of turns for the next crowd.
- Being admitted means receiving a **signed token** bound to that user and that tournament, valid for ten minutes. `POST /holds` requires it. Verifying it needs no Redis call, so the booking path gains almost nothing in cost. A token passed to a friend, used on another sale, expired, or forged is refused.
- Clients poll their position every second or two. The endpoint touches no database connection.

### 2. Rate limits

Token buckets, one Lua script so check and take are atomic. A bucket allows a short burst (a page that fires several requests at once) and caps the sustained rate.

| Bucket | Keyed by | Burst | Sustained |
| --- | --- | --- | --- |
| Reads | Address | 120 | 30 per second |
| Sign-in, sign-up, token refresh | Address | 10 | 1 per 5 seconds |
| Holds and checkout | User | 10 | 1 per second |

Refusals are `429` with `Retry-After`. Booking is limited per user, not per address, so a household or an office behind one address is not punished for one person's script. `X-Forwarded-For` is believed only when the deployment says every request comes through its own proxy; trusting it from anyone would let a client choose its own bucket.

## Why Redis, when ADR 0002 refused it for seat locks

The difference is what happens when it is wrong. If Redis loses the waiting room, people lose their place in a queue: annoying, recoverable, and no sale is affected. If Redis lost seat locks, seats would be sold twice. Redis is used where its failure mode is tolerable and its speed is needed.

**Failure policy:** if Redis is unreachable, rate limiting fails open (traffic is let through). A waiting room fails closed by construction: nobody can be admitted without Redis, and holds for that tournament require an admission token, so an unprotected spike cannot reach the booking path. Tokens already issued keep working, because checking them needs no Redis.

## Consequences

- The write path sees a steady, chosen load regardless of demand. This is the main reason one Postgres primary is enough.
- Extra moving part and extra user-facing states (queued, admitted, sold out) that must be communicated clearly.
- Admission rate is a tuning parameter (`waiting_room_admit_per_second`), set from the [benchmark](../benchmarks.md). It could adapt to observed hold latency later.
- Sold-out detection is not automatic yet: when a guarded sale sells out, people still queued are admitted and then see that nothing is left. Closing the queue at that point is future work.
- Tokens do not stop a determined bot from joining the queue many times with many accounts. That needs account-level defences (verified email, per-payment-method limits), noted as future work.
