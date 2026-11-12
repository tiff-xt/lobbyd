"""Registry of model modules.

Importing them attaches their tables to Base.metadata, which migrations and
the schema drift test need. Imports are deferred so this bottom-layer module
does not depend on feature modules at import time.
"""

from importlib import import_module

_MODEL_MODULES = (
    "lobbyd.platform.idempotency",
    "lobbyd.identity.models",
    "lobbyd.catalog.models",
    "lobbyd.inventory.models",
    "lobbyd.tickets.models",
    "lobbyd.orders.models",
)


def import_all_models() -> None:
    for module in _MODEL_MODULES:
        import_module(module)
