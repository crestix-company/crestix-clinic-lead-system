"""Cloud Run ASGI entrypoint loader.

The deployment-specific factory path is intentionally externalized so the
repository cannot silently discover production credentials or connect by itself.
It must return create_app(...) with Secret Manager values and injected adapters.
"""
from __future__ import annotations

import importlib
import os


def _load_factory(path: str):
    module_name, separator, name = path.partition(":")
    if not separator or not module_name or not name:
        raise RuntimeError("SHARED_DRAFT_API_FACTORY must be module:function")
    return getattr(importlib.import_module(module_name), name)


factory_path = os.environ.get(
    "SHARED_DRAFT_API_FACTORY",
    "src.outreach.shared_draft_runtime_factory:create_runtime_app",
).strip()
app = _load_factory(factory_path)()

