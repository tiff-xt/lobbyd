"""Distributed tracing with OpenTelemetry. Off unless LOBBYD_OTLP_ENDPOINT is set.

One checkout touches the API, Postgres, Redis, a worker, and the payment
provider, with two queues in between. A trace puts all of it on one
timeline. The request's trace context is saved with each outbox event and
picked up by the worker that handles it, so the trace continues across the
queue instead of stopping at the HTTP response.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind
from structlog.types import EventDict

from lobbyd.config import Settings

_provider: TracerProvider | None = None


def enabled() -> bool:
    return _provider is not None


def configure(
    settings: Settings, service: str, *, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Start exporting spans. Safe to call more than once; only the first call counts."""
    global _provider
    if _provider is not None:
        return _provider
    if exporter is None and not settings.otlp_endpoint:
        return None

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is not None:  # tests: export at once, in process
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        endpoint = f"{settings.otlp_endpoint.rstrip('/')}/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    RedisInstrumentor().instrument(tracer_provider=provider)
    _provider = provider
    return provider


def instrument_app(app: Any) -> None:
    if _provider is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_provider, excluded_urls="healthz,readyz,metrics"
    )


def instrument_engine(engine: Any) -> None:
    """One span per SQL statement, by listening to the engine's own events.

    (The OpenTelemetry SQLAlchemy package does not support SQLAlchemy 2.1.)
    The span carries the statement text with its placeholders, never the
    values, so no personal data ends up in a trace.
    """
    if _provider is None:
        return
    from sqlalchemy import event

    tracer = _provider.get_tracer("lobbyd.sql")

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _start(conn: Any, cursor: Any, statement: str, *_: Any) -> None:
        span = tracer.start_span(
            statement.split(None, 1)[0].upper() if statement.strip() else "SQL",
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.statement": statement[:2000]},
        )
        conn.info.setdefault("otel_spans", []).append(span)

    def _end(conn: Any, error: BaseException | None = None) -> None:
        spans = conn.info.get("otel_spans")
        if spans:
            span = spans.pop()
            if error is not None:
                span.record_exception(error)
                span.set_status(trace.StatusCode.ERROR)
            span.end()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _finish(conn: Any, *_: Any) -> None:
        _end(conn)

    @event.listens_for(engine.sync_engine, "handle_error")
    def _failed(context: Any) -> None:
        if context.connection is not None:
            _end(context.connection, context.original_exception)


def current_context() -> dict[str, str] | None:
    """The trace this code is running in, as headers. None when there is none."""
    if _provider is None:
        return None
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier or None


@contextmanager
def continued(name: str, carrier: dict[str, str] | None, **attributes: str) -> Iterator[None]:
    """Run a block as a span of the trace `carrier` came from (a new trace if it has none)."""
    if _provider is None:
        yield
        return
    context = propagate.extract(carrier) if carrier else None
    with _provider.get_tracer("lobbyd").start_as_current_span(
        name, context=context, kind=SpanKind.CONSUMER, attributes=attributes
    ):
        yield


def add_trace_id(_: Any, __: str, event: EventDict) -> EventDict:
    """structlog processor: put the trace id on every log line, to jump from a log to its trace."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event["trace_id"] = format(context.trace_id, "032x")
    return event


def shutdown() -> None:
    """Flush what is buffered. Also undoes the global instrumentation (tests)."""
    global _provider
    if _provider is None:
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    HTTPXClientInstrumentor().uninstrument()
    RedisInstrumentor().uninstrument()
    _provider.shutdown()
    _provider = None
