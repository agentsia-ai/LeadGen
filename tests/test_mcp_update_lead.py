"""MCP update_lead name correction — offline, no external APIs."""

from __future__ import annotations

import json

import pytest

from leadgen.mcp_server import server as mcp_server
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource, LeadStatus


@pytest.fixture
def mcp_env(initialized_db, test_config, test_keys, monkeypatch):
    monkeypatch.setattr(mcp_server, "config", test_config)
    monkeypatch.setattr(mcp_server, "keys", test_keys)
    monkeypatch.setattr(mcp_server, "db", initialized_db)
    return initialized_db


def _mchugh_lead() -> Lead:
    """PDL-style wrong contact name — Luann instead of principal Rachel."""
    return Lead(
        source=LeadSource.PDL,
        status=LeadStatus.ENRICHED,
        contact=ContactInfo(
            full_name="Luann McHugh",
            first_name="Luann",
            last_name="McHugh",
            email="rachel@mchughlaw.com",
            email_verified=True,
        ),
        company=CompanyInfo(name="McHugh Law", domain="mchughlaw.com"),
    )


@pytest.mark.asyncio
async def test_update_lead_tool_schema_exposes_name_params() -> None:
    """MCP clients must see first_name/last_name/full_name on update_lead."""
    tools = await mcp_server.list_tools()
    update_tool = next(t for t in tools if t.name == "update_lead")
    props = update_tool.inputSchema["properties"]

    assert "first_name" in props
    assert "last_name" in props
    assert "full_name" in props
    assert update_tool.inputSchema["required"] == ["lead_id"]


@pytest.mark.asyncio
async def test_mcp_update_lead_sets_contact_json_name(mcp_env) -> None:
    """Operator name correction via MCP writes contact_json and surfaces in detail."""
    lead = _mchugh_lead()
    await mcp_env.upsert(lead)

    update_result = await mcp_server.call_tool(
        "update_lead",
        {
            "lead_id": lead.id,
            "first_name": "Rachel",
            "last_name": "McHugh",
        },
    )
    update_payload = json.loads(update_result[0].text)

    assert update_payload["updated"] is True
    assert update_payload["name"] == "Rachel Mchugh"
    assert update_payload["first_name"] == "Rachel"
    assert update_payload["last_name"] == "Mchugh"
    assert update_payload["full_name"] == "Rachel Mchugh"
    assert "first_name" in update_payload["updated_fields"]
    assert "last_name" in update_payload["updated_fields"]
    assert "full_name" in update_payload["updated_fields"]

    reread = await mcp_env.get(lead.id)
    assert reread is not None
    assert reread.contact.first_name == "Rachel"
    assert reread.contact.last_name == "Mchugh"
    assert reread.contact.full_name == "Rachel Mchugh"
    assert reread.display_name == "Rachel Mchugh"

    detail_result = await mcp_server.call_tool(
        "get_lead_detail",
        {"lead_id": lead.id},
    )
    detail_payload = json.loads(detail_result[0].text)

    assert detail_payload["name"] == "Rachel Mchugh"


@pytest.mark.asyncio
async def test_mcp_update_lead_name_enables_rachel_greeting(
    mcp_env, test_config, test_keys
) -> None:
    """Corrected contact_json first_name flows into the drafter greeting."""
    from leadgen.ai.drafter import OutreachDrafter

    lead = _mchugh_lead()
    await mcp_env.upsert(lead)

    await mcp_server.call_tool(
        "update_lead",
        {"lead_id": lead.id, "first_name": "Rachel", "last_name": "McHugh"},
    )

    reread = await mcp_env.get(lead.id)
    assert reread is not None
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = ""
    drafter = OutreachDrafter(test_config, test_keys)
    body = drafter._format_body("Quick note about your firm.", reread)
    assert body.startswith("Hi Rachel,")
