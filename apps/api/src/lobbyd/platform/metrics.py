"""Prometheus metrics: what the system is doing, as numbers over time.

Everything here answers a question someone on call would ask. Is it up and
how slow is it (requests)? Is background work keeping up (outbox lag,
events)? Is it turning people away, and why (problem codes)? Is the data
still right (invariant violations)?

Labels are kept to small, fixed sets. A route label is the route's template
(`/orders/{order_id}`), never the path, so one metric cannot grow a series
per order.
"""

import os

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
    start_http_server,
)

# Wide enough for a 5 ms cache hit and a 5 s pool timeout to both land in a real bucket.
_LATENCY = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

HTTP_REQUESTS = Counter(
    "lobbyd_http_requests_total", "Requests handled.", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "lobbyd_http_request_duration_seconds",
    "Time to handle a request.",
    ["method", "route"],
    buckets=_LATENCY,
)
# "livesum": with several uvicorn workers, add up the ones still running.
HTTP_IN_FLIGHT = Gauge(
    "lobbyd_http_requests_in_flight",
    "Requests being handled right now.",
    multiprocess_mode="livesum",
)
PROBLEMS = Counter(
    "lobbyd_problem_responses_total",
    "Error responses by code: sold_out, seat_taken, rate_limited, overloaded and so on.",
    ["code"],
)

CACHE_LOOKUPS = Counter(
    "lobbyd_cache_lookups_total", "Read-through cache lookups.", ["result"]
)  # hit, stale, miss, error
LIVE_CLIENTS = Gauge(
    "lobbyd_live_clients", "WebSocket clients connected.", multiprocess_mode="livesum"
)
SCANS = Counter("lobbyd_ticket_scans_total", "Tickets scanned at the door.", ["mode", "result"])

OUTBOX_PUBLISHED = Counter(
    "lobbyd_outbox_published_total", "Events relayed from the outbox to the stream.", ["topic"]
)
OUTBOX_LAG = Gauge(
    "lobbyd_outbox_lag_seconds", "Age of the oldest event still waiting to be relayed."
)
EVENTS_HANDLED = Counter(
    "lobbyd_events_handled_total",
    "Events taken off the stream, by how it went: ok, retry, dead.",
    ["topic", "outcome"],
)
EVENT_DURATION = Histogram(
    "lobbyd_event_handle_duration_seconds",
    "Time for all of an event's handlers to run.",
    ["topic"],
    buckets=_LATENCY,
)
WORKER_LOOP_FAILURES = Counter(
    "lobbyd_worker_loop_failures_total", "Passes of a worker loop that raised.", ["loop"]
)
HOUSEKEEPING = Counter(
    "lobbyd_housekeeping_total",
    "What the sweeper cleaned up.",
    ["what"],  # holds_expired, seats_freed, orders_expired, payments_polled
)
INVARIANT_VIOLATIONS = Gauge(
    "lobbyd_invariant_violations",
    "Rules of the data model found broken by the last reconciliation. Anything but 0 is a page.",
)
RECONCILED_AT = Gauge("lobbyd_reconciled_timestamp_seconds", "When reconciliation last finished.")

ASSISTANT_TURNS = Counter(
    "lobbyd_assistant_turns_total",
    "Assistant turns by how they ended: answered, awaiting_approval, unverified, gave_up, "
    "model_error.",
    ["outcome"],
)
ASSISTANT_TOOL_CALLS = Counter(
    "lobbyd_assistant_tool_calls_total", "Tools the assistant ran.", ["tool", "outcome"]
)
ASSISTANT_ACTIONS = Counter(
    "lobbyd_assistant_actions_total",
    "Changes the assistant proposed, by what the person decided and how it went.",
    ["status"],
)
ASSISTANT_MODEL_SECONDS = Histogram(
    "lobbyd_assistant_model_duration_seconds",
    "Time the language model took per call.",
    buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30),
)
ASSISTANT_TOKENS = Counter(
    "lobbyd_assistant_tokens_total", "Tokens sent to and received from the model.", ["direction"]
)

DB_POOL = Gauge(
    "lobbyd_db_pool_connections",
    "Database connections: how many are checked out, and how many there can be.",
    ["state"],
    multiprocess_mode="livesum",
)


def render() -> tuple[bytes, str]:
    registry: CollectorRegistry = REGISTRY
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        # Several uvicorn workers: each writes its numbers to that directory,
        # and whichever one is scraped adds them all up.
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return generate_latest(registry), CONTENT_TYPE_LATEST


router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    body, content_type = render()
    return Response(body, media_type=content_type)


def serve(port: int) -> None:
    """For processes with no HTTP server of their own (the worker)."""
    start_http_server(port)
