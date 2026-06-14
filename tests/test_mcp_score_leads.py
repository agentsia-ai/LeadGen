"""MCP score_leads targeting — offline, mocked scorer (no LLM credits)."""

from __future__ import annotations

import json

import pytest

from leadgen._time import now_utc
from leadgen.mcp_server import server as mcp_server
from leadgen.models import LeadStatus, ScoringBreakdown


class _MockScorer:
    """Stand-in for LeadScorer — attaches a fixed score without calling Claude."""

    def __init__(self, config, keys):
        pass

    async def score_batch(self, leads, min_score=None):
        for lead in leads:
            lead.score = ScoringBreakdown(
                total=0.85,
                reasoning="mock score",
                scored_at=now_utc(),
            )
            lead.touch()
        return leads


@pytest.fixture
def mcp_env(initialized_db, test_config, test_keys, monkeypatch):
    monkeypatch.setattr(mcp_server, "config", test_config)
    monkeypatch.setattr(mcp_server, "keys", test_keys)
    monkeypatch.setattr(mcp_server, "db", initialized_db)
    monkeypatch.setattr(mcp_server, "SCORER_CLASS", _MockScorer)
    return initialized_db


@pytest.mark.asyncio
async def test_score_leads_by_id_scores_enriched_lead_preserves_contact(
    mcp_env, sample_lead
) -> None:
    """Targeted scoring must work on enriched leads and keep verified email."""
    sample_lead.status = LeadStatus.ENRICHED
    await mcp_env.upsert(sample_lead)
    email_before = sample_lead.contact.email
    verified_before = sample_lead.contact.email_verified

    result = await mcp_server.call_tool(
        "score_leads", {"lead_id": sample_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload["leads_scored"] == 1
    assert payload["passed_threshold"] == 1

    stored = await mcp_env.get(sample_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.SCORED
    assert stored.contact.email == email_before
    assert stored.contact.email_verified is verified_before
    assert stored.score is not None
    assert stored.score.total == 0.85


@pytest.mark.asyncio
async def test_score_leads_without_ids_still_sweeps_new_only(
    mcp_env, sample_lead
) -> None:
    """Default sweep behavior: only status=new leads, up to limit."""
    sample_lead.status = LeadStatus.ENRICHED
    await mcp_env.upsert(sample_lead)

    new_lead = sample_lead.model_copy(deep=True)
    new_lead.id = "new-only-lead"
    new_lead.status = LeadStatus.NEW
    new_lead.contact = new_lead.contact.model_copy(update={"email": "other@acmecorp.com"})
    await mcp_env.upsert(new_lead)

    result = await mcp_server.call_tool("score_leads", {"limit": 10})
    payload = json.loads(result[0].text)

    assert payload["leads_scored"] == 1

    enriched = await mcp_env.get(sample_lead.id)
    scored_new = await mcp_env.get(new_lead.id)
    assert enriched.status == LeadStatus.ENRICHED
    assert scored_new.status == LeadStatus.SCORED
