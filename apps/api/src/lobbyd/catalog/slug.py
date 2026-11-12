"""URL slugs for tournaments."""

import re
import secrets
import unicodedata

_MAX_LENGTH = 60


def slugify(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:_MAX_LENGTH].strip("-") or "tournament"


def with_suffix(slug: str) -> str:
    """Used when the plain slug is taken."""
    return f"{slug}-{secrets.token_hex(3)}"
