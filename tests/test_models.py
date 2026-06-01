"""Lead / ContactInfo / CompanyInfo / OutreachRecord model tests.

Pure model logic — no async, no DB, no network.
"""

from __future__ import annotations

from datetime import timedelta

from leadgen._time import now_utc
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
    OutreachRecord,
)


def _minimal_lead(**overrides) -> Lead:
    contact = overrides.pop("contact", ContactInfo())
    company = overrides.pop("company", CompanyInfo(name="Acme Corp"))
    return Lead(source=LeadSource.MANUAL, contact=contact, company=company, **overrides)


def test_display_name_prefers_full_name() -> None:
    """When `contact.full_name` is set, it wins over first/last reconstruction."""
    lead = _minimal_lead(
        contact=ContactInfo(first_name="Jane", last_name="Doe", full_name="Jane D. Doe")
    )
    assert lead.display_name == "Jane D. Doe"


def test_display_name_falls_back_to_first_last() -> None:
    """Without full_name, display_name joins first+last, skipping Nones."""
    lead = _minimal_lead(contact=ContactInfo(first_name="Jane", last_name="Doe"))
    assert lead.display_name == "Jane Doe"
    lead2 = _minimal_lead(contact=ContactInfo(first_name="Jane"))
    assert lead2.display_name == "Jane"


def test_display_name_returns_unknown_when_empty() -> None:
    """A lead with no name parts at all renders as 'Unknown' for CLI/log safety."""
    lead = _minimal_lead(contact=ContactInfo())
    assert lead.display_name == "Unknown"


def test_is_contactable_requires_verified_email() -> None:
    """`is_contactable` must be strict: unverified or missing email → False."""
    missing = _minimal_lead(contact=ContactInfo(email=None, email_verified=False))
    unverified = _minimal_lead(
        contact=ContactInfo(email="j@x.com", email_verified=False)
    )
    verified = _minimal_lead(contact=ContactInfo(email="j@x.com", email_verified=True))
    assert missing.is_contactable is False
    assert unverified.is_contactable is False
    assert verified.is_contactable is True


def test_next_follow_up_step_counts_only_sent_records() -> None:
    """next_follow_up_step reflects how many outreaches have actually been
    sent — drafted-but-unsent records must not advance the sequence."""
    now = now_utc()
    lead = _minimal_lead(
        outreach_history=[
            OutreachRecord(subject="draft only", body="x"),  # no sent_at
            OutreachRecord(subject="sent #1", body="y", sent_at=now),
            OutreachRecord(subject="sent #2", body="z", sent_at=now),
        ]
    )
    assert lead.next_follow_up_step == 2


def test_touch_bumps_updated_at() -> None:
    """touch() must advance updated_at; otherwise CRM freshness checks rot."""
    lead = _minimal_lead()
    before = lead.updated_at
    # Force a small observable delta without sleeping
    lead.updated_at = before - timedelta(seconds=5)
    lead.touch()
    assert lead.updated_at > before - timedelta(seconds=5)


def test_lead_default_status_is_new() -> None:
    """Every freshly constructed Lead must start at NEW — downstream status
    transitions depend on this invariant."""
    lead = _minimal_lead()
    assert lead.status is LeadStatus.NEW
