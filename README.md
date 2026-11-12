# lobbyd

Ticketing and registration backend for gaming tournaments and LAN events. FastAPI, Postgres, Redis.

Teams register for a limited number of competitor slots, spectators pick seats from a venue map, everyone pays, and tickets are scanned at the door. Popular tournaments sell out in seconds, so the interesting part is staying correct and fast when thousands of people want the same seats at once.

> **Status: phase 2 of 9 complete.** Accounts, venues with seat maps, tournaments, ticket types, search, and browsing work. Booking (holds, orders, tickets) is phase 3. The [roadmap](docs/roadmap.md) tracks progress.

## Why this project exists

"Design a ticketing system" is a classic system design question because a small feature list hides hard problems: overselling, double charging, lost events, cache stampedes, traffic spikes. lobbyd is a working answer to that question, with the trade-offs written down and the claims backed by load tests.

## Documents

| Document | What it covers |
| --- | --- |
| [Design](docs/design.md) | Requirements, core flows, capacity targets, architecture |
| [Data model](docs/data-model.md) | Postgres schema, constraints, indexes, and the reason for each |
| [Decision records](docs/adr/README.md) | One short record per significant technical choice |
| [Roadmap](docs/roadmap.md) | Build phases and what "done" means for each |
| [Query plans](docs/query-plans.md) | What `EXPLAIN ANALYZE` showed for each hot query, and what was fixed |
| [Errors](docs/errors.md) | Error format and every error code |

## Stack

| Area | Tools |
| --- | --- |
| Backend | Python, FastAPI, SQLAlchemy 2 (async) with asyncpg, Alembic, arq workers |
| Data | Postgres, Redis |
| Demo client | Next.js, React, TypeScript, Tailwind CSS, TanStack Query, types generated from the API's OpenAPI document |
| Testing | pytest against a real Postgres and Redis, Locust for load tests |
| Workspace | Nx monorepo, uv for Python, Docker Compose, GitHub Actions |

**Free tooling only.** Everything here is open source and runs locally with Docker Compose at no cost. The hosted demo uses free tiers only, and nothing in the project requires a paid plan or a credit card.

## Running it

**Everything in Docker** (needs only Docker):

```bash
docker compose up --build
```

The API is then at http://localhost:8000, with interactive docs at http://localhost:8000/docs.

To fill it with demo data (three venues including a 12,000 seat arena, 60 tournaments, and an organizer login that it prints):

```bash
docker compose run --rm api lobbyd-seed
```

**For development** (needs Node 22 and [uv](https://docs.astral.sh/uv/)):

```bash
npm install                             # Nx
uv sync                                 # Python dependencies for the whole workspace
docker compose up -d postgres redis     # just the data stores
npx nx migrate api                      # apply database migrations
npx nx serve api                        # API with reload on http://localhost:8000
```

| Command | What it does |
| --- | --- |
| `npx nx test api` | Tests, against the real Postgres and Redis from Compose |
| `npx nx lint api` | Ruff lint and format check (`npx nx format api` to fix) |
| `npx nx typecheck api` | mypy in strict mode |
| `npx nx boundaries api` | Checks module boundaries with import-linter |
| `uv run lobbyd-seed` | Demo data. `--tournaments 50000` for a dataset big enough to study query plans; `--reset` wipes first |
| `uv run lobbyd-admin set-role <email> organizer` | Change an account's role |
| `npm run check` | All of the above for every project |

Nx is only the task runner. Each target is a plain `uv run ...` command, so `cd apps/api && uv run pytest` works without Node.

## Layout

```
apps/api/            FastAPI application
  src/lobbyd/
    platform/        shared plumbing: database, cache, errors, logging, ids
    identity/        accounts, login, tokens, roles
    catalog/         venues, seat maps, tournaments, ticket types, search
  migrations/        Alembic migrations (the schema's source of truth)
  tests/
docs/                design, data model, decision records, roadmap
ops/                 local infrastructure config
```

## License

MIT
