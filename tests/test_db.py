"""LeadDatabase tests — upsert / list / dedup / roundtrip.

Uses the `initialized_db` fixture (async-initialized sqlite on a temp
path). No network, no shared state between tests.
"""

from __future__ import annotations

from datetime import timedelta

import aiosqlite
import pytest

from leadgen._time import now_utc
from leadgen.crm.database import EmailCollisionError, LeadDatabase
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
    OutreachRecord,
    ScoringBreakdown,
)


def _lead(
    email: str | None = "x@y.com",
    name: str = "Acme",
    domain: str | None = "acme.com",
    status: LeadStatus = LeadStatus.NEW,
    score_total: float | None = None,
    full_name: str | None = "Jane Doe",
) -> Lead:
    contact = ContactInfo(
        full_name=full_name,
        first_name="Jane",
        last_name="Doe",
        email=email,
        email_verified=bool(email),
    )
    company = CompanyInfo(name=name, domain=domain)
    lead = Lead(source=LeadSource.MANUAL, contact=contact, company=company, status=status)
    if score_total is not None:
        lead.score = ScoringBreakdown(total=score_total, reasoning="seed")
    return lead


# ── Upsert / dedup ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_new_returns_true_and_same_id_returns_false(
    initialized_db: LeadDatabase,
) -> None:
    """First write of a lead is new (True); writing the same id again is
    an update (False). This is the primary signal callers use to count
    'new leads added' in dashboards."""
    lead = _lead(email="first@example.com")
    assert await initialized_db.upsert(lead) is True
    assert await initialized_db.upsert(lead) is False


@pytest.mark.asyncio
async def test_upsert_dedups_by_email_even_with_different_id(
    initialized_db: LeadDatabase,
) -> None:
    """Two Lead objects with different ids but the same contact.email
    collapse to a single DB row. This is the main dedup guarantee for
    multi-source enrichment (Apollo returns a lead, Hunter returns 'the
    same person with a verified email' — we must not insert twice)."""
    a = _lead(email="dup@example.com", name="Acme")
    b = _lead(email="dup@example.com", name="Acme (renamed)")
    assert a.id != b.id  # sanity — these are distinct Pydantic instances

    assert await initialized_db.upsert(a) is True
    assert await initialized_db.upsert(b) is False

    counts = await initialized_db.count_by_status()
    assert sum(counts.values()) == 1


@pytest.mark.asyncio
async def test_identity_dedup_collapses_null_email_repull_onto_enriched(
    initialized_db: LeadDatabase,
) -> None:
    """PROBLEM 1 (prevention). A person already in the DB *with* an email is
    re-fetched by PDL with email=None and a fresh id. The email-independent
    name+company identity must collapse the re-pull onto the existing row —
    no new row, and the enriched email/status must NOT be clobbered by the
    thinner re-fetch. Also exercises case-insensitive company match + missing
    domain on the incoming record (the real two-Lloyd shape)."""
    enriched = _lead(email="known@acme.com", name="Acme", status=LeadStatus.ENRICHED)
    assert await initialized_db.upsert(enriched, dedupe_on_identity=True) is True

    repull = _lead(email=None, name="ACME", domain=None)  # diff case, no domain
    assert repull.id != enriched.id
    assert await initialized_db.upsert(repull, dedupe_on_identity=True) is False

    counts = await initialized_db.count_by_status()
    assert sum(counts.values()) == 1  # no duplicate inserted

    kept = await initialized_db.get(enriched.id)
    assert kept is not None
    assert kept.contact.email == "known@acme.com"   # email preserved
    assert kept.status == LeadStatus.ENRICHED         # status not downgraded


@pytest.mark.asyncio
async def test_identity_dedup_fills_missing_email_without_duplicating(
    initialized_db: LeadDatabase,
) -> None:
    """A fresh fetch that DOES carry an email for a person we only had
    email-less must enrich the existing row in place (no duplicate), not
    insert a second copy."""
    seed = _lead(email=None, name="Acme")
    assert await initialized_db.upsert(seed, dedupe_on_identity=True) is True

    with_email = _lead(email="found@acme.com", name="Acme")
    assert await initialized_db.upsert(with_email, dedupe_on_identity=True) is False

    counts = await initialized_db.count_by_status()
    assert sum(counts.values()) == 1
    kept = await initialized_db.get(seed.id)
    assert kept is not None and kept.contact.email == "found@acme.com"


@pytest.mark.asyncio
async def test_upsert_raises_email_collision_for_duplicate_email_on_other_id(
    initialized_db: LeadDatabase,
) -> None:
    """PROBLEM 2 (backstop). Writing an email that already belongs to a
    different lead raises EmailCollisionError carrying the holder id, so a
    batch caller can skip the one colliding lead instead of aborting."""
    a = _lead(email="taken@x.com", name="Acme")
    await initialized_db.upsert(a)
    b = _lead(email=None, name="Other")
    await initialized_db.upsert(b)

    # b later resolves (e.g. via Hunter) to an email already owned by a.
    b.contact.email = "taken@x.com"
    b.contact.email_verified = True
    with pytest.raises(EmailCollisionError) as exc:
        await initialized_db.upsert(b)
    assert exc.value.existing_lead_id == a.id
    assert exc.value.email == "taken@x.com"


@pytest.mark.asyncio
async def test_list_filters_by_status_and_min_score(
    initialized_db: LeadDatabase,
) -> None:
    """Filters compose: status filter AND min_score filter both apply."""
    await initialized_db.upsert(
        _lead(email="a@x.com", status=LeadStatus.SCORED, score_total=0.9)
    )
    await initialized_db.upsert(
        _lead(email="b@x.com", status=LeadStatus.SCORED, score_total=0.4)
    )
    await initialized_db.upsert(
        _lead(email="c@x.com", status=LeadStatus.NEW, score_total=0.95)
    )

    scored_high = await initialized_db.list(status=LeadStatus.SCORED, min_score=0.8)
    assert len(scored_high) == 1
    assert scored_high[0].contact.email == "a@x.com"


@pytest.mark.asyncio
async def test_list_orders_by_score_total_desc(
    initialized_db: LeadDatabase,
) -> None:
    """List ordering must be score DESC so top leads surface first in the
    MCP `get_pipeline` top_leads slice and the CLI review view."""
    await initialized_db.upsert(_lead(email="lo@x.com", score_total=0.30))
    await initialized_db.upsert(_lead(email="hi@x.com", score_total=0.95))
    await initialized_db.upsert(_lead(email="mid@x.com", score_total=0.60))

    all_leads = await initialized_db.list(limit=10)
    totals = [l.score.total for l in all_leads if l.score]
    assert totals == sorted(totals, reverse=True)


@pytest.mark.asyncio
async def test_count_by_status(initialized_db: LeadDatabase) -> None:
    """count_by_status returns a dict keyed by the status string values."""
    await initialized_db.upsert(_lead(email="1@x.com", status=LeadStatus.NEW))
    await initialized_db.upsert(_lead(email="2@x.com", status=LeadStatus.NEW))
    await initialized_db.upsert(_lead(email="3@x.com", status=LeadStatus.SCORED))

    counts = await initialized_db.count_by_status()
    assert counts.get("new") == 2
    assert counts.get("scored") == 1


# ── find_duplicates / delete_duplicates ───────────────────────────────────────
#
# Design note: `find_duplicates` has two branches — email-based and
# name|company|domain-based. The email branch is effectively unreachable
# in practice because:
#   (a) `upsert()` dedups by email before inserting, and
#   (b) there's a UNIQUE INDEX on contact_email WHERE contact_email IS NOT NULL.
# So the only observable dedup path in integration is the name/company
# branch, triggered when emails are missing. See NOTES.md → 'LeadGen
# find_duplicates email branch is unreachable in practice'.


@pytest.mark.asyncio
async def test_find_duplicates_groups_by_name_company_when_no_email(
    initialized_db: LeadDatabase,
) -> None:
    """Two leads with no email but matching name + company + domain are
    flagged as duplicates."""
    a = _lead(email=None, name="Acme", domain="acme.com", full_name="Jane Doe")
    b = _lead(email=None, name="Acme", domain="acme.com", full_name="Jane Doe")
    assert a.id != b.id

    await initialized_db.upsert(a)
    await initialized_db.upsert(b)

    dupes = await initialized_db.find_duplicates()
    assert len(dupes) == 1
    key, ids = dupes[0]
    assert "jane doe" in key
    assert "acme" in key
    assert sorted(ids) == sorted([a.id, b.id])


@pytest.mark.asyncio
async def test_find_duplicates_ignores_leads_with_no_identifying_info(
    initialized_db: LeadDatabase,
) -> None:
    """A lead without email, without name, without company, and without
    domain cannot be safely deduped. find_duplicates must skip it rather
    than false-positive merge unrelated blank records.

    This previously caught a bug where None first/last names rendered as
    the literal string 'None' inside an f-string, making blank leads
    falsely group together as `name:none none|company:|domain:`. Fixed
    in `LeadDatabase.find_duplicates` by explicitly coercing None → ''
    before interpolation."""
    blank1 = _lead(email=None, name="", domain=None, full_name=None)
    blank2 = _lead(email=None, name="", domain=None, full_name=None)
    blank1.contact.first_name = None
    blank1.contact.last_name = None
    blank2.contact.first_name = None
    blank2.contact.last_name = None

    await initialized_db.upsert(blank1)
    await initialized_db.upsert(blank2)

    dupes = await initialized_db.find_duplicates()
    assert dupes == []


@pytest.mark.asyncio
async def test_find_duplicates_groups_enriched_and_null_same_person(
    initialized_db: LeadDatabase,
) -> None:
    """PROBLEM 3 (cleanup). An enriched row (has email) and a null-email row
    for the SAME person at the SAME company are recognized as duplicates,
    even though their emails differ. This is the two-Lloyd case."""
    enriched = _lead(email="pat@acme.com", name="Acme", full_name="Pat Q")
    nullrow = _lead(email=None, name="Acme", full_name="Pat Q")
    assert enriched.id != nullrow.id

    await initialized_db.upsert(enriched)  # default: no identity collapse
    await initialized_db.upsert(nullrow)

    dupes = await initialized_db.find_duplicates()
    assert len(dupes) == 1
    _key, ids = dupes[0]
    assert sorted(ids) == sorted([enriched.id, nullrow.id])


@pytest.mark.asyncio
async def test_delete_duplicates_keeps_enriched_over_null_even_if_newer(
    initialized_db: LeadDatabase,
) -> None:
    """PROBLEM 3 (cleanup). When collapsing the two-Lloyd case, the row with
    an email survives regardless of keep order — here the enriched row is the
    NEWER one, yet keep='oldest' still keeps it because email beats age."""
    null_old = _lead(email=None, name="Acme", full_name="Pat Q")
    enriched_new = _lead(email="pat@acme.com", name="Acme", full_name="Pat Q")
    null_old.created_at = now_utc() - timedelta(days=5)
    enriched_new.created_at = now_utc()

    await initialized_db.upsert(null_old)
    await initialized_db.upsert(enriched_new)

    deleted = await initialized_db.delete_duplicates(keep="oldest")
    assert deleted == 1
    assert await initialized_db.get(enriched_new.id) is not None
    assert await initialized_db.get(null_old.id) is None


@pytest.mark.asyncio
async def test_delete_duplicates_keeps_oldest(
    initialized_db: LeadDatabase,
) -> None:
    """With keep='oldest', the earliest created_at survives and the rest
    are removed."""
    older = _lead(email=None, name="DupCo", domain="dup.com", full_name="A B")
    newer = _lead(email=None, name="DupCo", domain="dup.com", full_name="A B")
    older.created_at = now_utc() - timedelta(days=5)
    newer.created_at = now_utc()
    await initialized_db.upsert(older)
    await initialized_db.upsert(newer)

    deleted = await initialized_db.delete_duplicates(keep="oldest")
    assert deleted == 1

    assert await initialized_db.get(older.id) is not None
    assert await initialized_db.get(newer.id) is None


# ── Round-trip ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_then_get_roundtrips_score_and_outreach_history(
    initialized_db: LeadDatabase,
) -> None:
    """A lead stored with a score AND outreach_history survives the
    JSON-encode/decode round trip through sqlite. This is the primary
    integrity check for the denormalized-JSON persistence strategy."""
    lead = _lead(email="rt@x.com")
    lead.score = ScoringBreakdown(
        industry_match=0.8,
        company_size_match=0.7,
        geography_match=0.9,
        pain_point_signals=0.6,
        contact_quality=0.5,
        total=0.72,
        reasoning="fit",
    )
    lead.outreach_history = [
        OutreachRecord(subject="hi", body="first", sequence_step=0),
        OutreachRecord(subject="re: hi", body="second", sequence_step=1),
    ]
    lead.tags = ["priority", "q2"]
    lead.notes = "follow up next week"

    await initialized_db.upsert(lead)
    got = await initialized_db.get(lead.id)

    assert got is not None
    assert got.id == lead.id
    assert got.score is not None and got.score.total == 0.72
    assert got.score.industry_match == 0.8
    assert len(got.outreach_history) == 2
    assert got.outreach_history[1].sequence_step == 1
    assert got.tags == ["priority", "q2"]
    assert got.notes == "follow up next week"


@pytest.mark.asyncio
async def test_get_missing_returns_none(initialized_db: LeadDatabase) -> None:
    """get() on an unknown id returns None rather than raising — callers
    use this as a presence check."""
    assert await initialized_db.get("no-such-id") is None


# ── init() idempotency ────────────────────────────────────────────────────────


# ── Targeted delete (CLI-only) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_by_ids_removes_only_those_leads(
    initialized_db: LeadDatabase,
) -> None:
    """delete_by_ids removes only the requested rows; other leads stay."""
    keep = _lead(email="keep@example.com", name="KeepCo")
    drop_a = _lead(email="drop-a@example.com", name="DropA")
    drop_b = _lead(email="drop-b@example.com", name="DropB")
    await initialized_db.upsert(keep)
    await initialized_db.upsert(drop_a)
    await initialized_db.upsert(drop_b)

    deleted = await initialized_db.delete_by_ids([drop_a.id, drop_b.id])

    assert deleted == 2
    assert await initialized_db.get(drop_a.id) is None
    assert await initialized_db.get(drop_b.id) is None
    assert await initialized_db.get(keep.id) is not None
    assert await initialized_db.count_all() == 1


@pytest.mark.asyncio
async def test_delete_by_status_removes_only_that_status(
    initialized_db: LeadDatabase,
) -> None:
    """delete_by_status removes only rows with the exact status given."""
    new_a = _lead(email="new-a@example.com", status=LeadStatus.NEW)
    new_b = _lead(email="new-b@example.com", status=LeadStatus.NEW)
    contacted = _lead(email="contacted@example.com", status=LeadStatus.CONTACTED)
    following = _lead(email="follow@example.com", status=LeadStatus.FOLLOWING_UP)
    queued = _lead(email="queued@example.com", status=LeadStatus.QUEUED)
    enriched = _lead(email="enriched@example.com", status=LeadStatus.ENRICHED)
    closed = _lead(email="lost@example.com", status=LeadStatus.CLOSED_LOST)
    for lead in (new_a, new_b, contacted, following, queued, enriched, closed):
        await initialized_db.upsert(lead)

    deleted = await initialized_db.delete_by_status(LeadStatus.NEW)

    assert deleted == 2
    assert await initialized_db.get(new_a.id) is None
    assert await initialized_db.get(new_b.id) is None
    for survivor in (contacted, following, queued, enriched, closed):
        assert await initialized_db.get(survivor.id) is not None
    assert await initialized_db.count_all() == 5
    counts = await initialized_db.count_by_status()
    assert counts.get("new", 0) == 0
    assert counts.get("contacted") == 1
    assert counts.get("following_up") == 1
    assert counts.get("queued") == 1
    assert counts.get("enriched") == 1
    assert counts.get("closed_lost") == 1


@pytest.mark.asyncio
async def test_init_is_idempotent(tmp_db_path: str) -> None:
    """Calling init() twice on the same path must not raise — the MCP
    server's call_tool handler re-invokes init() on every request."""
    db = LeadDatabase(tmp_db_path)
    await db.init()
    await db.init()  # second call must be a no-op

    # Confirm the table is there by querying sqlite_master directly.
    async with aiosqlite.connect(tmp_db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='leads'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None and row[0] == "leads"
