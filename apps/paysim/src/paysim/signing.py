"""Webhook signatures.

The header is `t=<unix seconds>,v1=<hex hmac-sha256 of "<t>.<body>">`.
Signing the timestamp together with the body lets the receiver reject an old
delivery that someone captured and sent again.

The receiving application has its own copy of this logic on purpose: it would
not import code from a real payment provider either.
"""

import hashlib
import hmac

HEADER = "Paysim-Signature"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"
