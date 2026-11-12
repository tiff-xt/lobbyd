"""Cursor (keyset) pagination helpers.

A cursor is the sort key of the last row on the page. The next page is
"rows after that key", which the database answers from an index no matter how
deep the page is. OFFSET would read and throw away every skipped row, and can
skip or repeat rows when data changes between requests.
"""

import base64
import binascii
import json
from typing import Any

from pydantic import BaseModel

from lobbyd.platform.errors import ProblemError


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def encode_cursor(*parts: Any) -> str:
    raw = json.dumps([str(part) for part in parts], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str, expected_parts: int) -> list[str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parts = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _invalid() from exc
    if (
        not isinstance(parts, list)
        or len(parts) != expected_parts
        or not all(isinstance(part, str) for part in parts)
    ):
        raise _invalid()
    return parts


def _invalid() -> ProblemError:
    return ProblemError(422, "invalid_cursor", "The cursor is not valid.")
