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

Python 3.13, FastAPI, SQLAlchemy 2 (async) with asyncpg, Alembic, Postgres, Redis, arq workers, pytest with a real Postgres via testcontainers, Locust for load tests, Docker Compose, GitHub Actions.

## License

MIT
