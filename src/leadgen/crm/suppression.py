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


def _suppression_target_modes(
    *,
    lead_id: str | None = None,
    name: str | None = None,
    company: str | None = None,
    domain: str | None = None,
    suppression_key: str | None = None,
) -> int:
    """Count how many explicit identification modes were provided."""
    return sum(
        bool(x)
        for x in (
            lead_id,
            domain,
            name and company,
            suppression_key,
        )
    )


async def add_operator_suppression(
    db: LeadDatabase,
    *,
    lead_id: str | None = None,
    name: str | None = None,
    company: str | None = None,
    domain: str | None = None,
    reason: str = "manual",
) -> dict:
    """Add a manual suppression (MCP/CLI). One explicit target required."""
    if _suppression_target_modes(
        lead_id=lead_id, name=name, company=company, domain=domain
    ) != 1:
        return {
            "error": "Specify exactly one of: lead_id, domain, or both name and company.",
        }

    reason = reason or "manual"

    if lead_id:
        lead = await db.get(lead_id)
        if not lead:
            return {"error": f"Lead not found: {lead_id}"}
        key = db.identity_key_from_lead(lead)
        if not key:
            return {
                "error": "Lead lacks both person name and company — cannot suppress.",
            }
        added = await db.add_suppression(
            key,
            reason,
            source_lead_id=lead.id,
            display_name=lead.display_name,
            company_name=lead.company.name,
        )
        return {
            "added": added,
            "suppression_key": key,
            "reason": reason,
            "display_name": lead.display_name,
            "company_name": lead.company.name,
        }

    if domain:
        key = db.domain_suppression_key(domain)
        added = await db.add_suppression(
            key,
            reason,
            company_name=domain,
        )
        return {
            "added": added,
            "suppression_key": key,
            "reason": reason,
            "company_name": domain,
        }

    contact = {"full_name": name, "first_name": "", "last_name": ""}
    company_dict = {"name": company}
    key = db._name_company_key(contact, company_dict)
    if not key:
        return {"error": "name and company must both be non-empty."}
    added = await db.add_suppression(
        key,
        reason,
        display_name=name,
        company_name=company,
    )
    return {
        "added": added,
        "suppression_key": key,
        "reason": reason,
        "display_name": name,
        "company_name": company,
    }


async def remove_operator_suppression(
    db: LeadDatabase,
    *,
    suppression_key: str | None = None,
    lead_id: str | None = None,
    name: str | None = None,
    company: str | None = None,
    domain: str | None = None,
) -> dict:
    """Remove a manual suppression. One explicit target required."""
    if _suppression_target_modes(
        suppression_key=suppression_key,
        lead_id=lead_id,
        name=name,
        company=company,
        domain=domain,
    ) != 1:
        return {
            "error": (
                "Specify exactly one of: suppression_key, lead_id, domain, "
                "or both name and company."
            ),
        }

    if suppression_key:
        key = suppression_key
        label_company = None
        label_name = None
    elif lead_id:
        lead = await db.get(lead_id)
        if not lead:
            return {"error": f"Lead not found: {lead_id}"}
        key = db.identity_key_from_lead(lead)
        if not key:
            return {
                "error": "Lead lacks both person name and company — cannot unsuppress.",
            }
        label_name = lead.display_name
        label_company = lead.company.name
    elif domain:
        key = db.domain_suppression_key(domain)
        label_company = domain
        label_name = None
    else:
        contact = {"full_name": name, "first_name": "", "last_name": ""}
        company_dict = {"name": company}
        key = db._name_company_key(contact, company_dict)
        if not key:
            return {"error": "name and company must both be non-empty."}
        label_name = name
        label_company = company

    removed = await db.remove_suppression(key)
    result: dict = {"removed": removed, "suppression_key": key}
    if label_name:
        result["display_name"] = label_name
    if label_company:
        result["company_name"] = label_company
    return result
