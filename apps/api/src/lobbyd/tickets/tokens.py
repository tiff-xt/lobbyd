"""The signed token inside a ticket's QR code.

    lbd1.<key id>.<ticket id + tournament id>.<Ed25519 signature>

Signed with a private key only the server holds. A scanner needs just the
public key to tell a real ticket from a made-up one, with no network, and a
stolen scanner cannot mint tickets. The token carries no personal data.

A valid signature proves the ticket was issued. Whether it is still valid
and unused is state, which the scanner gets from the server or its manifest.
"""

import base64
import hashlib
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lobbyd.config import Settings

PREFIX = "lbd1"


class InvalidTicketToken(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    # Either base64 alphabet, padded or not: key generators differ.
    text = text.strip().rstrip("=").replace("+", "-").replace("/", "_")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class TicketClaim:
    ticket_id: uuid.UUID
    tournament_id: uuid.UUID


class Signer:
    def __init__(self, seed: bytes) -> None:
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        public = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key = _b64(public)
        # Names the key inside every token, so keys can be rotated: a scanner
        # holding several public keys knows which one to check against.
        self.key_id = hashlib.sha256(public).hexdigest()[:8]

    def sign(self, ticket_id: uuid.UUID, tournament_id: uuid.UUID) -> str:
        payload = ticket_id.bytes + tournament_id.bytes
        signed = f"{PREFIX}.{self.key_id}.{_b64(payload)}"
        return f"{signed}.{_b64(self._key.sign(signed.encode()))}"

    def verify(self, token: str) -> TicketClaim:
        try:
            prefix, key_id, payload, signature = token.strip().split(".")
            if prefix != PREFIX or key_id != self.key_id:
                raise InvalidTicketToken
            self._key.public_key().verify(
                _unb64(signature), f"{prefix}.{key_id}.{payload}".encode()
            )
            raw = _unb64(payload)
            if len(raw) != 32:
                raise InvalidTicketToken
        except (ValueError, InvalidSignature) as exc:
            raise InvalidTicketToken from exc
        return TicketClaim(uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:]))


_signer: Signer | None = None


def configure(settings: Settings) -> None:
    global _signer
    if settings.ticket_signing_key is not None:
        seed = _unb64(settings.ticket_signing_key.get_secret_value())
        if len(seed) != 32:
            raise ValueError("LOBBYD_TICKET_SIGNING_KEY must be 32 bytes, base64url encoded")
    else:
        # Local and test only (production refuses to start without a key).
        secret = settings.jwt_secret.get_secret_value()
        seed = hashlib.sha256(f"lobbyd-ticket-key:{secret}".encode()).digest()
    _signer = Signer(seed)


def signer() -> Signer:
    if _signer is None:
        raise RuntimeError("tickets.tokens.configure() has not been called")
    return _signer
