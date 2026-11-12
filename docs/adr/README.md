# Decision records

One short record per significant technical choice: the context, the options considered, the decision, and what it costs. Records are numbered and never rewritten; if a decision changes, a new record supersedes the old one.

| No. | Decision | Status |
| --- | --- | --- |
| [0001](0001-modular-monolith.md) | Build a modular monolith, not microservices | Accepted |
| [0002](0002-claiming-inventory.md) | Claim inventory with conditional writes in Postgres | Accepted, validated by benchmark |
| [0003](0003-idempotency-keys.md) | Idempotency keys stored in the same transaction as the effect | Accepted |
| [0004](0004-transactional-outbox.md) | Transactional outbox for events | Accepted |
| [0005](0005-waiting-room-and-rate-limits.md) | Waiting room and rate limits in Redis | Accepted |
| [0006](0006-check-in-tokens-and-offline-scanning.md) | Signed ticket tokens and offline scanning | Accepted |
| [0007](0007-nx-monorepo.md) | One repository, managed with Nx | Accepted |
| [0008](0008-caching.md) | Three layers of caching, each for a different kind of fact | Accepted |

"Accepted" means it is the plan. Records that make performance claims will be updated with measurements when the benchmarks exist.
