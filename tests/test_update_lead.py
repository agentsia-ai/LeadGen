"""Tests for operator-facing update_lead contact writes."""

from __future__ import annotations

import pytest

from leadgen.crm.database import LeadDatabase
from leadgen.crm.update_lead import update_lead
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource, LeadStatus


def _lead(
    email: str | None = "x@y.com",
    name: str = "Acme",
    domain: str | None = "acme.com",
    full_name: str | None = "Jane Doe",
) -> Lead:
    return Lead(
        source=LeadSource.MANUAL,
        contact=ContactInfo(
            full_name=full_name,
            first_name="Jane",
            last_name="Doe",
            email=email,
            email_verified=bool(email),
        ),
        company=CompanyInfo(name=name, domain=domain),
    )


@pytest.mark.asyncio
async def test_update_lead_sets_email_and_domain_with_unverified_default(
    initialized_db: LeadDatabase,
) -> None:
    lead = _lead(email=None, name="Laricy", full_name="matt laricy")
    await initialized_db.upsert(lead)

    result = await update_lead(
        initialized_db,
        lead.id,
        email="mlaricy@americorpre.com",
        domain="americorpre.com",
        note="manually found on company contact page",
    )
    assert result["updated"] is True
    assert result["email"] == "mlaricy@americorpre.com"
    assert result["email_verified"] is False
    assert result["domain"] == "americorpre.com"

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    assert reread.contact.email == "mlaricy@americorpre.com"
    assert reread.contact.email_verified is False
    assert reread.company.domain == "americorpre.com"
    assert "manually found on company contact page" in reread.notes


@pytest.mark.asyncio
async def test_update_lead_honors_explicit_email_verified(
    initialized_db: LeadDatabase,
) -> None:
    lead = _lead(email=None, name="Acme")
    await initialized_db.upsert(lead)

    result = await update_lead(
        initialized_db,
        lead.id,
        email="found@acme.com",
        email_verified=True,
        status="enriched",
    )
    assert result["updated"] is True
    assert result["email_verified"] is True
    assert result["status"] == "enriched"

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    assert reread.status == LeadStatus.ENRICHED


@pytest.mark.asyncio
async def test_update_lead_reports_email_collision(
    initialized_db: LeadDatabase,
) -> None:
    holder = _lead(email="taken@x.com", name="Holder")
    other = _lead(email=None, name="Other")
    await initialized_db.upsert(holder)
    await initialized_db.upsert(other)

    result = await update_lead(
        initialized_db,
        other.id,
        email="taken@x.com",
    )
    assert result["updated"] is False
    assert result["status"] == "collision"
    assert result["existing_lead_id"] == holder.id
    assert "collision: email already on lead" in result["reason"]


@pytest.mark.asyncio
async def test_update_lead_sets_phone(initialized_db: LeadDatabase) -> None:
    lead = _lead(email="a@x.com", name="Acme")
    await initialized_db.upsert(lead)

    result = await update_lead(initialized_db, lead.id, phone="+1-555-0100")
    assert result["updated"] is True
    assert result["phone"] == "+1-555-0100"

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    assert reread.contact.phone == "+1-555-0100"


@pytest.mark.asyncio
async def test_update_lead_name_propagates_to_contact_fields(
    initialized_db: LeadDatabase,
) -> None:
    lead = _lead(
        email="rachel@example.com",
        name="McHugh Law",
        full_name="Luann McHugh",
    )
    lead.contact.first_name = "Luann"
    lead.contact.last_name = "McHugh"
    await initialized_db.upsert(lead)

    result = await update_lead(
        initialized_db,
        lead.id,
        full_name="rachel mchugh",
    )
    assert result["updated"] is True
    assert result["name"] == "Rachel Mchugh"
    assert result["first_name"] == "Rachel"
    assert result["last_name"] == "Mchugh"
    assert result["full_name"] == "Rachel Mchugh"

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    assert reread.display_name == "Rachel Mchugh"
    assert reread.contact.first_name == "Rachel"
    assert reread.contact.last_name == "Mchugh"
    assert reread.contact.full_name == "Rachel Mchugh"


@pytest.mark.asyncio
async def test_update_lead_name_greeting_uses_correct_first_name(
    initialized_db: LeadDatabase,
    test_config,
    test_keys,
) -> None:
    from leadgen.ai.drafter import OutreachDrafter

    lead = _lead(email="rachel@example.com", name="McHugh Law", full_name="Luann McHugh")
    lead.contact.first_name = "Luann"
    lead.contact.last_name = "McHugh"
    await initialized_db.upsert(lead)

    await update_lead(initialized_db, lead.id, first_name="Rachel")

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = ""
    drafter = OutreachDrafter(test_config, test_keys)
    body = drafter._format_body("Quick note about your firm.", reread)
    assert body.startswith("Hi Rachel,")


@pytest.mark.asyncio
async def test_update_lead_manual_verify_records_provenance(
    initialized_db: LeadDatabase,
) -> None:
    lead = _lead(
        email="wendy@wcalvertlaw.com",
        name="Calvert Law",
        full_name="Wendy Calvert",
    )
    lead.contact.email_verified = False
    await initialized_db.upsert(lead)

    result = await update_lead(
        initialized_db,
        lead.id,
        email_verified=True,
        verification_source="manual: published on site",
    )
    assert result["updated"] is True
    assert result["email_verified"] is True
    assert result["email_verification_source"] == "manual: published on site"

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    assert reread.contact.email_verified is True
    assert reread.contact.email_verification_source == "manual: published on site"
    assert reread.contact.email == "wendy@wcalvertlaw.com"


@pytest.mark.asyncio
async def test_update_lead_explicit_email_verified_defaults_manual_source(
    initialized_db: LeadDatabase,
) -> None:
    lead = _lead(email=None, name="Acme")
    await initialized_db.upsert(lead)

    await update_lead(
        initialized_db,
        lead.id,
        email="found@acme.com",
        email_verified=True,
    )

    reread = await initialized_db.get(lead.id)
    assert reread is not None
    assert reread.contact.email_verification_source == "manual"
