# 0002. Claim inventory with conditional writes in Postgres

**Status:** Accepted, to be validated by benchmark (phase 5)

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
UPDATE ticket_types
   SET held_count = held_count + :n
 WHERE id = :type
   AND held_count + sold_count + :n <= capacity;
-- If no row updated: sold out, return 409.
```

### Why this option

- **The check and the write are one statement,** so there is no window between "looks free" and "claimed".
- **Expiry is part of the precondition.** An expired hold is claimable the moment it expires, with no dependency on a cleanup job. The sweeper only tidies up (marks the hold expired, corrects counters, triggers waitlist offers). If the sweeper stops, seats do not get stuck. Counted inventory is the exception: its counters are only released by the sweeper, so sweeper lag there delays availability (it can never cause overselling). That lag is a metric with an alert.
- **One source of truth.** With option C, Redis and Postgres can disagree after a failover or an eviction, and the disagreement shows up as a double sale. Keeping the claim in the same database, and the same transaction, as the hold and order records removes that class of bug.
- **The schema backs it up.** `no_oversell` and the unique index on valid tickets per seat mean a bug in this logic produces an error, not an oversold event.

### Lock order

A multi-row update locks rows as it finds them, so two overlapping claims can deadlock. Claims therefore lock their seats in ascending `seat_id` order first (`SELECT ... ORDER BY seat_id FOR UPDATE`) before the update. Postgres would detect and break a deadlock anyway, but a fixed order avoids paying the detection timeout during a spike.

A variant to measure in the benchmark: take those locks with `SKIP LOCKED` and fail at once if any seat is missing from the result. A seat locked by another in-flight claim is almost certainly about to be taken, so the loser gets its answer without queueing. The cost is an occasional false "taken" when the other claim rolls back.

### Completing a purchase

Payment success uses the same pattern:

```sql
UPDATE tournament_seats
   SET status = 'sold', hold_id = NULL, held_until = NULL
 WHERE tournament_id = :t AND hold_id = :hold AND status = 'held';
-- If rows updated <> seats in the hold: the hold lapsed and someone else
-- claimed a seat. ROLLBACK, mark the order expired, refund the payment.
```

The seat row still pointing at this hold is the proof the buyer still owns it. No clock comparison is needed at this step, which also means a buyer whose hold lapsed a moment ago still gets their seats if nobody else took them.

### Why not the others

- **A** is correct, and D uses the same row locks underneath. As the default it encourages read-then-decide logic in the application; D keeps the rule in one statement.
- **B** suits low contention, where conflicts are rare. Here conflicts are the normal case, and retry loops multiply load at the worst moment. It is used for match results (see the data model), where it fits.
- **C** is the fastest per operation and the weakest in guarantees, for the reason above.

## Consequences

- Losers still take a brief row lock before they fail. Under extreme contention on a handful of rows that is a queue. The waiting room ([0005](0005-waiting-room-and-rate-limits.md)) bounds how deep it can get, and showing only available seats steers users apart.
- The counter row for a ticket type is a single hot row. That is acceptable for hundreds of updates per second. If the benchmark shows it is the bottleneck, the alternative is one row per slot claimed with `FOR UPDATE SKIP LOCKED`, which spreads contention across rows.
- Availability reads can be slightly stale (cache, replica). That is safe: a stale read can cause a failed claim, never a wrong sale.

## Validation

Phase 5 implements options A, B, and D behind one interface (and C as an experiment) and runs the same contended load against each: N users, far fewer seats, random overlap. The benchmark records throughput, latency percentiles, wasted work, and invariant violations for each. This record will be updated with the results, and changed if the results say so.
