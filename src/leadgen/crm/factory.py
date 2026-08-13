"""Backend selection for the lead store.

Callers construct their database through :func:`create_database` so the
``database.backend`` config switch is honored everywhere — CLI, MCP stdio
server, and the container's internal HTTP API — rather than each entry point
hardcoding one backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from leadgen.config.loader import LeadGenConfig

if TYPE_CHECKING:
    from leadgen.crm.d1 import D1Database
    from leadgen.crm.database import LeadDatabase


def create_database(config: LeadGenConfig) -> LeadDatabase | D1Database:
    """Return the lead store selected by ``config.database.backend``.

    Defaults to SQLite. The D1 adapter is imported lazily so a local install
    never pays for it, and so an engine running on SQLite is unaffected by
    anything in the D1 module.
    """
    backend = (config.database.backend or "sqlite").strip().lower()

    if backend == "d1":
        from leadgen.crm.d1 import D1Database

        return D1Database(config.database.d1_url or None)

    from leadgen.crm.database import LeadDatabase

    return LeadDatabase(config.database.sqlite_path)


def describe_backend(config: LeadGenConfig) -> dict[str, Any]:
    """Human-readable backend summary for startup logs and health checks.

    Never includes credentials — the D1 path has none, and the SQLite path
    reports only a filesystem location.
    """
    backend = (config.database.backend or "sqlite").strip().lower()
    if backend == "d1":
        from leadgen.crm.d1 import resolve_d1_url

        return {"backend": "d1", "url": resolve_d1_url(config.database.d1_url or None)}
    return {"backend": "sqlite", "path": config.database.sqlite_path}
