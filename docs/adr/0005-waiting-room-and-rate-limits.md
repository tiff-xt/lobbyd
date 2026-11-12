# 0005. Waiting room and rate limits in Redis

**Status:** Accepted

## Context

At the moment a popular sale opens, demand is many times what the write path can serve, and most of it is doomed: there are 12,000 seats and 100,000 people. Letting everyone hit the hold endpoint at once produces lock queues, timeouts, and retries that make it worse. It is also unfair, because the outcome depends on who retries hardest.

## Decision

Two layers, both in Redis.

### 1. Waiting room, per tournament

- Enabled by the organizer for sales expected to be oversubscribed.
- Before sales open, users join a lobby. At opening time everyone in the lobby is placed in a queue in **random order**, so arriving early or having a fast connection earns nothing. Later arrivals join the back.
- The queue is a Redis sorted set. An admitter process releases users from the front at a fixed rate, tuned to what the database can serve.
- An admitted user receives a short-lived **signed admission token** bound to their user id and the tournament. `POST /holds` for that tournament requires it. Verifying the signature needs no Redis call.
- Users poll for their position (and later receive it over WebSocket).
- When inventory is gone the queue is closed and everyone remaining is told, instead of waiting for nothing.

### 2. Rate limits, everywhere

Token bucket per user and per IP, implemented as a Lua script so check and decrement are atomic. Separate buckets for cheap reads, hold attempts, and login. Responses include `Retry-After`.

## Why Redis, when ADR 0002 refused it for seat locks

The difference is what happens when it is wrong. If Redis loses the waiting room, people lose their place in a queue: annoying, recoverable, and no sale is affected. If Redis lost seat locks, seats would be sold twice. Redis is used where its failure mode is tolerable and its speed is needed.

**Failure policy:** if Redis is unreachable, rate limiting fails open for reads (browsing keeps working) and the hold endpoint for a tournament with an active waiting room fails closed (no admissions until it is back), because an unprotected spike is the thing the waiting room exists to prevent.

## Consequences

- The write path sees a steady, chosen load regardless of demand. This is the main reason one Postgres primary is enough.
- Extra moving part and extra user-facing states (queued, admitted, sold out) that must be communicated clearly.
- Admission rate is a tuning parameter. The benchmark will be used to set it, and it can adapt to observed hold latency later.
- Tokens do not stop a determined bot from joining the queue many times with many accounts. That needs account-level defences (verified email, per-payment-method limits), noted as future work.
