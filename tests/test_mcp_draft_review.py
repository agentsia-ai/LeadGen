"""MCP draft-review loop fixes — offline, no LLM or real SMTP."""

from __future__ import annotations

import json

import pytest

from leadgen._time import now_utc
from leadgen.mcp_server import server as mcp_server
from leadgen.models import LeadStatus, OutreachRecord, ScoringBreakdown


@pytest.fixture
def mcp_env(initialized_db, test_config, test_keys, monkeypatch):
    monkeypatch.setattr(mcp_server, "config", test_config)
    monkeypatch.setattr(mcp_server, "keys", test_keys)
    monkeypatch.setattr(mcp_server, "db", initialized_db)
    return initialized_db


@pytest.mark.asyncio
async def test_get_lead_detail_serializes_scored_at(mcp_env, scored_lead) -> None:
    """get_lead_detail must not blow up when score.scored_at is set."""
    scored_lead.score.scored_at = now_utc()
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "get_lead_detail", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload["score"]["total"] == 0.82
    assert payload["score"]["scored_at"] is not None
    assert isinstance(payload["score"]["scored_at"], str)


@pytest.mark.asyncio
async def test_get_lead_detail_returns_full_outreach_body(mcp_env, scored_lead) -> None:
    """Operator must be able to read the entire drafted message from lead detail."""
    long_body = "A" * 350 + "\n\nFooter line with signature."
    record = OutreachRecord(
        subject="Quick intro",
        body=long_body,
        sequence_step=0,
    )
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.QUEUED
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "get_lead_detail", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload["outreach_count"] == 1
    assert len(payload["outreach_history"]) == 1
    assert payload["outreach_history"][0]["body"] == long_body
    assert payload["outreach_history"][0]["subject"] == "Quick intro"
    assert isinstance(payload["outreach_history"][0]["drafted_at"], str)


@pytest.mark.asyncio
async def test_send_test_draft_delivers_without_mutating_lead(
    mcp_env, scored_lead, monkeypatch
) -> None:
    """Test-send goes to an override address and leaves the lead record intact."""
    record = OutreachRecord(subject="Hello", body="Full draft body", sequence_step=0)
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.QUEUED
    await mcp_env.upsert(scored_lead)

    sent_to: list[str] = []

    async def fake_send(self, to_email, to_name, subject, body):
        sent_to.append(to_email)
        assert subject == "[TEST] Hello"
        assert body == "Full draft body"
        return True

    monkeypatch.setattr(
        "leadgen.outreach.email.EmailSender._send",
        fake_send,
    )

    result = await mcp_server.call_tool(
        "send_test_draft",
        {
            "lead_id": scored_lead.id,
            "test_recipient": "dev@example.com",
        },
    )
    payload = json.loads(result[0].text)

    assert payload["test_sent"] is True
    assert payload["test_recipient"] == "dev@example.com"
    assert payload["lead_unchanged"] is True
    assert sent_to == ["dev@example.com"]

    stored = await mcp_env.get(scored_lead.id)
    assert stored.contact.email == scored_lead.contact.email
    assert stored.status == LeadStatus.QUEUED
    assert stored.outreach_history[0].sent_at is None


@pytest.mark.asyncio
async def test_draft_outreach_supersedes_prior_unapproved_at_same_step(
    mcp_env, scored_lead, monkeypatch
) -> None:
    """Re-drafting must leave one pending draft per sequence_step."""
    stale_1 = OutreachRecord(subject="old #1", body="stale one", sequence_step=0)
    stale_2 = OutreachRecord(subject="old #2", body="stale two", sequence_step=0)
    approved = OutreachRecord(
        subject="approved",
        body="keep",
        sequence_step=0,
        approved_at=now_utc(),
    )
    sent = OutreachRecord(
        subject="sent",
        body="sent body",
        sequence_step=0,
        approved_at=now_utc(),
        sent_at=now_utc(),
    )
    scored_lead.outreach_history = [stale_1, stale_2, approved, sent]
    await mcp_env.upsert(scored_lead)

    new_record = OutreachRecord(subject="fresh", body="latest draft", sequence_step=0)

    class FakeDrafter:
        def __init__(self, config, keys):
            pass

        async def draft_initial(self, lead):
            return new_record

    monkeypatch.setattr(mcp_server, "DRAFTER_CLASS", FakeDrafter)

    result = await mcp_server.call_tool(
        "draft_outreach", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload[0]["subject"] == "fresh"
    stored = await mcp_env.get(scored_lead.id)
    pending = [
        r for r in stored.outreach_history
        if r.approved_at is None and r.sent_at is None
    ]
    assert len(pending) == 1
    assert pending[0].subject == "fresh"
    assert len(stored.outreach_history) == 3  # fresh + approved + sent
