# Roadmap

Each phase ends with something that runs and is tested. A phase is done when its "done when" list is true, CI is green, and the relevant document is updated.

| Phase | Theme | Status |
| --- | --- | --- |
| 0 | Design | In review |
| 1 | Foundations | Not started |
| 2 | Catalog | Not started |
| 3 | Booking core | Not started |
| 4 | Payments and reliability | Not started |
| 5 | Performance under load | Not started |
| 6 | Teams and waitlist | Not started |
| 7 | Check-in and live brackets | Not started |
| 8 | Observability and deployment | Not started |
| 9 | Demo client | Not started |

## Phase 0. Design

Design document, data model, decision records, this roadmap.

## Phase 1. Foundations

Project skeleton with uv, FastAPI app factory, settings, async SQLAlchemy and Alembic, Docker Compose (API, Postgres, Redis), module layout with import rules, structured logging with request ids, error format, health endpoints, authentication (signup, login, access and refresh tokens, roles), CI (ruff, type checking, tests against real Postgres and Redis).

**Done when:** `docker compose up` gives a running API with docs; a new user can sign up and log in; CI runs lint, types, and integration tests on every push.

## Phase 2. Catalog

Venues, sections, seats (bulk seat map import), tournaments, ticket types, publish workflow, full-text search with filters, cursor pagination, seed script that generates a realistic dataset (several venues, dozens of tournaments, one 12,000 seat arena).

**Done when:** an organizer can create and publish a tournament through the API; search and browse return correct pages on the seeded data; the query plans for browse and search are checked and recorded.

## Phase 3. Booking core

Holds for reserved seats and counted inventory ([ADR 0002](adr/0002-claiming-inventory.md)), idempotency keys ([ADR 0003](adr/0003-idempotency-keys.md)), hold expiry sweeper, orders with a stubbed instant payment, ticket issuing, availability endpoint.

**Done when:** concurrency tests fire hundreds of simultaneous claims at overlapping seats and every invariant in the data model document holds on every run; retried requests return the original response; expired holds return to sale without the sweeper running.

## Phase 4. Payments and reliability

Payment simulator service with signed webhooks and fault injection (delay, duplicate, reorder, fail), webhook handling, order state machine including late-payment refunds, transactional outbox and relay ([ADR 0004](adr/0004-transactional-outbox.md)), arq workers, idempotent consumers, dead-letter handling, confirmation email (logged, not sent), reconciliation job that checks cross-table invariants.

**Done when:** a test suite drives the simulator through every fault and the system ends in a correct state each time; killing the relay or a worker mid-run loses nothing.

## Phase 5. Performance under load

Caching for catalog and availability with stampede protection, rate limiting, waiting room ([ADR 0005](adr/0005-waiting-room-and-rate-limits.md)), Locust scenarios for the reference sale, alternative claim strategies behind one interface, query tuning with `EXPLAIN ANALYZE`, connection pool sizing.

**Done when:** `docs/benchmarks.md` exists with the hardware, method, and measured results for each claim strategy and for the sale scenario with and without the waiting room, including what was slow at first and what fixed it; ADR 0002 is updated with the outcome; a small load test runs in CI as a regression guard.

## Phase 6. Teams and waitlist

Team registration as an atomic claim, roster rules, withdrawal, waitlist with offers that expire, promotion worker.

**Done when:** concurrent registrations for the last slot produce exactly one team; a freed slot is offered to the next waiting captain and moves on if they do not take it.

## Phase 7. Check-in and live brackets

Signed ticket tokens, online scanning, manifest download and offline sync ([ADR 0006](adr/0006-check-in-tokens-and-offline-scanning.md)), single-elimination bracket generation, result reporting, WebSocket updates through Redis pub/sub across multiple API instances.

**Done when:** racing scans of one ticket admit once; an offline batch syncs correctly and duplicates are flagged; a result entered on one API instance appears on clients connected to another.

## Phase 8. Observability and deployment

Prometheus metrics, OpenTelemetry tracing, Grafana dashboard in Compose, alerts for the key metrics, production Dockerfile, hosted demo on free tiers, "scaling to 100x" write-up expanded from the design document.

**Done when:** the dashboard shows a load test as it runs; the hosted demo is reachable and reseeds itself; the README links to it.

## Phase 9. Demo client

A small web client so the backend can be seen working: browse tournaments, pick seats on a live map, sit in the waiting room, check out against the simulator, watch a bracket update. Kept deliberately thin; the backend is the project.

**Done when:** a visitor can go from landing page to a ticket with a QR code in under a minute.
