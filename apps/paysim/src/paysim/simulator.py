"""The simulator's state and behaviour, independent of HTTP.

A payment's life is a list of scheduled events. When one comes due it changes
the payment's status and, usually, is delivered to the merchant as a signed
webhook. Each payment method token picks a different schedule, which is how a
caller asks for a particular failure:

    pm_success      processing, then succeeded
    pm_declined     processing, then failed
    pm_duplicate    succeeded is delivered twice (same event id)
    pm_reordered    succeeded is delivered BEFORE processing
    pm_slow         succeeds, but only after a long delay
    pm_never        succeeds, but no webhook is ever sent
"""

import asyncio
import heapq
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from paysim.signing import HEADER, sign

log = structlog.get_logger()

METHODS = ("pm_success", "pm_declined", "pm_duplicate", "pm_reordered", "pm_slow", "pm_never")
MAX_DELIVERY_ATTEMPTS = 6
DELIVERY_CONCURRENCY = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYSIM_", env_file=".env", extra="ignore")

    webhook_url: str = "http://localhost:8000/webhooks/payments"
    webhook_secret: SecretStr = SecretStr("dev-only-paysim-webhook-secret")
    delay_ms: int = 1500  # how long a payment takes to settle
    slow_delay_ms: int = 20_000  # for pm_slow


@dataclass
class Payment:
    id: str
    reference: str
    amount_cents: int
    currency: str
    method: str
    status: str = "created"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reference": self.reference,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "status": self.status,
        }


@dataclass(order=True)
class Scheduled:
    due: float
    sequence: int
    payment_id: str = field(compare=False)
    status: str = field(compare=False)
    event_id: str = field(compare=False)
    deliver: bool = field(compare=False)
    attempt: int = field(compare=False, default=1)


class UnknownMethodError(ValueError):
    pass


class Simulator:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self.payments: dict[str, Payment] = {}
        self._by_key: dict[str, str] = {}
        self._queue: list[Scheduled] = []
        self._sequence = 0
        self._wake = asyncio.Event()

    # --- API ---------------------------------------------------------------

    def create_payment(
        self, *, key: str, reference: str, amount_cents: int, currency: str, method: str
    ) -> Payment:
        if key in self._by_key:  # a retried request gets the payment it already made
            return self.payments[self._by_key[key]]
        if method not in METHODS:
            raise UnknownMethodError(method)

        payment = Payment(f"pay_{secrets.token_hex(8)}", reference, amount_cents, currency, method)
        self.payments[payment.id] = payment
        self._by_key[key] = payment.id

        delay = self.settings.delay_ms / 1000
        outcome = "failed" if method == "pm_declined" else "succeeded"
        if method == "pm_slow":
            delay = self.settings.slow_delay_ms / 1000
        deliver = method != "pm_never"

        processing_at, outcome_at = delay / 2, delay
        if method == "pm_reordered":
            processing_at, outcome_at = delay, delay / 2
        self._schedule(payment.id, "processing", processing_at, deliver=deliver)
        outcome_event = self._schedule(payment.id, outcome, outcome_at, deliver=deliver)
        if method == "pm_duplicate":
            self._schedule(payment.id, outcome, delay * 1.2, deliver=True, event_id=outcome_event)
        return payment

    def refund(self, *, key: str, payment_id: str) -> Payment | None:
        payment = self.payments.get(payment_id)
        if payment is None:
            return None
        if key not in self._by_key:
            self._by_key[key] = payment_id
            self._schedule(payment_id, "refunded", self.settings.delay_ms / 2000, deliver=True)
        return payment

    # --- events ---------------------------------------------------------------

    def _schedule(
        self,
        payment_id: str,
        status: str,
        after_seconds: float,
        *,
        deliver: bool,
        event_id: str | None = None,
        attempt: int = 1,
    ) -> str:
        self._sequence += 1
        event_id = event_id or f"evt_{secrets.token_hex(8)}"
        heapq.heappush(
            self._queue,
            Scheduled(
                time.monotonic() + after_seconds,
                self._sequence,
                payment_id,
                status,
                event_id,
                deliver,
                attempt,
            ),
        )
        self._wake.set()
        return event_id

    async def deliver_due(self, *, everything: bool = False, concurrency: int = 1) -> int:
        """Fire events that are due (or all of them). Returns how many fired.

        One at a time by default, which gives tests an exact order. The
        background loop passes a higher `concurrency`: a real provider does
        not wait for one merchant response before sending the next webhook,
        and a simulator that did would become the slowest part of any load
        test (it was: a minute-long backlog at a few dozen payments a second).
        """
        if concurrency <= 1:
            fired = 0
            while self._queue and (everything or self._queue[0].due <= time.monotonic()):
                await self._fire(heapq.heappop(self._queue))
                fired += 1
            return fired

        due = []
        while self._queue and (everything or self._queue[0].due <= time.monotonic()):
            due.append(heapq.heappop(self._queue))
        gate = asyncio.Semaphore(concurrency)

        async def fire(event: Scheduled) -> None:
            async with gate:
                await self._fire(event)

        await asyncio.gather(*(fire(event) for event in due))
        return len(due)

    async def _fire(self, event: Scheduled) -> None:
        payment = self.payments[event.payment_id]
        # A provider's own state only moves forward, whatever order its
        # webhooks happen to be delivered in.
        forward = {
            "processing": ("created",),
            "succeeded": ("created", "processing"),
            "failed": ("created", "processing"),
            "refunded": ("succeeded",),
        }
        if payment.status in forward[event.status]:
            payment.status = event.status
        if not event.deliver:
            return

        kind = "refund" if event.status == "refunded" else "payment"
        body = json.dumps(
            {
                "id": event.event_id,
                "type": f"{kind}.{'succeeded' if event.status == 'refunded' else event.status}",
                "created": datetime.now(UTC).isoformat(),
                "data": {**payment.public(), "status": event.status},
            }
        ).encode()
        signature = sign(self.settings.webhook_secret.get_secret_value(), int(time.time()), body)
        try:
            response = await self.http.post(
                self.settings.webhook_url,
                content=body,
                headers={HEADER: signature, "Content-Type": "application/json"},
            )
            delivered = response.status_code < 300
        except httpx.HTTPError:
            delivered = False
        if delivered or event.attempt >= MAX_DELIVERY_ATTEMPTS:
            if not delivered:
                log.warning("webhook_abandoned", event_id=event.event_id)
            return
        # Like a real provider: keep trying, with growing gaps.
        self._schedule(
            event.payment_id,
            event.status,
            min(2.0**event.attempt, 60.0),
            deliver=True,
            event_id=event.event_id,
            attempt=event.attempt + 1,
        )

    async def run(self) -> None:
        """Deliver events as they come due, forever."""
        while True:
            await self.deliver_due(concurrency=DELIVERY_CONCURRENCY)
            self._wake.clear()
            timeout = max(0.0, self._queue[0].due - time.monotonic()) if self._queue else None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue
