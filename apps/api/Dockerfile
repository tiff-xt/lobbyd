# Build context is the repository root (the uv workspace lives there):
#   docker build -f apps/api/Dockerfile .

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /workspace

# Dependencies first: this layer is reused until a lockfile changes.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace

COPY apps/api apps/api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home lobbyd
COPY --from=build /workspace/.venv /workspace/.venv
# Migrations run from the image too (see docker-compose.yml).
COPY apps/api/alembic.ini /workspace/apps/api/alembic.ini
COPY apps/api/migrations /workspace/apps/api/migrations
ENV PATH="/workspace/.venv/bin:$PATH" PYTHONUNBUFFERED=1 LOBBYD_LOG_JSON=true
WORKDIR /workspace/apps/api
USER lobbyd
EXPOSE 8000
# The app logs one structured line per request, so uvicorn's own access log is off.
CMD ["uvicorn", "lobbyd.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
