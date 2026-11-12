"""A small in-process cache for facts that rarely or never change.

Redis saves a database query at the price of a network round trip. For
lookups made on every request (a seat's label, a ticket type's name, a
tournament's sale window) even that is too much: this keeps them in the
process's own memory. Each API process has its own copy, so entries carry a
short lifetime unless the fact is immutable.
"""

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Iterable


class Memo[K: Hashable, V]:
    def __init__(self, *, ttl_seconds: float | None, max_entries: int = 50_000) -> None:
        """`ttl_seconds=None` is for values that never change once written."""
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires and time.monotonic() > expires:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        if self.ttl is not None and self.ttl <= 0:
            return  # caching switched off
        expires = time.monotonic() + self.ttl if self.ttl is not None else 0.0
        self._entries[key] = (expires, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)  # least recently used

    def drop(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    async def get_many(
        self, keys: Iterable[K], load: Callable[[list[K]], Awaitable[dict[K, V]]]
    ) -> dict[K, V]:
        """Values for `keys`, querying only for the ones not already known."""
        found: dict[K, V] = {}
        missing: list[K] = []
        for key in dict.fromkeys(keys):
            value = self.get(key)
            if value is None:
                missing.append(key)
            else:
                found[key] = value
        if missing:
            loaded = await load(missing)
            for key, value in loaded.items():
                self.put(key, value)
            found.update(loaded)
        return found
