"""Shared text normalization helpers."""

from __future__ import annotations

import re


def title_case_name(name: str, *, preserve_stored_casing: bool = False) -> str:
    """Title-case a name from normalized (often lowercased) lead data.

    When ``preserve_stored_casing`` is True (company names), keep the stored
    string if it already has meaningful caps; otherwise title-case it.
    Person names always pass ``preserve_stored_casing=False``.
    """
    name = (name or "").strip()
    if not name:
        return name
    if preserve_stored_casing and name != name.lower():
        return name

    def capitalize_part(part: str) -> str:
        if not part:
            return part
        if "'" in part:
            return "'".join(
                capitalize_part(p) if p else p for p in part.split("'")
            )
        return part[0].upper() + part[1:].lower()

    pieces: list[str] = []
    for segment in re.split(r"(\s+|-)", name):
        if not segment or segment.isspace() or segment == "-":
            pieces.append(segment)
        else:
            pieces.append(capitalize_part(segment))
    return "".join(pieces)
