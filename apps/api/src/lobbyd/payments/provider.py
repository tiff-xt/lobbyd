"""The payment provider, as this application sees it."""

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

SIGNATURE_HEADER = "Paysim-Signature"


@dataclass(frozen=True)
class ProviderPayment:
    ref: str
    status: str
    # Keys of partial refunds the provider has carried out.
    refund_keys: frozenset[str] = frozenset()


class PaymentProvider(Protocol):
    async def create_payment(
        self, *, key: str, reference: str, amount_cents: int, currency: str, method: str
    ) -> ProviderPayment: ...

    async def get_payment(self, ref: str) -> ProviderPayment: ...

    async def refund(self, *, key: str, ref: str, amount_cents: int | None = None) -> None: ...

    async def aclose(self) -> None: ...


class PaysimProvider:
    """HTTP client for the payment simulator.

    Every write sends an idempotency key, so a call that timed out can simply
    be made again: the provider returns what it already did.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @classmethod
    def connect(cls, base_url: str) -> "PaysimProvider":
        return cls(httpx.AsyncClient(base_url=base_url, timeout=5.0))

    async def create_payment(
        self, *, key: str, reference: str, amount_cents: int, currency: str, method: str
    ) -> ProviderPayment:
        response = await self._client.post(
            "/payments",
            json={
                "reference": reference,
                "amount_cents": amount_cents,
                "currency": currency,
                "method": method,
            },
            headers={"Idempotency-Key": key},
        )
        response.raise_for_status()
        body = response.json()
        return ProviderPayment(body["id"], body["status"])

    async def get_payment(self, ref: str) -> ProviderPayment:
        response = await self._client.get(f"/payments/{ref}")
        response.raise_for_status()
        body = response.json()
        return ProviderPayment(body["id"], body["status"], frozenset(body.get("refunds", ())))

    async def refund(self, *, key: str, ref: str, amount_cents: int | None = None) -> None:
        """All of the payment, or `amount_cents` of it."""
        response = await self._client.post(
            f"/payments/{ref}/refunds",
            headers={"Idempotency-Key": key},
            json=None if amount_cents is None else {"amount_cents": amount_cents},
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


class InvalidSignatureError(Exception):
    pass


def verify_signature(
    secret: str, header: str | None, body: bytes, *, tolerance_seconds: int
) -> None:
    """Check a webhook really came from the provider, and recently.

    Header format: `t=<unix seconds>,v1=<hex hmac-sha256 of "<t>.<body>">`.
    The timestamp is inside the signed data, so an attacker who captured a
    genuine delivery cannot replay it later with a fresh timestamp.
    """
    try:
        parts = dict(part.split("=", 1) for part in (header or "").split(","))
        timestamp = int(parts["t"])
        received = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise InvalidSignatureError("malformed signature header") from exc

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    # Constant time: a plain == would leak, through timing, how many leading
    # characters of a forged signature were right.
    if not hmac.compare_digest(expected, received):
        raise InvalidSignatureError("signature mismatch")
    if abs(time.time() - timestamp) > tolerance_seconds:
        raise InvalidSignatureError("timestamp outside tolerance")
