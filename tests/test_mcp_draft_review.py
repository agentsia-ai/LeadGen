"""MCP draft-review loop fixes — offline, no LLM or real SMTP."""

from __future__ import annotations

import json

import pytest

from leadgen._time import now_utc
from leadgen.mcp_server import server as mcp_server
from leadgen.models import LeadStatus, OutreachRecord, ScoringBreakdown


@pytest.fixture
def mcp_env(initialized_db, test_config, test_keys, monkeypatch):
    test_keys.smtp_host = "smtp.test.com"
    test_keys.smtp_username = "smtp-user"
    test_keys.smtp_password = "smtp-pass"
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
async def test_send_outreach_requires_lead_id(mcp_env) -> None:
    result = await mcp_server.call_tool("send_outreach", {})
    payload = json.loads(result[0].text)
    assert "lead_id is required" in payload["error"]


@pytest.mark.asyncio
async def test_send_outreach_refuses_unapproved(mcp_env, scored_lead) -> None:
    record = OutreachRecord(subject="Hello", body="Draft body", sequence_step=0)
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.QUEUED
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "send_outreach", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert "not approved" in payload["error"]
    stored = await mcp_env.get(scored_lead.id)
    assert stored.outreach_history[0].sent_at is None


@pytest.mark.asyncio
async def test_send_outreach_refuses_already_sent(mcp_env, scored_lead) -> None:
    record = OutreachRecord(
        subject="Hello",
        body="Draft body",
        sequence_step=0,
        approved_at=now_utc(),
        sent_at=now_utc(),
    )
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.CONTACTED
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "send_outreach",
        {"lead_id": scored_lead.id, "outreach_id": record.id},
    )
    payload = json.loads(result[0].text)

    assert "already sent" in payload["error"]
    assert payload["sent_at"] is not None


@pytest.mark.asyncio
async def test_send_outreach_refuses_at_daily_cap(
    mcp_env, scored_lead, test_config, monkeypatch
) -> None:
    test_config.outreach.daily_email_limit = 1
    record = OutreachRecord(
        subject="Hello",
        body="Draft body",
        sequence_step=0,
        approved_at=now_utc(),
    )
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.QUEUED
    await mcp_env.upsert(scored_lead)

    async def fake_sync(self):
        self._sent_today = 1
        return 1

    monkeypatch.setattr(
        "leadgen.outreach.email.EmailSender.sync_sent_today",
        fake_sync,
    )

    result = await mcp_server.call_tool(
        "send_outreach", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert "Daily send limit reached" in payload["error"]
    assert payload["remaining"] == 0


@pytest.mark.asyncio
async def test_send_outreach_sends_approved_and_returns_audit_fields(
    mcp_env, scored_lead, monkeypatch
) -> None:
    record = OutreachRecord(
        subject="Hello",
        body="Production body",
        sequence_step=0,
        approved_at=now_utc(),
    )
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.QUEUED
    await mcp_env.upsert(scored_lead)

    sent_to: list[str] = []

    async def fake_send(self, to_email, to_name, subject, body):
        sent_to.append(to_email)
        assert subject == "Hello"
        assert body == "Production body"
        return True

    monkeypatch.setattr(
        "leadgen.outreach.email.EmailSender._send",
        fake_send,
    )

    result = await mcp_server.call_tool(
        "send_outreach",
        {"lead_id": scored_lead.id, "dry_run": False},
    )
    payload = json.loads(result[0].text)

    assert payload["sent"] is True
    assert payload["recipient"] == scored_lead.contact.email
    assert payload["sent_at"] is not None
    assert payload["status"] == LeadStatus.CONTACTED.value
    assert payload["outreach_id"] == record.id
    assert sent_to == [scored_lead.contact.email]

    stored = await mcp_env.get(scored_lead.id)
    assert stored.status == LeadStatus.CONTACTED
    assert stored.outreach_history[0].sent_at is not None


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


@pytest.mark.asyncio
async def test_draft_followup_requires_lead_id(mcp_env) -> None:
    result = await mcp_server.call_tool("draft_followup", {})
    payload = json.loads(result[0].text)
    assert "lead_id is required" in payload["error"]


@pytest.mark.asyncio
async def test_draft_followup_refuses_without_sent_outreach(mcp_env, scored_lead) -> None:
    await mcp_env.upsert(scored_lead)
    result = await mcp_server.call_tool(
        "draft_followup", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)
    assert "No sent outreach" in payload["error"]


@pytest.mark.asyncio
async def test_draft_followup_refuses_at_max(mcp_env, scored_lead, test_config) -> None:
    test_config.outreach.max_follow_ups = 2
    now = now_utc()
    scored_lead.outreach_history = [
        OutreachRecord(subject="#0", body="a", sent_at=now, sequence_step=0),
        OutreachRecord(subject="#1", body="b", sent_at=now, sequence_step=1),
    ]
    scored_lead.status = LeadStatus.FOLLOWING_UP
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "draft_followup", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)
    assert "max follow-ups" in payload["error"]


@pytest.mark.asyncio
async def test_draft_followup_preserves_contacted_status(
    mcp_env, scored_lead, monkeypatch
) -> None:
    """Follow-up drafting must not reset contacted leads to queued."""
    now = now_utc()
    sent = OutreachRecord(
        subject="Initial",
        body="First email body",
        sequence_step=0,
        approved_at=now,
        sent_at=now,
    )
    scored_lead.outreach_history = [sent]
    scored_lead.status = LeadStatus.CONTACTED
    await mcp_env.upsert(scored_lead)

    followup = OutreachRecord(
        subject="Re: Initial",
        body="Follow-up body with new angle.",
        sequence_step=1,
    )

    class FakeDrafter:
        def __init__(self, config, keys):
            pass

        async def draft_followup(self, lead):
            assert lead.next_follow_up_step == 1
            return followup

    monkeypatch.setattr(mcp_server, "DRAFTER_CLASS", FakeDrafter)

    result = await mcp_server.call_tool(
        "draft_followup", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload[0]["sequence_step"] == 1
    assert payload[0]["follow_up_number"] == 1
    assert payload[0]["status"] == LeadStatus.CONTACTED.value
    assert payload[0]["body"] == followup.body

    stored = await mcp_env.get(scored_lead.id)
    assert stored.status == LeadStatus.CONTACTED
    assert len(stored.outreach_history) == 2
    assert stored.outreach_history[0].sent_at is not None
    assert stored.outreach_history[1].sequence_step == 1
    assert stored.outreach_history[1].sent_at is None


@pytest.mark.asyncio
async def test_draft_followup_supersedes_prior_unapproved_at_same_step(
    mcp_env, scored_lead, monkeypatch
) -> None:
    now = now_utc()
    sent = OutreachRecord(
        subject="Initial",
        body="sent",
        sequence_step=0,
        approved_at=now,
        sent_at=now,
    )
    stale = OutreachRecord(subject="old follow-up", body="stale", sequence_step=1)
    scored_lead.outreach_history = [sent, stale]
    scored_lead.status = LeadStatus.CONTACTED
    await mcp_env.upsert(scored_lead)

    fresh = OutreachRecord(subject="fresh follow-up", body="latest", sequence_step=1)

    class FakeDrafter:
        def __init__(self, config, keys):
            pass

        async def draft_followup(self, lead):
            return fresh

    monkeypatch.setattr(mcp_server, "DRAFTER_CLASS", FakeDrafter)

    await mcp_server.call_tool("draft_followup", {"lead_id": scored_lead.id})
    stored = await mcp_env.get(scored_lead.id)
    pending = [
        r for r in stored.outreach_history
        if r.approved_at is None and r.sent_at is None
    ]
    assert len(pending) == 1
    assert pending[0].subject == "fresh follow-up"
    assert len(stored.outreach_history) == 2


@pytest.mark.asyncio
async def test_send_outreach_sends_approved_followup_and_advances_step(
    mcp_env, scored_lead, monkeypatch
) -> None:
    now = now_utc()
    initial = OutreachRecord(
        subject="Initial",
        body="First email",
        sequence_step=0,
        approved_at=now,
        sent_at=now,
    )
    followup = OutreachRecord(
        subject="Re: Initial",
        body="Follow-up body",
        sequence_step=1,
        approved_at=now,
    )
    scored_lead.outreach_history = [initial, followup]
    scored_lead.status = LeadStatus.CONTACTED
    await mcp_env.upsert(scored_lead)

    async def fake_send(self, to_email, to_name, subject, body):
        assert subject == "Re: Initial"
        assert body == "Follow-up body"
        return True

    monkeypatch.setattr(
        "leadgen.outreach.email.EmailSender._send",
        fake_send,
    )

    result = await mcp_server.call_tool(
        "send_outreach",
        {"lead_id": scored_lead.id, "outreach_id": followup.id},
    )
    payload = json.loads(result[0].text)

    assert payload["sent"] is True
    assert payload["status"] == LeadStatus.FOLLOWING_UP.value

    stored = await mcp_env.get(scored_lead.id)
    assert stored.status == LeadStatus.FOLLOWING_UP
    assert stored.next_follow_up_step == 2
    assert stored.outreach_history[1].sent_at is not None


@pytest.mark.asyncio
async def test_send_outreach_prompts_draft_followup_when_all_sent(
    mcp_env, scored_lead
) -> None:
    now = now_utc()
    initial = OutreachRecord(
        subject="Initial",
        body="First email",
        sequence_step=0,
        approved_at=now,
        sent_at=now,
    )
    scored_lead.outreach_history = [initial]
    scored_lead.status = LeadStatus.CONTACTED
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "send_outreach", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert "draft_followup" in payload["error"]
    assert payload["sent_count"] == 1
