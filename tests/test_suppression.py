"""Suppression set tests — permanent opt-out from fetch/enrich."""

from __future__ import annotations

import pytest

from leadgen.crm.database import LeadDatabase
from leadgen.crm.suppression import (
    check_lead_suppressed,
    sync_suppression_from_lead,
)
from leadgen.crm.update_lead import update_lead
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
)


def _lead(
    email: str | None = "x@y.com",
    name: str = "Acme Corp",
    full_name: str = "Jane Doe",
    status: LeadStatus = LeadStatus.NEW,
    domain: str | None = "acme.com",
) -> Lead:
    return Lead(
        source=LeadSource.PDL,
        status=status,
        contact=ContactInfo(
            full_name=full_name,
            first_name="Jane",
            last_name="Doe",
            email=email,
        ),
        company=CompanyInfo(name=name, domain=domain),
    )


@pytest.mark.asyncio
async def test_closed_lost_adds_identity_to_suppression_set(
    initialized_db: LeadDatabase,
) -> None:
    """Marking closed_lost records the name+company key in suppressions."""
    lead = _lead()
    await initialized_db.upsert(lead)

    await update_lead(initialized_db, lead.id, status="closed_lost")

    key = initialized_db.identity_key_from_lead(lead)
    assert key is not None
    suppressed, reason = await initialized_db.is_suppression_key(key)
    assert suppressed is True
    assert reason == "closed_lost"


@pytest.mark.asyncio
async def test_subsequent_pull_skips_suppressed_identity_before_insert(
    initialized_db: LeadDatabase,
) -> None:
    """A re-pulled lead with the same name+company is blocked pre-insert."""
    original = _lead(email="jane@acme.com")
    await initialized_db.upsert(original)
    await update_lead(initialized_db, original.id, status="closed_lost")
    await initialized_db.delete_by_ids([original.id])
    assert await initialized_db.count_all() == 0

    # PDL-style re-fetch: fresh id, null email — suppression survives deletion
    refetch = _lead(email=None, status=LeadStatus.NEW)
    refetch.id = "fresh-pdl-uuid"

    blocked, reason = await check_lead_suppressed(initialized_db, refetch)
    assert blocked is True
    assert reason == "closed_lost"

    # Fetch path skips upsert when suppressed — no row re-inserted
    assert await initialized_db.count_all() == 0


@pytest.mark.asyncio
async def test_deleted_new_lead_is_not_suppressed(
    initialized_db: LeadDatabase,
) -> None:
    """Cleanup delete of an unworked new lead does not add a suppression."""
    lead = _lead(email=None, status=LeadStatus.NEW)
    await initialized_db.upsert(lead)
    key = initialized_db.identity_key_from_lead(lead)
    assert key is not None

    await initialized_db.delete_by_ids([lead.id])
    assert await initialized_db.get(lead.id) is None

    suppressed, _ = await initialized_db.is_suppression_key(key)
    assert suppressed is False

    refetch = _lead(email=None, status=LeadStatus.NEW)
    refetch.id = "another-fresh-uuid"
    blocked, _ = await check_lead_suppressed(initialized_db, refetch)
    assert blocked is False


@pytest.mark.asyncio
async def test_domain_suppression_blocks_fetch_before_insert(
    initialized_db: LeadDatabase,
) -> None:
    """Manual domain suppress skips any lead at that domain pre-insert."""
    await initialized_db.add_suppression(
        LeadDatabase.domain_suppression_key("junkco.com"),
        "icp_mismatch",
        company_name="junkco.com",
    )

    incoming = _lead(
        email=None,
        name="JunkCo",
        full_name="Bob Smith",
        domain="junkco.com",
    )
    blocked, reason = await check_lead_suppressed(initialized_db, incoming)
    assert blocked is True
    assert reason == "icp_mismatch"


@pytest.mark.asyncio
async def test_replied_no_tag_triggers_suppression(
    initialized_db: LeadDatabase,
) -> None:
    """A replied-no tag adds the lead identity to the suppression set."""
    lead = _lead(status=LeadStatus.RESPONDED)
    lead.tags = ["replied-no"]
    await initialized_db.upsert(lead)

    added = await sync_suppression_from_lead(initialized_db, lead)
    assert added is True

    key = initialized_db.identity_key_from_lead(lead)
    suppressed, reason = await initialized_db.is_suppression_key(key)
    assert suppressed is True
    assert reason == "replied-no"
