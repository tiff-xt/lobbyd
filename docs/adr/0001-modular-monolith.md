# 0001. Build a modular monolith, not microservices

**Status:** Accepted

## Context

The system has clear sub-domains (catalog, inventory, orders, payments, teams, tickets, brackets). The core flow (claim inventory, create order, issue tickets) needs several of them to change together, atomically.

## Options

1. **Microservices**, one per sub-domain, each with its own database.
2. **A single unstructured application.**
3. **A modular monolith:** one deployable, one database, enforced boundaries between modules.

## Decision

Option 3.

The most important property of this system is that a sale is all or nothing. With one database that is a local transaction. Split across services it becomes a distributed saga with compensation logic, which adds failure modes exactly where correctness matters most, to solve an organisational scaling problem this project does not have.

Boundaries are kept honest by rules, checked in CI with import-linter:

- Each module owns its tables. No module queries another module's tables directly.
- Modules talk through a small service interface, or through events on the outbox.
- A module's internals are private.

## Consequences

- Transactions across modules are simple and safe.
- One thing to deploy, run locally, and trace.
- All modules scale together. If one part ever needs independent scaling, the boundaries above are the seams to cut along, and the outbox already carries the events a separate service would consume.
- The payment simulator is deliberately a separate service, because it stands in for a third party and must be reached over the network like one.
