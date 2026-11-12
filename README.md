# lobbyd

Ticketing and registration backend for gaming tournaments and LAN events. FastAPI, Postgres, Redis.

Teams register for a limited number of competitor slots, spectators pick seats from a venue map, everyone pays, and tickets are scanned at the door. Popular tournaments sell out in seconds, so the interesting part is staying correct and fast when thousands of people want the same seats at once.

> **Status: design phase.** The documents below describe what is going to be built and why. No application code exists yet. The [roadmap](docs/roadmap.md) tracks progress.

## Why this project exists

"Design a ticketing system" is a classic system design question because a small feature list hides hard problems: overselling, double charging, lost events, cache stampedes, traffic spikes. lobbyd is a working answer to that question, with the trade-offs written down and the claims backed by load tests.

## Documents

| Document | What it covers |
| --- | --- |
| [Design](docs/design.md) | Requirements, core flows, capacity targets, architecture |
| [Data model](docs/data-model.md) | Postgres schema, constraints, indexes, and the reason for each |
| [Decision records](docs/adr/README.md) | One short record per significant technical choice |
| [Roadmap](docs/roadmap.md) | Build phases and what "done" means for each |

## Planned stack

| Area | Tools |
| --- | --- |
| Backend | Python, FastAPI, SQLAlchemy 2 (async) with asyncpg, Alembic, arq workers |
| Data | Postgres, Redis |
| Demo client | Next.js, React, TypeScript, Tailwind CSS, TanStack Query, types generated from the API's OpenAPI document |
| Testing | pytest against a real Postgres and Redis, Locust for load tests |
| Workspace | Nx monorepo, uv for Python, Docker Compose, GitHub Actions |

**Free tooling only.** Everything here is open source and runs locally with Docker Compose at no cost. The hosted demo uses free tiers only, and nothing in the project requires a paid plan or a credit card.

## License

MIT
