# Benchmarks

What the system does under load, measured. This page reports the bad numbers alongside the good ones, and what was wrong at first.

## Read this first

**Everything ran on one machine**: 4 virtual CPUs, 16 GB of memory, holding Postgres 16, Redis 7, three API processes, one worker, the payment simulator, **and the load generator**. The API is written in Python, and each API process uses one CPU. So these numbers describe a small box sharing its CPUs with its own attacker. They show how the system behaves at and beyond its limit, and what each design decision is worth. They are not a statement of what the design can reach on real hardware.

- Postgres runs with its default configuration (128 MB of shared buffers).
- Rate limiting is off, because every simulated buyer shares one address.
- The payment simulator settles a payment in 0.3 seconds.
- Each run ends by checking [all cross-table invariants](data-model.md#invariants). **In every run on this page: zero invariants broken, zero seats oversold, zero deadlocks.**

To reproduce any row: `cd apps/api && uv run python bench/run.py <scenario>`. Raw results are in [`apps/api/bench/results`](../apps/api/bench/results).

## How a simulated buyer behaves

Like a person with the tournament page open ([`locustfile.py`](../apps/api/bench/locustfile.py)): load the tournament, the seat map and availability; queue if there is a waiting room; pick two neighbouring seats that look free and try to hold them, choosing others if refused (up to five tries); pay; ask for the order until the payment settles, backing off between asks; then stay on the page polling availability every few seconds, like an open browser tab.

A refused hold (409) is counted as a success: in an oversubscribed sale most buyers must lose, and telling them so quickly is the system working. Only server errors and timeouts are failures.

## 1. Within capacity

400 buyers arriving over 27 seconds, 2,000 seats. About 160 requests a second.

| Request | p50 | p95 | p99 |
| --- | --- | --- | --- |
| Availability (2,000 seats) | 3 ms | 13 ms | 28 ms |
| Seat map | 9 ms | 25 ms | 35 ms |
| Tournament detail | 16 ms | 35 ms | 93 ms |
| **Hold two seats** | **65 ms** | **120 ms** | **170 ms** |
| Start checkout | 72 ms | 150 ms | 180 ms |
| Page open to seats held | 120 ms | 330 ms | 390 ms |
| Page open to tickets in hand | 0.96 s | 1.7 s | 1.9 s |

No failures. All 400 buyers got tickets. Both latency targets from the [design](design.md) are met at this load: browsing under 100 ms at p95, holds under 250 ms at p95.

"Page open to tickets" includes the simulated provider's 0.3 seconds and the buyer's own polling interval.

## 2. Beyond capacity: 1,500 buyers in ten seconds

1,500 buyers arrive within ten seconds for 2,000 seats, then keep their pages open. This is several times what the machine can serve, on purpose.

| | First run | Final | Final, cache off | Final, waiting room |
| --- | --- | --- | --- | --- |
| Requests served per second | 410 | 543 | 371 | 658 |
| Failed requests | 34 (after waits of up to 30 s) | 969 (2.4%) | 5,808 (19.8%) | 221 (0.3%) |
| Hold, p50 | 5.0 s | 1.9 s | 1.4 s | **0.42 s** |
| Hold, p95 | 19 s | 5.4 s | 5.5 s | 2.8 s |
| Availability, p50 | 190 ms | 5 ms | 1,400 ms | 5 ms |
| Orders completed in the run | 118 | 712 | 574 | 829 |
| Unhandled server errors | 31 | 0 | 0 | 0 |
| Run length | 75 s | 75 s | 75 s | 110 s |

What this says:

- **Overloaded, it degrades instead of breaking.** In the final version an overloaded request is refused within five seconds with a `503` and `Retry-After`, and is safe to retry because bookings carry idempotency keys. In the first run the same requests hung for up to thirty seconds and some ended as unhandled errors.
- **Caching is worth a factor of five in failures here.** With the cache off, one request in five fails and availability takes over a second, because every open tab's poll runs the 2,000 row query.
- **The waiting room does what it is for.** Letting 20 buyers a second through to booking cuts the median hold from 1.9 seconds to 0.42 and failures from 2.4% to 0.3%, and more orders complete. The price is the wait itself: a median of 35 seconds in the queue, 60 at p95, for a 1,500 person rush. That wait is visible, fair, and has a position counter, which is the point: the alternative is the same delay as unexplained errors.
- **Even so, hold p95 is 2.8 seconds with the waiting room.** The machine is still saturated, now by reads: 1,500 clients polling their queue position and availability is about 530 requests a second on its own. The waiting room protects the write path. It does not create CPU. On this box the load generator and the API are also competing for the same four cores.

### Admission rate

The first waiting-room run admitted 40 buyers a second and helped much less (median hold 0.86 s, 644 failures): 40 holds a second is more than this machine serves comfortably alongside everything else. The "within capacity" run above shows it handling 7 a second with ease; 20 a second was chosen as a rate with headroom, and the table shows the result. The rate is a setting (`waiting_room_admit_per_second`) and should be set from a measurement like this one on the real hardware.

## 3. What was slow at first, and what fixed it

In the order they were found. Each was found by a test or a load run, not by inspection.

**1. The Redis connection pool ran out.** A test firing 200 simultaneous requests at one endpoint got `Too many connections`. The default pool raises the moment it is exhausted. Switched to a blocking pool, where a request waits its turn.

**2. The API was CPU-bound; the database was idle.** During the first run, `top` showed three API processes at 100% and Postgres at 10%, with 46 database connections "idle in transaction", waiting for Python. The cost was not slow queries but many small ones. Measured unloaded, one request at a time:

| Request | Before | After |
| --- | --- | --- |
| Hold two seats | 23.7 ms | 18.4 ms |
| Start checkout | 30.6 ms | 25.1 ms |
| Read an order | 12.5 ms | 8.8 ms |

The savings came from remembering immutable facts in process (seat labels, ticket type names), remembering a tournament's sale window and the signed-in user for a few seconds, building the hold response from memory instead of reading it back, and replacing read-then-insert with insert-and-let-the-index-decide. See [ADR 0008](adr/0008-caching.md).

**3. Polling created a feedback loop.** Buyers waiting for payment asked for their order twice a second. Each ask cost five queries. That load slowed payments down, so more buyers were waiting and asking. In the first run 853 buyers held seats and 118 got tickets. Fixed from both ends: an order is now cached until it changes, and clients back off between asks. After that fix alone: 673 orders completed in the same 75 seconds.

**4. An exhausted connection pool was a 30 second wait and a `500`.** Now a request waits at most five seconds for a connection and is then told the service is busy, with `Retry-After`.

**5. The order cache had a race.** A load run ended with 862 orders paid in the database and 627 buyers still being told "pending". A read that began before a payment committed could finish after the cached entry was dropped, and write the old "pending" copy back for thirty seconds. Now a pending order is cached for just over a second, whatever the path, and a settled one for thirty. There is a test that reproduces the race.

**6. The payment simulator was the bottleneck.** After fix 5 buyers still waited: orders took 66 seconds to be marked paid. The simulator delivered webhooks one at a time, waiting for each response, and had built a minute-long backlog. A real provider sends them concurrently, and now so does the simulator. In the waiting-room run that followed, the median time from order to paid was about ten seconds, on a machine that was still saturated.

**7. The load script had two mistakes of its own.** A buyer whose queue request was refused as busy carried on without a turn, which showed up as hundreds of bogus refusals. And buyers always asked for seats in ascending order, which meant lock ordering was never exercised. Both fixed before the numbers on this page were taken.

## 4. Claim strategies compared

[ADR 0002](adr/0002-claiming-inventory.md) chose to claim seats by locking the rows in a fixed order and then making one conditional update, and promised to measure the alternatives. All four are implemented behind one setting and held to the same correctness tests.

500 buyers at once, 250 seats, each wanting two to four neighbouring seats, clicked in any order. Thirty seconds.

| Strategy | Hold p50 | Hold p95 | Failed | Deadlocks | Seats sold |
| --- | --- | --- | --- | --- | --- |
| **Lock in order, then claim** (in use) | 650 ms | 3.3 s | 7 | 0 | 224 |
| Claim only, no locks first | 930 ms | 3.2 s | 2 | 0 | 227 |
| Lock with `SKIP LOCKED`, fail fast | 680 ms | 3.3 s | 0 | 0 | 232 |
| Redis lock in front, then claim | 810 ms | 3.1 s | 2 | 0 | 217 |

**The honest reading: on this machine they cannot be told apart.** The differences are within run-to-run noise, because the time goes to Python and to waiting for a CPU, not to row locks. Postgres was never the bottleneck in any run.

Two things are still worth recording:

- **"Claim only" did not deadlock, but not by design.** Postgres happens to lock the rows of a multi-row update in index order, whatever order the request listed them in. That is planner behaviour, not a guarantee. When seats were locked one at a time in the buyer's order (in the test suite, deliberately), Postgres logged dozens of deadlocks. The strategy in use takes its locks in an explicit order, so it does not depend on the accident.
- **The Redis front door bought nothing measurable** and adds a second system to the claim path.

So the decision stands, on the grounds it was made on (correctness that does not rely on luck, and a refusal that can name the seats that went), with the benchmark confirming that the safer choice costs nothing measurable. On hardware where the API is no longer the bottleneck, `SKIP LOCKED` is the variant to measure again.

## 5. What this suggests doing next

In rough order of payoff:

1. **Push, don't poll.** Queue position, availability, and order status are all polled. Server-sent updates (planned with live brackets, phase 7) would remove most of the read load that saturates the machine in section 2.
2. **Give the API CPU of its own.** Separate machines for the API, the database, and above all the load generator. Three API processes on four shared cores is the ceiling here.
3. **Spend less Python per request.** A hold is still 18 ms of mostly framework and ORM overhead across a dozen round trips. A hand-written query path for the two hottest endpoints, or a single stored procedure for the claim, would cut it severalfold.
4. **Adaptive admission.** Let the waiting room's rate follow measured hold latency instead of being a fixed number.
5. **A connection pooler** in front of Postgres once there are more API processes than connections to spare.

## 6. In CI

A fifteen second run with 60 buyers (`bench/run.py smoke`) runs on every push. It fails the build on any server error or broken invariant. It is a check that nothing is badly wrong under concurrency, not a performance measurement: shared CI machines are too noisy for that.
