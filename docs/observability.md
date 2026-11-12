# Observability

Three questions, three tools: *what is happening* (metrics), *what happened to this one request* (traces), and *what exactly did the code say* (logs). All three carry the same ids, so you can move between them.

```bash
docker compose -f docker-compose.yml -f docker-compose.obs.yml up --build
```

| | Address | What is there |
| --- | --- | --- |
| Grafana | http://localhost:3001 | The `lobbyd` dashboard, no login |
| Prometheus | http://localhost:9090 | Raw metrics; alert rules and their state under **Alerts** |
| Jaeger | http://localhost:16686 | Traces: choose the `lobbyd-api` service |

To see it move, run a load test in another terminal: `cd apps/api && uv run python bench/run.py steady`.

## Metrics

The API serves them at `/metrics`, the worker at port 9100. Defined in one file, [`platform/metrics.py`](../apps/api/src/lobbyd/platform/metrics.py).

| Metric | Answers |
| --- | --- |
| `lobbyd_http_requests_total{method, route, status}` | How much traffic, and how much of it fails |
| `lobbyd_http_request_duration_seconds{method, route}` | How slow, per endpoint (histogram) |
| `lobbyd_problem_responses_total{code}` | Who is being turned away and why: `sold_out`, `seat_taken`, `rate_limited`, `overloaded` |
| `lobbyd_db_pool_connections{state}` | Whether requests are queueing for the database |
| `lobbyd_cache_lookups_total{result}` | Whether the cache is earning its keep |
| `lobbyd_outbox_lag_seconds` | Whether committed events are reaching the workers |
| `lobbyd_outbox_published_total{topic}` | Business events per second: orders paid, cancelled, refunded |
| `lobbyd_events_handled_total{topic, outcome}` | Whether handlers succeed, retry, or give up |
| `lobbyd_invariant_violations` | Whether the data is still right. Must be 0 |
| `lobbyd_ticket_scans_total{mode, result}` | What is happening at the door |
| `lobbyd_live_clients` | How many browsers are watching a bracket |

Two rules kept the label sets small. A route label is the route's template (`/orders/{order_id}`), never the path, and requests that match no route share the label `unmatched`: a metric cannot grow a series per order or per URL a scanner probes. And there is no user, tournament, or order id on any metric; that level of detail belongs to traces and logs.

Business numbers come from the outbox on purpose. `order.paid` is counted when the relay publishes the event, which only happens for committed transactions, so the dashboard cannot count an order that was rolled back.

### With several API processes

Each uvicorn worker process keeps its own counters, so a scrape would see only the one that answered. Set `PROMETHEUS_MULTIPROC_DIR` to an empty directory before starting and every process writes there; whichever is scraped adds them up. Empty the directory on each start. Compose runs one process per container (scale with more containers), so it does not need this.

## Alerts

[`ops/prometheus/alerts.yml`](../ops/prometheus/alerts.yml), with unit tests beside it that CI runs with `promtool`. Nine rules in two severities:

| Alert | Fires when | Severity |
| --- | --- | --- |
| `InvariantViolated` | Reconciliation finds any broken rule of the data model | page |
| `HighErrorRate` | Over 2% of requests return 5xx for five minutes | page |
| `HoldsAreSlow` | p95 of `POST /holds` is over one second for five minutes | page |
| `OutboxBacklog` | The oldest unpublished event is over 30 seconds old for two minutes | page |
| `TargetDown` | The API or worker stops answering scrapes for a minute | page |
| `ReconciliationNotRunning` | No reconciliation has finished for fifteen minutes | ticket |
| `DatabasePoolSaturated` | Over 90% of connections in use for five minutes | ticket |
| `EventsDeadLettered` | Any event was given up on in the last fifteen minutes | ticket |
| `WorkerLoopFailing` | A worker loop keeps raising | ticket |

Each is on a symptom a customer or an organizer would feel, and each description says what to do. There is no alert on CPU or memory: those are causes, and a cause that hurts nobody is not worth waking for. The local stack evaluates the rules and shows them in Prometheus; it does not send notifications (that would be Alertmanager, pointed at whatever the team uses).

## Traces

Off by default. Set `LOBBYD_OTLP_ENDPOINT` to an OTLP/HTTP collector (the overlay sets it to Jaeger) and every request becomes a trace with a span for each SQL statement, Redis command, and outgoing HTTP call. SQL spans carry the statement with placeholders, never the values.

The interesting part is what happens at the queues. A checkout is not one request:

```
POST /orders                      (API)       writes the order and an outbox event
  handle payment.requested        (worker)    calls the payment provider
    POST /payments                (provider)
  ... 1.5 seconds pass ...
  POST /webhooks/payments         (API)       marks the order paid, writes another event
    handle order.paid             (worker)    sends the confirmation, confirms the team
```

All of it is one trace. The trace context is saved in the outbox row with the event and picked up by the worker that handles it. Across the payment provider, which knows nothing about our tracing, the standard `traceparent` header is sent with the payment and the simulator returns it on the webhook. A test asserts the whole chain shares one trace id.

## Logs

One JSON line per request and per event (set `LOBBYD_LOG_JSON=true`; Compose does). Every line carries `request_id`, which is also returned to the client in `X-Request-ID`, and `trace_id` when tracing is on. From a user's bug report to the request, to its trace, to every log line of every process that touched it, is a search for one id.
