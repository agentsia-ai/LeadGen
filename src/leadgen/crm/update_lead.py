"""Operator-facing lead contact updates — manual email/domain/phone writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from leadgen.crm.database import EmailCollisionError, LeadDatabase
from leadgen.crm.suppression import sync_suppression_from_lead
from leadgen.models import LeadStatus

if TYPE_CHECKING:
    from leadgen.sources.hunter import HunterConnector


async def update_lead(
    db: LeadDatabase,
    lead_id: str,
    *,
    email: str | None = None,
    domain: str | None = None,
    phone: str | None = None,
    email_verified: bool | None = None,
    status: str | None = None,
    note: str | None = None,
    verify: bool = False,
    hunter: HunterConnector | None = None,
) -> dict[str, Any]:
    """Apply operator-supplied contact corrections to an existing lead.

    Only non-``None`` kwargs are written; omitted fields are left untouched.
    When ``email`` is set without an explicit ``email_verified``, verification
    defaults to ``False`` so manual/scraped addresses are not auto-trusted.
    """
    lead = await db.get(lead_id)
    if not lead:
        return {"error": "Lead not found", "lead_id": lead_id}

    if all(
        v is None for v in (email, domain, phone, email_verified, status, note)
    ) and not verify:
        return {"error": "No fields to update", "lead_id": lead_id}

    updated_fields: list[str] = []

    if domain is not None:
        lead.company.domain = domain
        updated_fields.append("domain")

    if phone is not None:
        lead.contact.phone = phone
        updated_fields.append("phone")

    if email is not None:
        lead.contact.email = email
        updated_fields.append("email")
        # Manual email writes are unverified unless the operator says otherwise
        # or opts into Hunter verification below.
        if email_verified is None and not verify:
            lead.contact.email_verified = False
            updated_fields.append("email_verified")

    if verify:
        target_email = email if email is not None else lead.contact.email
        if not target_email:
            return {
                "error": "verify requires an email on the lead or an email argument",
                "lead_id": lead_id,
            }
        if hunter is None:
            return {
                "error": "HUNTER_API_KEY is not set — cannot run email verification",
                "lead_id": lead_id,
            }
        lead.contact.email_verified = await hunter.verify_email(target_email)
        updated_fields.append("email_verified")
    elif email_verified is not None:
        lead.contact.email_verified = email_verified
        updated_fields.append("email_verified")

    if status is not None:
        lead.status = LeadStatus(status)
        updated_fields.append("status")

    if note is not None:
        lead.notes = (lead.notes + "\n" + note).strip()
        updated_fields.append("note")

    lead.touch()
    await sync_suppression_from_lead(db, lead)
    try:
        await db.upsert(lead)
    except EmailCollisionError as exc:
        return {
            "updated": False,
            "lead_id": lead_id,
            "status": "collision",
            "email": exc.email,
            "reason": f"collision: email already on lead {exc.existing_lead_id}",
            "existing_lead_id": exc.existing_lead_id,
        }

    return {
        "updated": True,
        "lead_id": lead.id,
        "name": lead.display_name,
        "email": lead.contact.email,
        "email_verified": lead.contact.email_verified,
        "domain": lead.company.domain,
        "phone": lead.contact.phone,
        "status": lead.status.value,
        "updated_fields": updated_fields,
    }
