# 0007. One repository, managed with Nx

**Status:** Accepted

## Context

The project has several parts in two languages: the API and workers (Python), the payment simulator (Python), the demo client (TypeScript), and API types that the client generates from the backend's OpenAPI document. They change together, and the client must never drift from the API.

## Decision

A single repository organised as an Nx workspace.

```
apps/
  api/        FastAPI application and workers        (Python)
  paysim/     payment simulator                      (Python)
  web/        demo client                            (Next.js)
libs/
  api-client/ TypeScript types generated from the API's OpenAPI document
docs/
```

- **Nx is the task runner, not the build system for Python.** Each Python project keeps a normal `pyproject.toml` and is managed by a uv workspace with one lockfile. Its `project.json` defines targets (`lint`, `typecheck`, `test`, `serve`) that call `uv run ...`. Nothing about the Python code depends on Nx, so `uv run pytest` works without Node installed.
- **The dependency graph is declared.** `api-client` depends on `api` (it is generated from the API's OpenAPI output), and `web` depends on `api-client`. Changing an API route therefore regenerates the types and re-checks the client.
- **CI runs `nx affected`,** so a change to the client does not rerun the Python integration tests, and the reverse.
- **Caching is local and in the GitHub Actions cache.** Nx Cloud is not used.

## Options not taken

- **Separate repositories.** The API and its generated client types would need publishing and version juggling for no benefit at this size.
- **A plain repository with a Makefile.** Works, but has no notion of what a change affects, so CI runs everything every time.
- **A community Nx plugin for Python.** Adds generators and inferred targets, at the price of depending on a third-party plugin keeping pace with Nx and uv. Plain `uv run` targets are a few lines per project and have nothing to break. Can be revisited.
- **Pants or Bazel.** Stronger Python support, far more setup, and weaker for the Next.js side.

## Consequences

- Node is required for the full workspace experience even though most of the code is Python. Python-only work remains possible with uv alone.
- Nx understands the TypeScript import graph automatically but not Python imports, so dependencies between Python projects are declared by hand in `project.json`. With two Python apps this is trivial.
- One place for CI, formatting, and commit history.
