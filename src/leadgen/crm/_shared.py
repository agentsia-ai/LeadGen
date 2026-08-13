"""Backend-independent pieces shared by the SQLite and D1 lead stores.

Both backends persist the *identical* schema. They differ only in how a row
comes back — ``aiosqlite`` yields positional tuples, D1 yields column-keyed
objects — and in how writes are grouped (see ``leadgen.crm.d1``). Keeping row
(de)serialization and dedup-identity logic in one place means a schema or
identity change cannot silently drift between the two adapters, which would
show up as leads deduping differently in local dev than in production.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from leadgen._time import parse_iso
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
    OutreachRecord,
    ScoringBreakdown,
)

# Column order of `leads`, matching the CREATE TABLE in both backends. The
# positional INSERT and the tuple unpacking of `SELECT *` both depend on this
# order, so it is declared once here.
LEAD_COLUMNS: tuple[str, ...] = (
    "id",
    "source",
    "status",
    "contact_json",
    "company_json",
    "score_json",
    "outreach_json",
    "notes",
    "tags_json",
    "raw_data_json",
    "created_at",
    "updated_at",
    "company_name",
    "contact_email",
    "score_total",
)

# Columns written by the UPDATE branch of upsert(), in statement order.
LEAD_UPDATE_COLUMNS: tuple[str, ...] = (
    "status",
    "contact_json",
    "company_json",
    "score_json",
    "outreach_json",
    "notes",
    "tags_json",
    "updated_at",
    "score_total",
    "company_name",
    "contact_email",
)


def lead_from_mapping(row: Mapping[str, Any]) -> Lead:
    """Build a :class:`Lead` from a column-keyed row.

    Tolerates NULL in the JSON/text columns that carry a schema DEFAULT: rows
    imported into D1 from a `.dump` can arrive with an explicit NULL where the
    default would otherwise have applied.
    """
    return Lead(
        id=row["id"],
        source=LeadSource(row["source"]),
        status=LeadStatus(row["status"]),
        contact=ContactInfo(**json.loads(row["contact_json"])),
        company=CompanyInfo(**json.loads(row["company_json"])),
        score=(
            ScoringBreakdown(**json.loads(row["score_json"]))
            if row.get("score_json")
            else None
        ),
        outreach_history=[
            OutreachRecord(**r) for r in json.loads(row.get("outreach_json") or "[]")
        ],
        notes=row.get("notes") or "",
        tags=json.loads(row.get("tags_json") or "[]"),
        raw_data=json.loads(row.get("raw_data_json") or "{}"),
        created_at=parse_iso(row["created_at"]),
        updated_at=parse_iso(row["updated_at"]),
    )


def lead_from_row(row: Sequence[Any]) -> Lead:
    """Build a :class:`Lead` from a positional ``SELECT *`` tuple."""
    return lead_from_mapping(dict(zip(LEAD_COLUMNS, row, strict=False)))


def lead_insert_values(lead: Lead) -> list[Any]:
    """Positional bind values for a full-row INSERT, in ``LEAD_COLUMNS`` order."""
    return [
        lead.id,
        lead.source.value,
        lead.status.value,
        lead.contact.model_dump_json(),
        lead.company.model_dump_json(),
        lead.score.model_dump_json() if lead.score else None,
        json.dumps([r.model_dump(mode="json") for r in lead.outreach_history]),
        lead.notes,
        json.dumps(lead.tags),
        json.dumps(lead.raw_data),
        lead.created_at.isoformat(),
        lead.updated_at.isoformat(),
        lead.company.name,
        lead.contact.email,
        lead.score.total if lead.score else None,
    ]


def lead_update_values(lead: Lead) -> list[Any]:
    """Positional bind values for the UPDATE branch, in ``LEAD_UPDATE_COLUMNS`` order.

    ``created_at`` and ``id`` are deliberately absent: an update must never
    rewrite a lead's creation time or primary key.
    """
    return [
        lead.status.value,
        lead.contact.model_dump_json(),
        lead.company.model_dump_json(),
        lead.score.model_dump_json() if lead.score else None,
        json.dumps([r.model_dump(mode="json") for r in lead.outreach_history]),
        lead.notes,
        json.dumps(lead.tags),
        lead.updated_at.isoformat(),
        lead.score.total if lead.score else None,
        lead.company.name,
        lead.contact.email,
    ]


class LeadIdentityMixin:
    """Dedup-identity and suppression-key logic, shared by both backends.

    Implementors must provide an async ``is_suppression_key(key)`` returning
    ``(bool, reason)``.
    """

    @staticmethod
    def _name_company_key(contact: dict, company: dict) -> str | None:
        """Email-INDEPENDENT identity key: person name + company.

        Deliberately ignores email so the *same person at the same company*
        collapses to one row regardless of whether the incoming email is
        null. PDL's free tier returns null emails, so an email-bearing key
        let an already-known (enriched) person look brand-new when PDL
        re-returned them with ``email=None`` — stacking duplicate rows and
        wasting PDL/Hunter credits re-fetching someone we already had.

        Returns None unless BOTH a person name and a company name are
        present: matching on company alone would wrongly merge different
        people at the same firm, and matching on name alone would merge
        namesakes across companies.

        Shared by `upsert` (insert-time dedupe) and `find_duplicates` /
        `delete_duplicates` (cleanup) so prevention and cure use exactly the
        same notion of identity.
        """
        first = contact.get("first_name") or ""
        last = contact.get("last_name") or ""
        full_name = (contact.get("full_name") or f"{first} {last}").strip()
        company_name = (company.get("name") or "").strip()
        if not full_name or not company_name:
            return None
        return f"name:{full_name.lower()}|company:{company_name.lower()}"

    @staticmethod
    def domain_suppression_key(domain: str) -> str:
        """Suppression key for an entire company domain."""
        return f"domain:{domain.lower().strip()}"

    def identity_key_from_lead(self, lead: Lead) -> str | None:
        """Email-independent identity key for a Lead model instance."""
        return self._name_company_key(
            lead.contact.model_dump(), lead.company.model_dump()
        )

    async def check_lead_suppressed(self, lead: Lead) -> tuple[bool, str | None]:
        """Check name+company identity and company domain against suppressions."""
        identity_key = self.identity_key_from_lead(lead)
        if identity_key:
            suppressed, reason = await self.is_suppression_key(identity_key)
            if suppressed:
                return True, reason
        if lead.company.domain:
            suppressed, reason = await self.is_suppression_key(
                self.domain_suppression_key(lead.company.domain)
            )
            if suppressed:
                return True, reason
        return False, None
