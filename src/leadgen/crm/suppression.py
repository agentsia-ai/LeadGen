"""Lead suppression — permanent opt-out from fetch/enrich/contact.

Suppression is keyed on email-independent identity (name + company, same as
dedup) and optionally on company domain. Records live in a separate table so
they survive lead-row deletion (cleanup delete must NOT add suppressions).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from leadgen.models import Lead, LeadStatus

if TYPE_CHECKING:
    from leadgen.crm.database import LeadDatabase

SUPPRESSION_STATUSES = frozenset({
    LeadStatus.CLOSED_LOST,
    LeadStatus.UNSUBSCRIBED,
})

SUPPRESSION_TAGS = frozenset({
    "replied-no",
    "do-not-contact",
    "dnc",
})


def lead_triggers_suppression(lead: Lead) -> str | None:
    """Return the suppression reason if *lead* should be on the suppression set."""
    if lead.status in SUPPRESSION_STATUSES:
        return lead.status.value
    for tag in lead.tags:
        if tag.lower() in SUPPRESSION_TAGS:
            return tag.lower()
    return None


async def sync_suppression_from_lead(db: LeadDatabase, lead: Lead) -> bool:
    """Add *lead* to the suppression set when status/tags warrant it.

    Only the name+company identity key is recorded — not the domain — so
    closing one person does not suppress an entire company. Domain-level
    blocks are operator-initiated via the suppress CLI.

    Returns True if a new suppression row was inserted.
    """
    reason = lead_triggers_suppression(lead)
    if not reason:
        return False

    key = db.identity_key_from_lead(lead)
    if not key:
        return False
    return await db.add_suppression(
        key,
        reason,
        source_lead_id=lead.id,
        display_name=lead.display_name,
        company_name=lead.company.name,
    )


async def check_lead_suppressed(db: LeadDatabase, lead: Lead) -> tuple[bool, str | None]:
    """Return (True, reason) if *lead* matches the suppression set."""
    return await db.check_lead_suppressed(lead)
