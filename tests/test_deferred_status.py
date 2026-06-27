"""Deferred lead status — inert parking slot, distinct from closed_lost."""

from __future__ import annotations

import json

import pytest

from leadgen._time import now_utc
from leadgen.crm.suppression import lead_triggers_suppression
from leadgen.crm.update_lead import update_lead
from leadgen.mcp_server import server as mcp_server
from leadgen.models import LeadStatus, OutreachRecord, ScoringBreakdown
from leadgen.outreach.email import EmailSender


class _MockScorer:
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
    test_keys.smtp_host = "smtp.test.com"
    test_keys.smtp_username = "smtp-user"
    test_keys.smtp_password = "smtp-pass"
    monkeypatch.setattr(mcp_server, "config", test_config)
    monkeypatch.setattr(mcp_server, "keys", test_keys)
    monkeypatch.setattr(mcp_server, "db", initialized_db)
    monkeypatch.setattr(mcp_server, "SCORER_CLASS", _MockScorer)
    return initialized_db


@pytest.mark.asyncio
async def test_deferred_in_status_enum() -> None:
    assert LeadStatus.DEFERRED.value == "deferred"


@pytest.mark.asyncio
async def test_update_lead_status_sets_deferred(mcp_env, sample_lead) -> None:
    await mcp_env.upsert(sample_lead)

    result = await mcp_server.call_tool(
        "update_lead_status",
        {"lead_id": sample_lead.id, "status": "deferred", "notes": "home inspection vertical"},
    )
    payload = json.loads(result[0].text)

    assert payload == {"updated": True, "status": "deferred"}

    stored = await mcp_env.get(sample_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.DEFERRED
    assert "home inspection vertical" in stored.notes


@pytest.mark.asyncio
async def test_deferred_does_not_auto_suppress(mcp_env, sample_lead) -> None:
    await mcp_env.upsert(sample_lead)
    await update_lead(mcp_env, sample_lead.id, status="deferred")

    stored = await mcp_env.get(sample_lead.id)
    assert stored is not None
    assert lead_triggers_suppression(stored) is None

    suppressed = await mcp_env.check_lead_suppressed(stored)
    assert suppressed == (False, None)


@pytest.mark.asyncio
async def test_deferred_survives_delete_by_status_new(
    initialized_db, sample_lead
) -> None:
    deferred = sample_lead.model_copy(deep=True)
    deferred.id = "deferred-lead"
    deferred.status = LeadStatus.DEFERRED
    await initialized_db.upsert(deferred)

    new_lead = sample_lead.model_copy(deep=True)
    new_lead.id = "new-lead"
    new_lead.status = LeadStatus.NEW
    new_lead.contact = new_lead.contact.model_copy(
        update={"email": "other@acmecorp.com"}
    )
    await initialized_db.upsert(new_lead)

    deleted = await initialized_db.delete_by_status(LeadStatus.NEW)
    assert deleted == 1

    assert await initialized_db.get("deferred-lead") is not None
    assert await initialized_db.get("new-lead") is None


@pytest.mark.asyncio
async def test_score_leads_skips_deferred_by_id(mcp_env, sample_lead) -> None:
    sample_lead.status = LeadStatus.DEFERRED
    await mcp_env.upsert(sample_lead)

    result = await mcp_server.call_tool(
        "score_leads", {"lead_id": sample_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload["leads_scored"] == 0
    assert payload["skipped_inert"] == [sample_lead.id]

    stored = await mcp_env.get(sample_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.DEFERRED
    assert stored.score is None


@pytest.mark.asyncio
async def test_draft_outreach_skips_deferred(mcp_env, scored_lead, monkeypatch) -> None:
    scored_lead.status = LeadStatus.DEFERRED
    await mcp_env.upsert(scored_lead)

    class FakeDrafter:
        def __init__(self, config, keys):
            pass

        async def draft_initial(self, lead):
            raise AssertionError("should not draft inert lead")

    monkeypatch.setattr(mcp_server, "DRAFTER_CLASS", FakeDrafter)

    result = await mcp_server.call_tool(
        "draft_outreach", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert payload == []
    stored = await mcp_env.get(scored_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.DEFERRED
    assert stored.outreach_history == []


@pytest.mark.asyncio
async def test_draft_followup_refuses_deferred(mcp_env, scored_lead) -> None:
    now = now_utc()
    scored_lead.outreach_history = [
        OutreachRecord(
            subject="Initial",
            body="Sent body",
            sequence_step=0,
            approved_at=now,
            sent_at=now,
        )
    ]
    scored_lead.status = LeadStatus.DEFERRED
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool(
        "draft_followup", {"lead_id": scored_lead.id}
    )
    payload = json.loads(result[0].text)

    assert "inert" in payload["error"]
    assert payload["lead_id"] == scored_lead.id


@pytest.mark.asyncio
async def test_send_outreach_refuses_deferred(
    mcp_env, scored_lead, monkeypatch
) -> None:
    record = OutreachRecord(
        subject="Hello",
        body="Production body",
        sequence_step=0,
        approved_at=now_utc(),
    )
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.DEFERRED
    await mcp_env.upsert(scored_lead)

    async def fake_send(self, to_email, to_name, subject, body):
        raise AssertionError("should not send inert lead")

    monkeypatch.setattr("leadgen.outreach.email.EmailSender._send", fake_send)

    result = await mcp_server.call_tool(
        "send_outreach",
        {"lead_id": scored_lead.id, "dry_run": False},
    )
    payload = json.loads(result[0].text)

    assert "inert" in payload["error"]
    assert payload["lead_id"] == scored_lead.id

    stored = await mcp_env.get(scored_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.DEFERRED
    assert stored.outreach_history[0].sent_at is None


@pytest.mark.asyncio
async def test_send_batch_skips_deferred(
    initialized_db, test_config, test_keys, scored_lead
) -> None:
    record = OutreachRecord(
        subject="Hello",
        body="Body",
        sequence_step=0,
        approved_at=now_utc(),
    )
    scored_lead.outreach_history = [record]
    scored_lead.status = LeadStatus.DEFERRED
    await initialized_db.upsert(scored_lead)

    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    summary = await sender.send_batch([scored_lead])

    assert summary["sent"] == 0
    assert summary["skipped"] == 1

    stored = await initialized_db.get(scored_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.DEFERRED


@pytest.mark.asyncio
async def test_deferred_reversible_to_new_and_scored(mcp_env, sample_lead) -> None:
    sample_lead.status = LeadStatus.DEFERRED
    sample_lead.notes = "parked for home inspection experiment"
    await mcp_env.upsert(sample_lead)

    result = await mcp_server.call_tool(
        "update_lead_status", {"lead_id": sample_lead.id, "status": "new"}
    )
    payload = json.loads(result[0].text)
    assert payload["status"] == "new"

    stored = await mcp_env.get(sample_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.NEW
    assert "parked for home inspection experiment" in stored.notes

    result = await mcp_server.call_tool(
        "score_leads", {"lead_id": sample_lead.id}
    )
    score_payload = json.loads(result[0].text)
    assert score_payload["leads_scored"] == 1

    stored = await mcp_env.get(sample_lead.id)
    assert stored is not None
    assert stored.status == LeadStatus.SCORED


@pytest.mark.asyncio
async def test_get_pipeline_excludes_deferred_from_top_leads(
    mcp_env, scored_lead
) -> None:
    scored_lead.status = LeadStatus.DEFERRED
    await mcp_env.upsert(scored_lead)

    result = await mcp_server.call_tool("get_pipeline", {})
    payload = json.loads(result[0].text)

    top_ids = [l["id"] for l in payload["top_leads"]]
    assert scored_lead.id not in top_ids
    assert payload["pipeline_counts"].get("deferred") == 1
