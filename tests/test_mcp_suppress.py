"""MCP suppress_lead / unsuppress_lead — offline, no external APIs."""

from __future__ import annotations

import json

import pytest

from leadgen.crm.suppression import check_lead_suppressed
from leadgen.mcp_server import server as mcp_server
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource, LeadStatus


@pytest.fixture
def mcp_env(initialized_db, test_config, test_keys, monkeypatch):
    monkeypatch.setattr(mcp_server, "config", test_config)
    monkeypatch.setattr(mcp_server, "keys", test_keys)
    monkeypatch.setattr(mcp_server, "db", initialized_db)
    return initialized_db


def _lead(
    *,
    lead_id: str = "lead-1",
    full_name: str = "Jane Doe",
    company: str = "Acme Corp",
    domain: str = "acmecorp.com",
    email: str | None = "jane@acmecorp.com",
) -> Lead:
    return Lead(
        id=lead_id,
        source=LeadSource.PDL,
        status=LeadStatus.NEW,
        contact=ContactInfo(
            full_name=full_name,
            first_name="Jane",
            last_name="Doe",
            email=email,
        ),
        company=CompanyInfo(name=company, domain=domain),
    )


@pytest.mark.asyncio
async def test_suppress_lead_via_lead_id(mcp_env, sample_lead) -> None:
    await mcp_env.upsert(sample_lead)

    result = await mcp_server.call_tool(
        "suppress_lead",
        {"lead_id": sample_lead.id, "reason": "icp_mismatch"},
    )
    payload = json.loads(result[0].text)

    assert payload["added"] is True
    assert payload["reason"] == "icp_mismatch"
    assert payload["display_name"] == sample_lead.display_name
    assert payload["company_name"] == sample_lead.company.name
    assert payload["suppression_key"].startswith("name:")

    suppressed, reason = await mcp_env.is_suppression_key(payload["suppression_key"])
    assert suppressed is True
    assert reason == "icp_mismatch"


@pytest.mark.asyncio
async def test_suppress_lead_via_name_company(mcp_env) -> None:
    result = await mcp_server.call_tool(
        "suppress_lead",
        {
            "name": "Bob Smith",
            "company": "JunkCo",
            "reason": "exhausted",
        },
    )
    payload = json.loads(result[0].text)

    assert payload["added"] is True
    assert payload["reason"] == "exhausted"
    assert payload["display_name"] == "Bob Smith"
    assert payload["company_name"] == "JunkCo"

    incoming = _lead(
        lead_id="fresh-id",
        full_name="Bob Smith",
        company="JunkCo",
        domain="junkco.com",
        email=None,
    )
    blocked, reason = await check_lead_suppressed(mcp_env, incoming)
    assert blocked is True
    assert reason == "exhausted"


@pytest.mark.asyncio
async def test_unsuppress_lead_reverses(mcp_env, sample_lead) -> None:
    await mcp_env.upsert(sample_lead)

    suppress = await mcp_server.call_tool(
        "suppress_lead",
        {"lead_id": sample_lead.id, "reason": "manual"},
    )
    key = json.loads(suppress[0].text)["suppression_key"]

    unsuppress = await mcp_server.call_tool(
        "unsuppress_lead",
        {"suppression_key": key},
    )
    payload = json.loads(unsuppress[0].text)
    assert payload["removed"] is True
    assert payload["suppression_key"] == key

    blocked, _ = await check_lead_suppressed(mcp_env, sample_lead)
    assert blocked is False


@pytest.mark.asyncio
async def test_suppress_requires_exactly_one_identifier(mcp_env) -> None:
    result = await mcp_server.call_tool(
        "suppress_lead",
        {"lead_id": "x", "domain": "example.com"},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


def _mock_apollo_connector(leads: list[Lead]):
    class MockApollo:
        def __init__(self, config, keys):
            self._leads = leads

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, limit=25):
            return self._leads[:limit]

    return MockApollo


@pytest.mark.asyncio
async def test_suppressed_lead_skipped_on_re_fetch(
    mcp_env, sample_lead, monkeypatch
) -> None:
    await mcp_env.upsert(sample_lead)

    await mcp_server.call_tool(
        "suppress_lead",
        {"lead_id": sample_lead.id, "reason": "replied_no"},
    )

    refetch = sample_lead.model_copy(deep=True)
    refetch.id = "pdl-refetch-uuid"
    refetch.contact = refetch.contact.model_copy(update={"email": None})

    monkeypatch.setattr(
        "leadgen.sources.apollo.ApolloConnector",
        _mock_apollo_connector([refetch]),
    )

    result = await mcp_server.call_tool("fetch_new_leads", {"limit": 5})
    payload = json.loads(result[0].text)

    assert payload["fetched"] == 1
    assert payload["suppressed_skipped"] == 1
    assert payload["new_leads_added"] == 0
    assert await mcp_env.count_all() == 1

    unsuppress = await mcp_server.call_tool(
        "unsuppress_lead",
        {"lead_id": sample_lead.id},
    )
    assert json.loads(unsuppress[0].text)["removed"] is True

    result2 = await mcp_server.call_tool("fetch_new_leads", {"limit": 5})
    payload2 = json.loads(result2[0].text)

    assert payload2["suppressed_skipped"] == 0
    assert payload2["new_leads_added"] == 0
    assert payload2["fetched"] == 1

    blocked, _ = await check_lead_suppressed(mcp_env, refetch)
    assert blocked is False
