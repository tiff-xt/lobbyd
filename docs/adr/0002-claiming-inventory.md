# 0002. Claim inventory with conditional writes in Postgres

**Status:** Accepted, implemented (phase 3), and confirmed by benchmark (phase 5)

## Context

Many users try to claim the same seats at the same instant. Exactly one may win each seat, a multi-seat claim must be all or nothing, and losers must fail quickly. A claim is temporary (10 minutes) and must free itself if the user walks away.

## Options

**A. Pessimistic row locks.** `SELECT ... FOR UPDATE` on the seat rows, check they are free, then update.

**B. Optimistic versioning.** Read the rows with a version number, write back with `WHERE version = :seen`, retry on conflict.

**C. Redis as the lock.** `SET seat:{id} {hold} NX EX 600`, and write to Postgres only at purchase.

**D. Single conditional write.** One `UPDATE` whose `WHERE` clause states the precondition, and a check that it changed the expected number of rows.

## Decision

Option D for both inventory shapes, with Postgres as the only authority.

**Reserved seats:**

```sql
-- Inside one transaction. :seats is sorted, see "Lock order" below.
UPDATE tournament_seats
   SET status = 'held', hold_id = :hold, held_until = now() + interval '10 minutes'
 WHERE tournament_id = :t
   AND seat_id = ANY(:seats)
   AND (status = 'available' OR (status = 'held' AND held_until < now()));
-- If rows updated <> number of seats requested: ROLLBACK and return 409.
```

**Counted inventory (team slots, general admission):**

```sql
UPDATE inventory_counters
   SET held_count = held_count + :n
 WHERE ticket_type_id = :type
   AND held_count + sold_count + :n <= capacity;
-- If no row updated: sold out, return 409.
```

### Why this option

- **The check and the write are one statement,** so there is no window between "looks free" and "claimed".
- **Expiry is part of the precondition.** A seat whose hold has lapsed is claimable the moment it lapses, with no dependency on a cleanup job. If the sweeper stops, seats do not get stuck.
- **One source of truth.** With option C, Redis and Postgres can disagree after a failover or an eviction, and the disagreement shows up as a double sale. Keeping the claim in the same database, and the same transaction, as the hold and order records removes that class of bug.
- **The schema backs it up.** `no_oversell` and the unique index on valid tickets per seat mean a bug in this logic produces an error, not an oversold event. This was verified: with the capacity condition deleted from the claim, the test suite fails with database errors and nothing is oversold.

### Counters cannot see the clock

A seat row carries its own deadline, so a lapsed seat frees itself. A counter is just a number: the places held by a lapsed hold stay counted until something subtracts them. Waiting for the sweeper would make buyers depend on a background job.

So the claim does it on demand. If a counter looks too full for the request, the claim first closes that tournament's lapsed holds (`FOR UPDATE SKIP LOCKED`, so it never waits on a hold someone is checking out), returns their places, and then claims. Counted inventory is therefore also available as soon as a hold lapses, sweeper or no sweeper.

### Lock order

Deadlocks happen when two transactions each hold a lock the other wants. They cannot happen if everyone takes locks in the same order, so every code path in the inventory module follows one:

1. the caller's own hold row
2. other people's lapsed holds, with `SKIP LOCKED` (never waited for)
3. counter rows, ascending by ticket type id
4. seat rows, ascending by seat id

Two details matter:

- **Counters before seats.** Reclaiming lapsed holds touches counters, and it has to happen before the seat claim, so counters come first everywhere.
- **All of a tournament's counters at once.** When a claim is going to close other holds, it does not yet know which counters that will touch. It locks every counter of the tournament, in order, before touching any. Everything it needs afterwards it already has.

Tidying that is optional (the sweeper resetting lapsed seat rows) uses `SKIP LOCKED`. It never waits, so it cannot be part of a deadlock.

This was verified too. With seats locked one at a time in the order the buyer listed them, the concurrency tests fail and Postgres logs dozens of detected deadlocks. With the order above: none, across repeated runs.

### Completing a purchase

Payment success uses the same pattern:

```sql
UPDATE tournament_seats
   SET status = 'sold', hold_id = NULL, held_until = NULL
 WHERE tournament_id = :t AND hold_id = :hold AND status = 'held';
-- If rows updated <> seats in the hold: the hold lapsed and someone else
-- claimed a seat. ROLLBACK and refuse the checkout. (From phase 4, with real
-- payments: mark the order expired and refund.)
```

The seat row still pointing at this hold is the proof the buyer still owns it. No clock comparison is needed at this step, which also means a buyer whose hold lapsed a moment ago still gets their seats if nobody else took them.

### Why not the others

- **A** is correct, and D uses the same row locks underneath. As the default it encourages read-then-decide logic in the application; D keeps the rule in one statement.
- **B** suits low contention, where conflicts are rare. Here conflicts are the normal case, and retry loops multiply load at the worst moment. It is used for match results (see the data model), where it fits.
- **C** is the fastest per operation and the weakest in guarantees, for the reason above.

## Consequences

- Losers still take a brief row lock before they fail. Under extreme contention on a handful of rows that is a queue. The waiting room ([0005](0005-waiting-room-and-rate-limits.md)) bounds how deep it can get, and showing only available seats steers users apart.
- The counter row for a ticket type is a single hot row, and because counters are locked first, a basket that includes counted items queues behind it before reaching its seats. If the benchmark shows this is the bottleneck, the alternative is one row per place claimed with `FOR UPDATE SKIP LOCKED`, which spreads contention across rows.
- Seat rows of a lapsed hold keep saying `held` until someone claims them or the sweeper resets them. Every reader therefore has to compare `held_until` with the clock, never just read `status`. One shared expression in the code does this, used for both availability and the claim.
- Availability reads can be slightly stale (cache, replica). That is safe: a stale read can cause a failed claim, never a wrong sale.
- All time comparisons use the database's clock, never an application server's.

## Validation

Done in phase 5. Four strategies sit behind one setting (`claim_strategy`): the one described here, a bare conditional update with no locks first, a `SKIP LOCKED` fail-fast variant, and a Redis lock in front of the claim. A parametrised test holds all four to the same correctness bar, and each was run under the same contended load.

Result ([benchmarks, section 4](../benchmarks.md#4-claim-strategies-compared)): on the test machine they are indistinguishable, because the API's CPU is the bottleneck and Postgres is nearly idle. No strategy oversold, none deadlocked under load. The decision stands on its original grounds. Two details from the exercise:

- The bare update avoided deadlocks only because Postgres happens to lock a multi-row update's rows in index order. Locking in the buyer's order, deliberately, in the test suite produced dozens. The chosen strategy's explicit order does not depend on that accident.
- `SKIP LOCKED` is the variant worth measuring again on hardware where lock waits, not Python, dominate.
