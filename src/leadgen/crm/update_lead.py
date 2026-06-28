"""Operator-facing lead contact updates — manual email/domain/phone writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from leadgen.crm.database import EmailCollisionError, LeadDatabase
from leadgen.crm.suppression import sync_suppression_from_lead
from leadgen.models import ContactInfo, LeadStatus
from leadgen.text import title_case_name

if TYPE_CHECKING:
    from leadgen.sources.hunter import HunterConnector


def _apply_contact_name_update(
    contact: ContactInfo,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    full_name: str | None = None,
) -> list[str]:
    """Title-case and keep first/last/full name fields consistent."""
    updated_fields: list[str] = []

    if first_name is not None or last_name is not None:
        if first_name is not None:
            contact.first_name = title_case_name(first_name) or None
            updated_fields.append("first_name")
        if last_name is not None:
            contact.last_name = title_case_name(last_name) or None
            updated_fields.append("last_name")
        parts = [contact.first_name, contact.last_name]
        contact.full_name = " ".join(p for p in parts if p) or None
        updated_fields.append("full_name")
    elif full_name is not None:
        contact.full_name = title_case_name(full_name) or None
        updated_fields.append("full_name")
        tokens = (contact.full_name or "").split(None, 1)
        contact.first_name = tokens[0] if tokens else None
        contact.last_name = tokens[1] if len(tokens) > 1 else None
        updated_fields.extend(["first_name", "last_name"])

    return updated_fields


async def update_lead(
    db: LeadDatabase,
    lead_id: str,
    *,
    email: str | None = None,
    domain: str | None = None,
    phone: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    full_name: str | None = None,
    email_verified: bool | None = None,
    verification_source: str | None = None,
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
        v is None
        for v in (
            email,
            domain,
            phone,
            first_name,
            last_name,
            full_name,
            email_verified,
            status,
            note,
        )
    ) and not verify:
        return {"error": "No fields to update", "lead_id": lead_id}

    updated_fields: list[str] = []

    name_updates = _apply_contact_name_update(
        lead.contact,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
    )
    updated_fields.extend(name_updates)

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
            lead.contact.email_verification_source = None
            updated_fields.append("email_verified")
            updated_fields.append("email_verification_source")

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
        verified = await hunter.verify_email(target_email)
        lead.contact.email_verified = verified
        lead.contact.email_verification_source = "hunter" if verified else None
        updated_fields.append("email_verified")
        updated_fields.append("email_verification_source")
    elif email_verified is not None:
        lead.contact.email_verified = email_verified
        if email_verified:
            lead.contact.email_verification_source = verification_source or "manual"
        else:
            lead.contact.email_verification_source = None
        updated_fields.append("email_verified")
        updated_fields.append("email_verification_source")

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
        "first_name": lead.contact.first_name,
        "last_name": lead.contact.last_name,
        "full_name": lead.contact.full_name,
        "email": lead.contact.email,
        "email_verified": lead.contact.email_verified,
        "email_verification_source": lead.contact.email_verification_source,
        "domain": lead.company.domain,
        "phone": lead.contact.phone,
        "status": lead.status.value,
        "updated_fields": updated_fields,
    }
