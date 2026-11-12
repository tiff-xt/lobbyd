"""Metrics and tracing: what an operator sees."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lobbyd import reconcile
from lobbyd.config import Settings
from lobbyd.platform import tracing
from tests.helpers import buy, hold, make_buyers, open_sale

Sessions = async_sessionmaker[AsyncSession]


async def scrape(client: AsyncClient) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }


Samples = dict[tuple[str, frozenset[tuple[str, str]]], float]


def value(samples: Samples, name: str, **labels: str) -> float:
    return samples.get((name, frozenset(labels.items())), 0.0)


async def test_metrics_count_what_happened(
    client: AsyncClient, app: FastAPI, sessionmaker: Sessions, settings: Settings
) -> None:
    sale = await open_sale(client, sessionmaker, general=2)
    first, second = await make_buyers(sessionmaker, settings, 2)
    before = await scrape(client)

    held = (await hold(client, first, sale, general=2)).json()
    assert (await hold(client, second, sale, general=1)).json()["code"] == "sold_out"
    order = await buy(client, app, first, held["id"])
    await client.get(f"/orders/{order['id']}", headers=first)
    await client.get("/no/such/page/1")
    await client.get("/no/such/page/2")
    async with sessionmaker() as session:
        await reconcile.run(session)

    after = await scrape(client)

    def grew(name: str, **labels: str) -> float:
        return value(after, name, **labels) - value(before, name, **labels)

    holds = {"method": "POST", "route": "/holds"}
    assert grew("lobbyd_http_requests_total", status="201", **holds) == 1
    assert grew("lobbyd_http_requests_total", status="409", **holds) == 1
    assert grew("lobbyd_http_request_duration_seconds_count", **holds) == 2
    # Labelled by the route's template, not the path: one series however many orders.
    one_order = {"method": "GET", "route": "/orders/{order_id}", "status": "200"}
    assert grew("lobbyd_http_requests_total", **one_order) == 2
    assert not [key for key in after if order["id"] in str(key)]
    # Paths that match nothing share a single label.
    assert grew("lobbyd_http_requests_total", method="GET", route="unmatched", status="404") == 2

    assert grew("lobbyd_problem_responses_total", code="sold_out") == 1
    assert grew("lobbyd_outbox_published_total", topic="order.paid") == 1
    assert grew("lobbyd_events_handled_total", topic="order.paid", outcome="ok") == 1
    assert grew("lobbyd_event_handle_duration_seconds_count", topic="payment.requested") == 1
    assert value(after, "lobbyd_invariant_violations") == 0
    assert value(after, "lobbyd_reconciled_timestamp_seconds") > 0
    assert value(after, "lobbyd_db_pool_connections", state="capacity") > 0
    # Scraping is not itself counted, or every scrape would move the numbers.
    assert not [key for key in after if ("route", "/metrics") in key[1]]


@pytest.fixture
def spans(app: FastAPI, settings: Settings) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tracing.configure(settings, "lobbyd-test", exporter=exporter)
    tracing.instrument_app(app)
    # The API's client to the provider was made before tracing was switched
    # on (in production it never is). The provider itself is left alone: it
    # is someone else's service and knows nothing of our tracing.
    HTTPXClientInstrumentor.instrument_client(app.state.provider._client)
    try:
        yield exporter
    finally:
        HTTPXClientInstrumentor.uninstrument_client(app.state.provider._client)
        FastAPIInstrumentor.uninstrument_app(app)
        tracing.shutdown()


async def test_one_checkout_is_one_trace_across_both_queues(
    client: AsyncClient,
    app: FastAPI,
    sessionmaker: Sessions,
    settings: Settings,
    spans: InMemorySpanExporter,
) -> None:
    sale = await open_sale(client, sessionmaker)
    (buyer,) = await make_buyers(sessionmaker, settings, 1)
    held = (await hold(client, buyer, sale, general=1)).json()
    spans.clear()

    order = await buy(client, app, buyer, held["id"])
    assert order["status"] == "paid"

    finished = spans.get_finished_spans()
    (checkout,) = [s for s in finished if s.name == "POST /orders"]
    trace_id = checkout.context.trace_id
    in_trace = {s.name for s in finished if s.context.trace_id == trace_id}

    # The request, the worker picking up its event from the outbox, the call
    # to the provider, the provider's webhook seconds later, and the worker
    # sending the confirmation: one timeline.
    assert {
        "POST /orders",
        "handle payment.requested",
        "POST /webhooks/payments",
        "handle order.paid",
    } <= in_trace
    (handler,) = [s for s in finished if s.name == "handle payment.requested"]
    assert handler.parent is not None
    assert handler.attributes is not None
    assert handler.attributes["messaging.message.id"]
