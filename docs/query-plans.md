# Query plans

Every query on a hot path is checked with `EXPLAIN (ANALYZE, BUFFERS)` against a dataset large enough for the planner's choice to matter. This page records what was found. It is updated when a query or an index changes.

**Dataset:** `lobbyd-seed --reset --tournaments 50000` (50,000 tournaments starting between 30 days ago and a year ahead, 90% published; 14,700 seats in 3 venues), Postgres 16, after `VACUUM ANALYZE`.

"Buffers" is the number of 8 kB pages the query touched. It is the fairest single measure of work, because unlike time it does not depend on the machine or on what happens to be cached.

## Summary

| Query | Plan | Pages touched |
| --- | --- | --- |
| Browse, first page | Index scan on `tournaments_browse_idx` | 1,381 (was 3,180, and growing without limit; see below) |
| Browse, a page 35,000 rows deep | Same index scan, entered at the cursor | 23 |
| The same page using `OFFSET` | Sequential scan and sort of 41,753 rows | 3,347 |
| Browse one game | Index scan on `tournaments_game_browse_idx` | 195 |
| Search, selective (`voidstrike masters osaka`) | Bitmap scan on the GIN index, then sort 20 rows | 37 |
| Search, broad (`tournament`, matches every row) | Index scan on `tournaments_browse_idx`, filter | 1,381 |
| Seat map, 12,000 seats | Index scan on `seats_venue_id_idx`, in-memory sort | 152 |

## A problem the plans exposed

The public listing hides tournaments that have ended, and sorts by start time. The first version of the query was:

```sql
WHERE status IN ('published', 'live') AND ends_at >= now()
ORDER BY starts_at, id LIMIT 21
```

```
Limit (actual rows=21)
  ->  Index Scan using tournaments_browse_idx on tournaments t (actual rows=21)
        Filter: (ends_at >= now())
        Rows Removed by Filter: 3142
        Buffers: shared hit=3180
```

The index is ordered by `starts_at`, and nothing in the query says where in that order to begin. So the scan started at the oldest tournament ever published and stepped over every finished one to reach the first that was still running: 3,142 rows read to return 21. The seed only contains one month of history. With a few years of it, the most requested page on the site would read the whole table, and get slower every day.

**The fix** is a business rule that turns into an index bound. A tournament may last at most 14 days (a `CHECK` constraint, also validated in the API). Therefore anything that has not ended must have started within the last 14 days, and the query can say so:

```sql
WHERE status IN ('published', 'live')
  AND starts_at >= now() - interval '14 days'   -- redundant logically; gives the scan a start
  AND ends_at >= now()
ORDER BY starts_at, id LIMIT 21
```

```
Limit (actual rows=21)
  ->  Index Scan using tournaments_browse_idx on tournaments t (actual rows=21)
        Index Cond: (starts_at >= (now() - '14 days'::interval))
        Filter: (ends_at >= now())
        Rows Removed by Filter: 1353
        Buffers: shared hit=1381
```

The work is now bounded by how many tournaments start in any 14 day window, and no longer grows with history. The 1,353 rows still stepped over are tournaments that started in the last two weeks and already finished. The seed is dense (about 110 tournaments start every day), so this is close to a worst case. It is still the most expensive page in the catalog and the first candidate for caching in the performance phase.

## Why cursors, not page numbers

Both of these return the same 21 rows, 35,000 rows into the listing:

```
-- Keyset: WHERE (starts_at, id) > (:last_starts_at, :last_id)
Index Scan using tournaments_browse_idx
  Index Cond: (ROW(starts_at, id) > ROW(...))
  Buffers: shared hit=23

-- OFFSET 35000
Sort (actual rows=35021)
  ->  Seq Scan on tournaments t (actual rows=41753)
  Buffers: shared hit=3347
```

The cursor jumps straight into the index at the right place, so page 1,700 costs the same as page 2. `OFFSET` has to produce and discard every row before it. Cursors also stay correct when rows are added between requests (there is a test for exactly that), where `OFFSET` would repeat or skip a row.

## Search

Selective queries use the GIN index on the generated `tsvector` column and sort the handful of matches:

```
Sort (actual rows=20)
  ->  Bitmap Heap Scan on tournaments t (actual rows=20)
        Recheck Cond: (search @@ '''voidstrik'' & ''master'' & ''osaka'''::tsquery)
        ->  Bitmap Index Scan on tournaments_search_idx (actual rows=22)
  Buffers: shared hit=37
```

For a term that matches nearly everything, the planner switches by itself to walking the browse index in order and checking each row, which finds 21 matches almost at once. Both extremes are handled without hints.

**Known limits.** Results are ordered by date, not by relevance: ranking by relevance cannot be paginated with a stable cursor without materialising all matches. Matching is on whole words after stemming ("racing" finds "race"), so a prefix like "void" does not find "Voidstrike". Trigram matching for typeahead is a possible later addition.

## Partial indexes need literal predicates

The browse indexes only contain listed tournaments (`WHERE status IN ('published', 'live')`), which keeps drafts and cancelled events out of them. The planner will only use a partial index if it can prove the query's condition implies the index's. With the statuses sent as bound parameters it cannot always prove that, so the listing query writes the condition as literal SQL, identical to the index definition.

## Response times

Measured with `curl` against one local API process on the 50,000 tournament dataset. Median of three. These are single requests on a development machine, not a load test; the benchmarks phase covers behaviour under load.

| Request | Time | Size |
| --- | --- | --- |
| `GET /tournaments` | 10 ms | 9 kB |
| `GET /tournaments?limit=100` | 12 ms | 42 kB |
| `GET /tournaments?q=voidstrike masters osaka` | 9 ms | 7 kB |
| `GET /tournaments?game=hexbound` | 8 ms | 8 kB |
| `GET /games` | 70 ms | 0.3 kB |
| `GET /venues/{id}/seats` (12,000 seats) | 84 ms | 705 kB |

The two slow ones are slow for understood reasons, and both results change rarely, so both are marked for caching in phase 5 instead of more indexes now:

- `/games` counts every upcoming listed tournament per game on each request.
- The seat map is 12,000 rows and 705 kB of JSON. It never changes after the venue is created.
