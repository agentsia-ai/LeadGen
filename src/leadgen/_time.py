"""Time helpers — all engine timestamps go through here.

Aware-UTC by construction. Reads tolerate legacy naive ISO strings (treated
as UTC) so existing local SQLite rows continue to work without a one-shot
migration script.

Private module: import via `from leadgen._time import now_utc, ...`. Not
re-exported at the package level — engine internals + tests only.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Current time as a tz-aware UTC datetime.

    Replaces every prior call to `datetime.utcnow()` in the engine. Returning
    an aware datetime means downstream `.isoformat()` produces a string with
    a `+00:00` offset, which round-trips losslessly through `parse_iso`.
    """
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    """ISO 8601 string for storage. None passes through.

    Aware datetimes serialize with a `+00:00` suffix; naive datetimes (which
    should not occur post-migration) serialize without one. Use only on
    values produced by `now_utc()` or `parse_iso()` to guarantee aware output.
    """
    return dt.isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a tz-aware UTC datetime.

    Tolerates both shapes for forward/back compat:
      - aware  ('2025-01-15T10:30:00+00:00') — converted to UTC
      - naive  ('2025-01-15T10:30:00')       — assumed UTC, tz attached

    Returns None for None / empty input. Raises ValueError on malformed input
    (same behavior as `datetime.fromisoformat`).
    """
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
