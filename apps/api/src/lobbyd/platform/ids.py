"""UUIDv7 primary keys.

Time-ordered, so inserts land at the end of the index the way a sequence
would, and the application can generate them before the insert.
Layout (RFC 9562): 48 bits of Unix milliseconds, 4 bits version, 12 random
bits, 2 bits variant, 62 random bits.
"""

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10))
    rand_a = rand >> 68  # top 12 of 80 random bits
    rand_b = rand & ((1 << 62) - 1)
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)
