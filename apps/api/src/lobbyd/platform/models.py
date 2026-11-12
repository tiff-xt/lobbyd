"""Registry of model modules.

Importing them attaches their tables to Base.metadata, which migrations and
the schema drift test need. Imports are deferred so this bottom-layer module
does not depend on feature modules at import time.
"""

from importlib import import_module

_MODEL_MODULES = ("lobbyd.identity.models", "lobbyd.catalog.models")


def import_all_models() -> None:
    for module in _MODEL_MODULES:
        import_module(module)
