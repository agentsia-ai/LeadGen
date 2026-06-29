"""Shared text normalization helpers."""

from __future__ import annotations

import re

# Words, apostrophe names (O'Connor), or single-letter initials with a period (J.).
_NAME_TOKEN_RE = re.compile(
    r"[a-zA-Z]'[a-zA-Z]+(?:'[a-zA-Z]+)*|[a-zA-Z]\.|[\w']+"
)


def name_tokens(name: str) -> list[str]:
    """Split a person or company name into casing tokens."""
    return _NAME_TOKEN_RE.findall(name or "")


def _has_mixed_case(name: str) -> bool:
    """True when the string has both upper- and lowercase letters."""
    return name != name.lower() and name != name.upper()


def _capitalize_name_part(part: str) -> str:
    """Title-case a single name token, with common intercap heuristics."""
    if not part:
        return part
    if re.fullmatch(r"[a-zA-Z]\.", part):
        return part[0].upper() + "."
    if "'" in part:
        return "'".join(_capitalize_name_part(p) if p else p for p in part.split("'"))

    lower = part.lower()
    # Mac before Mc — "macdonald" must not match the two-letter Mc prefix.
    if lower.startswith("mac") and len(lower) > 3:
        return "Mac" + _capitalize_name_part(lower[3:])
    if lower.startswith("mc") and len(lower) > 2:
        return "Mc" + _capitalize_name_part(lower[2:])
    return lower[0].upper() + lower[1:]


def _repair_naive_mc_casing(name: str) -> str:
    """Fix Mc names broken by naive title-case (Mchugh -> McHugh)."""
    if re.match(r"^Mc[a-z]", name):
        return "Mc" + _capitalize_name_part(name[2:])
    return name


def looks_like_acronym(word: str) -> bool:
    """True for short or non-word all-caps tokens (GTPS, ACME, B2B), not common words."""
    letters = [c for c in word if c.isalpha()]
    if len(letters) < 2:
        return False
    if any(c.isdigit() for c in word):
        return True
    if len(letters) <= 4:
        return True
    if not any(c.lower() in "aeiou" for c in letters):
        return True
    return False


def normalize_company_display_name(name: str) -> str:
    """Fix all-caps multi-word company names from noisy sources (e.g. PDL enrich).

    Preserves operator/source mixed case (LuckyTruck, McHugh, CORFAC International)
    and short acronym tokens (GTPS). Only repairs shouting common words such as
    PROTECT REALTY -> Protect Realty.
    """
    name = (name or "").strip()
    if not name:
        return name

    if _has_mixed_case(name):
        return name

    tokens = name_tokens(name)
    if len(tokens) < 2 or name != name.upper():
        return name

    pieces: list[str] = []
    for segment in re.split(r"(\s+|-)", name):
        if not segment or segment.isspace() or segment == "-":
            pieces.append(segment)
        elif looks_like_acronym(segment):
            pieces.append(segment)
        else:
            pieces.append(_capitalize_name_part(segment.lower()))
    return "".join(pieces)


def title_case_name(name: str, *, preserve_stored_casing: bool = False) -> str:
    """Title-case a name from normalized (often lowercased) lead data.

    When ``preserve_stored_casing`` is True (company names), keep the stored
    string if it already has meaningful caps; otherwise title-case it.

    Person names preserve operator- or source-supplied mixed case (McHugh),
    repair common naive Mc mis-casing (Mchugh), and apply intercap heuristics
    for all-lowercase tokens (mchugh -> McHugh, o'brien -> O'Brien).
    """
    name = (name or "").strip()
    if not name:
        return name
    if preserve_stored_casing and name != name.lower():
        return name
    if _has_mixed_case(name):
        return _repair_naive_mc_casing(name)

    pieces: list[str] = []
    for segment in re.split(r"(\s+|-)", name):
        if not segment or segment.isspace() or segment == "-":
            pieces.append(segment)
        else:
            pieces.append(_capitalize_name_part(segment))
    return "".join(pieces)
