# 0008. Three layers of caching, each for a different kind of fact

**Status:** Accepted and implemented (phase 5)

## Context

During a sale almost every request is a read, and almost all of them ask for the same few things: the tournament, the seat map, what is still available, and "has my payment gone through yet?". The first load test ([benchmarks](../benchmarks.md)) showed the API processes pinned at 100% CPU while Postgres sat nearly idle. The cost was not slow queries. It was many small ones, each with its share of Python and network overhead, repeated for every request.

## Decision

Cache at three levels, matching each to how the fact changes.

| Layer | Used for | Lifetime | Why here |
| --- | --- | --- | --- |
| **In process** (`platform/memo.py`) | Seat labels, ticket type names | Forever (they are immutable) | Looked up on nearly every booking request. Even a Redis round trip is too slow for this |
| | A tournament's sale window, the signed-in user | 2 s and 5 s | Change rarely; a few seconds of staleness is harmless (see below) |
| **Redis, by time** (`platform/cache.py`) | Listings, games, tournament detail, seat map, availability | 1 s to 1 h | Shared by all API processes; the same for every visitor |
| **Redis, until changed** | A buyer's order | 30 s, dropped on every change | Polled constantly while a payment is pending; must update the moment it settles |

Responses are stored as rendered JSON text and sent as they are. A cache hit costs one Redis read and no serialisation.

### Stampedes

When a popular entry expires, every request that arrives before it is rebuilt would run the same query at once, at the moment the system can least afford it. `cached()` prevents this in two ways:

- **One builder.** The first request to find an entry missing takes a short lock and rebuilds it. The rest wait for its result.
- **Stale while revalidating.** Availability is fresh for one second and may be served for two more while a single request refreshes it. Nobody waits at all.

A test fires 200 simultaneous requests at uncached availability and asserts the query ran once.

### What staleness can and cannot do

Every cached value in this system is advisory. The decisions that matter are always re-made against the database:

- **Availability** up to three seconds old can show a seat as free that has just been taken. The claim is checked in Postgres, so the result is a refused hold, never a double sale. The refusal names the seats that went, and the client greys them out immediately.
- **A sale window** two seconds old can let a hold begin in the two seconds after a sale is cancelled.
- **A signed-in user** remembered for five seconds means a deleted account or a changed role takes effect within five seconds, not instantly. Without the lookup at all it would be fifteen minutes (the token's lifetime); with a lookup on every request it cost two milliseconds of every signed-in request.

### Invalidation

- Tournament detail is dropped when the organizer edits or publishes.
- An order is dropped by every path that changes one: the payment webhook, the status poll, and the job that expires stale orders. The drop happens **after** the transaction commits. Dropping before would let a concurrent read put the old order straight back.
- Everything else simply expires.

### Failure

If Redis is unavailable, `cached()` loads the value directly. Slower, never wrong. The Redis client uses a blocking connection pool: the default pool raises an error the moment it is exhausted, which a 200-request spike test triggered.

## Options not taken

- **HTTP caching and a CDN only.** Right for the seat map (which also sends `Cache-Control`), but availability and orders need invalidation a CDN cannot give, and the demo deployment has no CDN.
- **Cache everything in Redis.** Simpler, but the per-request lookups (labels, names, the user) are made several times per request; a network hop each would cost more than the query it replaced.
- **Invalidate availability on every hold.** During a sale that means dropping the entry dozens of times a second, which is no cache at all. A one-second lifetime gives the same freshness for far less work.

## Consequences

- Per-process caches mean two API processes can briefly disagree about a sale window or a user's role. Both converge within seconds, and neither is trusted for a decision that matters.
- Staleness is a tuning parameter in five places. Each is a named setting or constant with its reasoning beside it.
- Tests run with all three layers switched off, so they exercise the real queries. The caches have their own tests.
