"""EmailSender tests — dry-run + pure logic, no real SMTP.

We exercise the send pipeline with `dry_run=True` so `_send` returns
True without touching aiosmtplib. That lets us assert state transitions
(status, sent_at, _sent_today counter) without any network.
"""

from __future__ import annotations

import pytest

from leadgen._time import now_utc
from leadgen.crm.database import LeadDatabase
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
    OutreachRecord,
)
from leadgen.outreach.email import DailyLimitReached, EmailSender


def _approve(record: OutreachRecord) -> OutreachRecord:
    record.approved_at = now_utc()
    return record


# ── Pending-record selection ──────────────────────────────────────────────────


def test_next_pending_record_returns_first_approved_unsent(
    test_config, test_keys, initialized_db, sample_lead
) -> None:
    """_next_pending_record picks the first approved-but-unsent record,
    skipping sent ones and unapproved drafts."""
    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    r0 = OutreachRecord(
        subject="sent", body="b0", sent_at=now_utc(), approved_at=now_utc()
    )
    r1 = OutreachRecord(subject="draft only", body="b1")  # unapproved
    r2 = _approve(OutreachRecord(subject="approved", body="b2"))
    sample_lead.outreach_history = [r0, r1, r2]

    got = sender._next_pending_record(sample_lead)
    assert got is r2


def test_next_pending_record_returns_none_when_nothing_pending(
    test_config, test_keys, initialized_db, sample_lead
) -> None:
    """No records at all, or only unapproved drafts → None."""
    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    assert sender._next_pending_record(sample_lead) is None

    sample_lead.outreach_history = [OutreachRecord(subject="draft", body="b")]
    assert sender._next_pending_record(sample_lead) is None


# ── Skip / guard paths ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_approved_outreach_skips_lead_without_email(
    test_config, test_keys, initialized_db: LeadDatabase, sample_lead: Lead
) -> None:
    """A lead with no email must be skipped (return False) rather than
    attempting a send to an empty recipient."""
    sample_lead.contact.email = None
    sample_lead.outreach_history = [_approve(OutreachRecord(subject="s", body="b"))]
    await initialized_db.upsert(sample_lead)

    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    ok = await sender.send_approved_outreach(sample_lead)
    assert ok is False
    # Status must NOT have advanced
    assert sample_lead.status == LeadStatus.NEW


@pytest.mark.asyncio
async def test_send_approved_outreach_skips_when_no_pending(
    test_config, test_keys, initialized_db: LeadDatabase, sample_lead: Lead
) -> None:
    """No approved record → send returns False and status stays put."""
    await initialized_db.upsert(sample_lead)
    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    ok = await sender.send_approved_outreach(sample_lead)
    assert ok is False
    assert sample_lead.status == LeadStatus.NEW


# ── Happy path (dry run) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_initial_moves_status_to_contacted(
    test_config, test_keys, initialized_db: LeadDatabase, sample_lead: Lead
) -> None:
    """Successful send of a sequence_step=0 record advances the lead to
    CONTACTED and stamps sent_at on the record."""
    record = _approve(OutreachRecord(subject="hi", body="b", sequence_step=0))
    sample_lead.outreach_history = [record]
    await initialized_db.upsert(sample_lead)

    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    ok = await sender.send_approved_outreach(sample_lead)

    assert ok is True
    assert sample_lead.status == LeadStatus.CONTACTED
    assert record.sent_at is not None
    assert sender._sent_today == 1


@pytest.mark.asyncio
async def test_send_followup_moves_status_to_following_up(
    test_config, test_keys, initialized_db: LeadDatabase, sample_lead: Lead
) -> None:
    """A sequence_step>=1 record uses FOLLOWING_UP as the target status
    so the pipeline view distinguishes first-touched from repeatedly-
    nudged leads."""
    record = _approve(OutreachRecord(subject="re: hi", body="b", sequence_step=1))
    sample_lead.outreach_history = [record]
    await initialized_db.upsert(sample_lead)

    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    ok = await sender.send_approved_outreach(sample_lead)

    assert ok is True
    assert sample_lead.status == LeadStatus.FOLLOWING_UP


# ── Daily limit enforcement ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_daily_limit_raises_when_over(
    test_config, test_keys, initialized_db: LeadDatabase
) -> None:
    """_check_daily_limit must raise DailyLimitReached at or above the
    configured cap — the send loop uses this as its stop signal."""
    test_config.outreach.daily_email_limit = 2
    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    sender._sent_today = 2
    with pytest.raises(DailyLimitReached):
        await sender._check_daily_limit()


@pytest.mark.asyncio
async def test_send_batch_stops_at_daily_limit(
    test_config, test_keys, initialized_db: LeadDatabase
) -> None:
    """When DailyLimitReached fires mid-batch, send_batch must stop
    cleanly and return a summary reflecting what went out before the cap
    was hit. No partial sends should be lost from the counter."""
    test_config.outreach.daily_email_limit = 2

    leads: list[Lead] = []
    for i in range(5):
        lead = Lead(
            source=LeadSource.MANUAL,
            contact=ContactInfo(
                full_name=f"U {i}",
                email=f"u{i}@x.com",
                email_verified=True,
            ),
            company=CompanyInfo(name=f"Co{i}"),
            outreach_history=[
                _approve(OutreachRecord(subject=f"hi {i}", body="b", sequence_step=0))
            ],
        )
        await initialized_db.upsert(lead)
        leads.append(lead)

    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=True)
    summary = await sender.send_batch(leads)

    assert summary["sent"] == 2
    assert summary["failed"] == 0
    # Remaining leads should be accounted for as skipped (the exact count
    # depends on when DailyLimitReached fires — contract is: sent + failed
    # + skipped == total input.)
    assert summary["sent"] + summary["skipped"] + summary["failed"] == len(leads)
