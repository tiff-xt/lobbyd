# Scaling to 100 times the load

The [benchmarks](benchmarks.md) measured one small machine: 4 CPUs shared with the load generator, about 700 orders in ten seconds before it sheds load. This document is about what changes, in what order, as the load grows a hundredfold: the [reference sale](design.md#3-capacity-planning) of 100,000 buyers in a minute, and then a platform running many such sales at once.

None of it is built. It is here because the design was made with these steps in mind, and because "what would you do next" is the honest end of any system design.

## Start from what limits it today

The benchmarks say what runs out first, in this order:

1. **API CPU.** Python handles each request on one core. The first thing to saturate was the API process, not the database.
2. **Database connections.** Every hold needs a connection for a few milliseconds. Past the pool size, requests queue, and past a timeout they are refused.
3. **The hot rows.** Counters for standing tickets are single rows that every buyer of that type updates.

Postgres itself, on the same small machine, was never the limit. That shapes the plan: most of the early steps are about spending less per request and admitting fewer requests, not about a bigger database.

## Step 1. More API instances (10x)

The API holds no state between requests: sessions are tokens, the cache and rate limits are in Redis, live updates fan out through Redis pub/sub. So this is configuration: run 10 to 20 instances behind a load balancer.

What has to change with it:

- **A connection pooler** (PgBouncer in transaction mode) between the instances and Postgres. Twenty instances with twenty connections each is more than Postgres should hold open. The app would switch its driver's prepared statement cache off to work with it.
- **The in-process memos** (tournament details, signed-in user, two to five seconds) become per-instance. They already tolerate being stale for that long, so nothing breaks; hit rate drops a little.
- **Rate limits** are already global, since the token buckets live in Redis.

Workers scale the same way and already can: every query that picks up work uses `SKIP LOCKED`, and stream consumers share a group.

## Step 2. Shape the spike before it reaches the database (the real 100x)

A hundred thousand people arriving in a minute do not need a hundred times the write capacity. There are 12,000 seats. At most a few thousand holds can succeed, whatever happens. The job is to make the other 95% of attempts cheap, and to not let them all in at once.

This is what the waiting room is for, and it is the one piece that is already built for this scale:

- The queue lives entirely in Redis. Polling your position costs no database connection.
- Admission is a rate (buyers per second), so the database sees the load it was sized for no matter how many are waiting.
- Admission tokens are signed, so the API verifies one with no lookup.

At this scale the waiting room page and the tournament page move to a CDN, with only the position poll reaching the origin. A queue of 100,000 polling every 1.5 seconds is about 67,000 requests a second: that poll must be the cheapest thing the system does (one Redis read, already the case), and the interval should stretch with queue length, since someone 80,000th in line does not need second-by-second news.

With the spike shaped, the measured single-machine numbers (hundreds of holds per second) are already close to enough for one sale. Step 1 gives the headroom.

## Step 3. Take reads off the primary

Reads outnumber writes about three to one in a sale, and almost all are the same few responses.

- **Availability** is cached for a second with stampede protection. At 10,000 reads a second that is one database query a second per tournament. Already built.
- **Browse and search** move to read replicas. They can be seconds stale with no harm.
- **The claim itself always runs on the primary.** This is the rule that makes everything above safe: a stale read can show a seat as free when it is not, and the cost is one refused hold, never a double sale.

## Step 4. The hot rows

Reserved seats do not have this problem: each seat is its own row, so buyers of different seats never wait on each other.

Standing tickets and team slots are counters, one row per ticket type, and every buyer of that type updates it, one after another. That was not the limit in the benchmarks, but it is a queue with one server, so it will be. When it is, there are two options, in order of preference:

- **Split the counter** into N rows (say 16), each holding a share of the capacity. A buyer picks one at random and falls through to the others only when it is empty. Contention drops by N; "sold out" needs all N to agree, which the existing lazy-reclaim code path already resembles.
- **Hand out allocations from Redis** in blocks (each API instance takes, say, fifty at a time and returns what it does not sell), with Postgres still the record of what was sold.

The first keeps Postgres as the only source of truth, which is why it is preferred.

## Step 5. Partition by tournament

By now the tables that grow without bound (`tournament_seats`, `holds`, `tickets`, `checkin_scans`, `outbox`) are large, mostly because of tournaments that ended long ago.

Every contended query already filters by `tournament_id`. So these tables partition by it cleanly: a sale touches only its own partition's indexes, which stay small and in memory, and a finished tournament's partitions are detached and archived instead of deleted row by row. The outbox partitions by time instead, since it is pruned by age.

## Step 6. Shard by tournament

One Postgres primary on good hardware does tens of thousands of short transactions a second. A single sale will not exhaust that after Step 2. A platform running hundreds of sales at the same moment eventually would.

The unit to split on is again the tournament. **A booking never spans two tournaments**, so nothing needs a transaction across shards:

- A small directory maps tournament to shard. It is read on every request and almost never changes, so it is cached hard.
- Inventory, holds, orders, tickets, teams, and matches for a tournament live together on its shard.
- Users stay in one place, on their own database.
- "My orders" spans shards. It becomes a read model: a table keyed by user, fed by the outbox events that already exist (`order.paid`, `order.refunded`). It can lag by a second, and the order page itself still reads the shard.
- A flagship sale can be given a shard to itself for the day.

The cost is real: migrations run per shard, cross-tournament reporting needs a warehouse, and operations multiply. It is the last step for that reason.

## What else moves

- **Redis** splits by job before it splits by size: one for the cache (can lose data), one for streams and rate limits (persistent, never evicts). They have opposite needs, and the demo's single instance is a compromise.
- **The event stream** would move to Kafka or a managed equivalent once events per second or the number of consumers outgrows one Redis. The outbox does not change: only the relay's destination does.
- **Search** moves to a dedicated engine fed by the outbox when Postgres full-text search stops being enough: typo tolerance, facets, ranking by popularity.
- **Live updates** already fan out through pub/sub to any number of API instances. Past tens of thousands of viewers per bracket, the WebSocket tier becomes its own service so that connection count and request load scale apart.
- **Regions.** Reads and the waiting room can be served near the user. Writes for a tournament stay in one region, near its venue: a seat cannot be sold in two places at once, and no cross-region consensus protocol is worth it for a ticket.

## What does not change

At every step, the same three things hold the system together, and none of them depends on scale:

1. **The claim is decided in Postgres, in one transaction, on the primary.** Caches, replicas, queues, and CDNs only decide who gets to try.
2. **Constraints, not code, forbid the impossible states.** A counter cannot go over capacity, a seat cannot have two valid tickets, a hold cannot become two orders. These survive any bug in the layers above.
3. **Everything that crosses a process boundary is at least once and idempotent.** Outbox, webhooks, scans, retries. Adding instances, shards, or regions adds more ways for a message to arrive twice, and none for it to matter.

And reconciliation keeps checking that all of this is true, on every shard, every five minutes.
