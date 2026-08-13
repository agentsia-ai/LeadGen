"""D1 backend tests — wire protocol, SQLite parity, and batch atomicity.

The mocked ``http://db.internal/query`` endpoint is backed by a real in-memory
SQLite connection rather than canned JSON. That makes these behavioral tests
rather than shape tests: the SQL the adapter emits has to actually parse, bind
its parameters, honor the UNIQUE index, and return rows the adapter can rebuild
Leads from. A hand-stubbed endpoint would happily accept SQL that D1 rejects.

Request payloads are still recorded, so the batch-vs-single decisions documented
in ``leadgen.crm.d1`` are asserted directly: a path that is supposed to be one
atomic batch must not silently become N round trips, and vice versa.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import pytest_asyncio
import respx

from leadgen.config.loader import DatabaseConfig
from leadgen.crm.d1 import (
    DEFAULT_D1_URL,
    D1Client,
    D1Database,
    D1Error,
    resolve_d1_url,
)
from leadgen.crm.database import EmailCollisionError, LeadDatabase
from leadgen.crm.factory import create_database, describe_backend
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
    OutreachRecord,
)


class FakeD1Backend:
    """Stands in for the Worker's D1 outbound handler.

    Mirrors the real handler's contract: a single statement returns
    ``{results, meta}``; a batch runs every statement in one transaction and
    returns a list of those, rolling back entirely if any statement fails.
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.requests: list[dict] = []

    def close(self) -> None:
        self.conn.close()

    # -- request log helpers -------------------------------------------------

    @property
    def batch_requests(self) -> list[dict]:
        return [r for r in self.requests if "batch" in r]

    @property
    def single_requests(self) -> list[dict]:
        return [r for r in self.requests if "sql" in r]

    def reset_log(self) -> None:
        self.requests.clear()

    def statements(self) -> list[str]:
        """Every SQL statement seen, flattened across singles and batches."""
        out: list[str] = []
        for req in self.requests:
            if "sql" in req:
                out.append(req["sql"])
            else:
                out.extend(s["sql"] for s in req["batch"])
        return out

    # -- handler -------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if "batch" in payload:
            return self._run_batch(payload["batch"])
        return self._run_single(payload["sql"], payload.get("params") or [])

    def _run_single(self, sql: str, params: list) -> httpx.Response:
        try:
            cur = self.conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            return self._error(exc)
        return httpx.Response(
            200,
            json={
                "success": True,
                "results": rows,
                "meta": {"changes": max(cur.rowcount, 0)},
            },
        )

    def _run_batch(self, statements: list[dict]) -> httpx.Response:
        results = []
        try:
            with self.conn:  # commits on success, rolls back on exception
                for stmt in statements:
                    cur = self.conn.execute(stmt["sql"], stmt.get("params") or [])
                    results.append({
                        "results": [dict(r) for r in cur.fetchall()],
                        "meta": {"changes": max(cur.rowcount, 0)},
                    })
        except sqlite3.Error as exc:
            return self._error(exc)
        return httpx.Response(200, json={"success": True, "batch": results})

    @staticmethod
    def _error(exc: sqlite3.Error) -> httpx.Response:
        # D1 surfaces SQLite's message verbatim behind a D1_ERROR prefix.
        return httpx.Response(
            500, json={"success": False, "error": f"D1_ERROR: {exc}"}
        )


@pytest_asyncio.fixture
async def d1():
    """An initialized D1Database wired to the fake backend."""
    backend = FakeD1Backend()
    with respx.mock:
        respx.post(DEFAULT_D1_URL).mock(side_effect=backend.handle)
        db = D1Database()
        try:
            await db.init()
            yield db, backend
        finally:
            await db.aclose()
            backend.close()


def _lead(**overrides) -> Lead:
    """Minimal valid lead; override any field per test."""
    contact = overrides.pop("contact", None) or ContactInfo(
        first_name="Jane", last_name="Doe", full_name="Jane Doe",
        title="Owner", email="jane@acme.com", email_verified=True,
    )
    company = overrides.pop("company", None) or CompanyInfo(
        name="acme corp", display_name="Acme Corp", domain="acme.com",
    )
    return Lead(
        source=LeadSource.PDL, contact=contact, company=company, **overrides
    )


# ── Wire protocol ─────────────────────────────────────────────────────────────

def test_resolve_d1_url_prefers_explicit_then_env_then_default(monkeypatch):
    monkeypatch.delenv("LEADGEN_D1_URL", raising=False)
    assert resolve_d1_url() == DEFAULT_D1_URL
    assert resolve_d1_url("http://explicit/query") == "http://explicit/query"

    monkeypatch.setenv("LEADGEN_D1_URL", "http://from-env/query")
    assert resolve_d1_url() == "http://from-env/query"
    # An explicit argument still wins over the environment.
    assert resolve_d1_url("http://explicit/query") == "http://explicit/query"


@pytest.mark.asyncio
@respx.mock
async def test_single_statement_wire_format() -> None:
    """One statement posts {sql, params} — never a batch envelope."""
    route = respx.post(DEFAULT_D1_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "results": [{"n": 3}], "meta": {"changes": 0}}
        )
    )
    client = D1Client()
    try:
        result = await client.query("SELECT COUNT(*) AS n FROM leads WHERE status = ?", ["new"])
    finally:
        await client.aclose()

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "sql": "SELECT COUNT(*) AS n FROM leads WHERE status = ?",
        "params": ["new"],
    }
    assert result.scalar() == 3
    assert result.rows == [{"n": 3}]


@pytest.mark.asyncio
@respx.mock
async def test_batch_posts_one_request_with_all_statements() -> None:
    """A batch is ONE HTTP request — that is what makes it the atomic unit."""
    route = respx.post(DEFAULT_D1_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "batch": [
                    {"results": [{"n": 2}], "meta": {"changes": 0}},
                    {"results": [], "meta": {"changes": 2}},
                ],
            },
        )
    )
    client = D1Client()
    try:
        results = await client.batch([
            ("SELECT COUNT(*) AS n FROM leads", []),
            ("DELETE FROM leads", []),
        ])
    finally:
        await client.aclose()

    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert [s["sql"] for s in sent["batch"]] == [
        "SELECT COUNT(*) AS n FROM leads",
        "DELETE FROM leads",
    ]
    assert results[0].scalar() == 2
    assert results[1].changes == 2


@pytest.mark.asyncio
async def test_empty_batch_makes_no_request() -> None:
    with respx.mock:
        route = respx.post(DEFAULT_D1_URL)
        client = D1Client()
        try:
            assert await client.batch([]) == []
        finally:
            await client.aclose()
        assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_http_error_raises_d1_error_with_proxy_message() -> None:
    respx.post(DEFAULT_D1_URL).mock(
        return_value=httpx.Response(500, json={"success": False, "error": "no such table: leads"})
    )
    client = D1Client()
    try:
        with pytest.raises(D1Error, match="no such table"):
            await client.query("SELECT 1")
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_unreachable_proxy_raises_d1_error_naming_the_url() -> None:
    respx.post(DEFAULT_D1_URL).mock(side_effect=httpx.ConnectError("refused"))
    client = D1Client()
    try:
        with pytest.raises(D1Error, match="db.internal"):
            await client.query("SELECT 1")
    finally:
        await client.aclose()


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_init_creates_schema_once_and_caches(d1) -> None:
    """init() is called per MCP tool invocation; it must not re-issue DDL."""
    db, backend = d1
    after_first = len(backend.requests)
    assert after_first > 0
    assert any("CREATE TABLE IF NOT EXISTS leads" in s for s in backend.statements())

    await db.init()
    await db.init()
    assert len(backend.requests) == after_first


# ── Round-trip and reads ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_then_get_roundtrips_every_field(d1) -> None:
    db, _ = d1
    lead = _lead(
        status=LeadStatus.SCORED,
        notes="spoke at conference",
        tags=["warm", "referral"],
        raw_data={"pdl": {"id": "abc"}},
        outreach_history=[OutreachRecord(subject="Hi", body="Body text", sequence_step=0)],
    )

    assert await db.upsert(lead) is True

    fetched = await db.get(lead.id)
    assert fetched is not None
    assert fetched.id == lead.id
    assert fetched.status is LeadStatus.SCORED
    assert fetched.source is LeadSource.PDL
    assert fetched.contact.email == "jane@acme.com"
    assert fetched.contact.email_verified is True
    assert fetched.company.display_name == "Acme Corp"
    assert fetched.notes == "spoke at conference"
    assert fetched.tags == ["warm", "referral"]
    assert fetched.raw_data == {"pdl": {"id": "abc"}}
    assert [r.subject for r in fetched.outreach_history] == ["Hi"]
    # Datetimes come back tz-aware so aware/naive comparisons can't blow up
    # at runtime (see OutreachRecord._coerce_aware_utc).
    assert fetched.created_at.tzinfo is not None
    assert fetched.outreach_history[0].drafted_at.tzinfo is not None


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_id(d1) -> None:
    db, _ = d1
    assert await db.get("nope") is None


@pytest.mark.asyncio
async def test_second_upsert_updates_rather_than_inserts(d1) -> None:
    db, _ = d1
    lead = _lead()
    assert await db.upsert(lead) is True

    lead.status = LeadStatus.ENRICHED
    lead.notes = "updated"
    assert await db.upsert(lead) is False

    assert await db.count_all() == 1
    fetched = await db.get(lead.id)
    assert fetched.status is LeadStatus.ENRICHED
    assert fetched.notes == "updated"


@pytest.mark.asyncio
async def test_upsert_write_is_a_single_statement_not_a_batch(d1) -> None:
    """Documents the audit decision: the upsert write needs no batch because
    it is exactly one INSERT or one UPDATE."""
    db, backend = d1
    backend.reset_log()

    await db.upsert(_lead())

    assert backend.batch_requests == []
    writes = [s for s in backend.statements() if s.strip().startswith(("INSERT", "UPDATE"))]
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_list_filters_by_status_and_min_score(d1) -> None:
    db, _ = d1
    from leadgen.models import ScoringBreakdown

    high = _lead(status=LeadStatus.SCORED)
    high.score = ScoringBreakdown(total=0.9)
    low = _lead(
        status=LeadStatus.SCORED,
        contact=ContactInfo(full_name="Bob Low", email="bob@low.com"),
        company=CompanyInfo(name="low co"),
    )
    low.score = ScoringBreakdown(total=0.2)
    new_lead = _lead(
        contact=ContactInfo(full_name="Cara New", email="cara@new.com"),
        company=CompanyInfo(name="new co"),
    )
    for lead in (high, low, new_lead):
        await db.upsert(lead)

    scored = await db.list(status=LeadStatus.SCORED)
    assert {lead.id for lead in scored} == {high.id, low.id}

    strong = await db.list(min_score=0.5)
    assert [lead.id for lead in strong] == [high.id]

    assert len(await db.list(limit=2)) == 2


@pytest.mark.asyncio
async def test_count_by_status_and_get_by_ids(d1) -> None:
    db, _ = d1
    a = _lead(status=LeadStatus.NEW)
    b = _lead(
        status=LeadStatus.NEW,
        contact=ContactInfo(full_name="B B", email="b@b.com"),
        company=CompanyInfo(name="b co"),
    )
    c = _lead(
        status=LeadStatus.CONTACTED,
        contact=ContactInfo(full_name="C C", email="c@c.com"),
        company=CompanyInfo(name="c co"),
    )
    for lead in (a, b, c):
        await db.upsert(lead)

    assert await db.count_by_status() == {"new": 2, "contacted": 1}
    assert await db.count_all() == 3
    assert {lead.id for lead in await db.get_by_ids([a.id, c.id])} == {a.id, c.id}
    assert await db.get_by_ids([]) == []


# ── Dedup semantics ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_collapses_onto_existing_email(d1) -> None:
    db, _ = d1
    first = _lead()
    await db.upsert(first)

    same_email = _lead()
    same_email.id = "different-id"
    same_email.notes = "second pull"

    assert await db.upsert(same_email) is False
    assert await db.count_all() == 1


@pytest.mark.asyncio
async def test_identity_dedupe_merges_without_downgrading(d1) -> None:
    """A thin re-pull (null email) must collapse onto the enriched row and
    must not overwrite the stored email or status."""
    db, _ = d1
    enriched = _lead(status=LeadStatus.ENRICHED)
    await db.upsert(enriched)

    thin = Lead(
        source=LeadSource.PDL,
        status=LeadStatus.NEW,
        contact=ContactInfo(first_name="Jane", last_name="Doe", full_name="Jane Doe"),
        company=CompanyInfo(name="acme corp"),
    )
    assert await db.upsert(thin, dedupe_on_identity=True) is False

    assert await db.count_all() == 1
    stored = await db.get(enriched.id)
    assert stored.contact.email == "jane@acme.com"
    assert stored.status is LeadStatus.ENRICHED
    assert stored.company.domain == "acme.com"


@pytest.mark.asyncio
async def test_upsert_raises_email_collision_naming_the_holder(d1) -> None:
    """The UNIQUE(contact_email) index is the real concurrency backstop, so a
    violation has to surface as EmailCollisionError naming the owning row —
    not as a raw D1Error that would abort a whole enrich batch.

    This is the enrich_lead shape: an existing row (matched by id) is handed an
    email that already belongs to a different lead. An incoming *new* lead
    carrying a taken email never reaches the constraint, because upsert's
    dedupe-by-email collapses it onto the owner first.
    """
    db, _ = d1
    holder = _lead()
    await db.upsert(holder)

    other = _lead(
        contact=ContactInfo(first_name="Other", last_name="Person", full_name="Other Person"),
        company=CompanyInfo(name="other co"),
    )
    await db.upsert(other)

    other.contact.email = "jane@acme.com"
    with pytest.raises(EmailCollisionError) as excinfo:
        await db.upsert(other)

    assert excinfo.value.email == "jane@acme.com"
    assert excinfo.value.existing_lead_id == holder.id
    # The failed statement left nothing behind.
    assert (await db.get(other.id)).contact.email is None


@pytest.mark.asyncio
async def test_find_duplicates_groups_on_name_and_company(d1) -> None:
    db, _ = d1
    one = _lead()
    two = _lead(
        contact=ContactInfo(first_name="Jane", last_name="Doe", full_name="Jane Doe"),
        company=CompanyInfo(name="acme corp"),
    )
    await db.upsert(one)
    await db.upsert(two)  # no email, so no UNIQUE conflict

    dupes = await db.find_duplicates()
    assert len(dupes) == 1
    key, ids = dupes[0]
    assert key == "name:jane doe|company:acme corp"
    assert set(ids) == {one.id, two.id}


# ── Batched destructive paths ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_duplicates_keeps_enriched_row_in_one_batch(d1) -> None:
    db, backend = d1
    enriched = _lead()
    thin = _lead(
        contact=ContactInfo(first_name="Jane", last_name="Doe", full_name="Jane Doe"),
        company=CompanyInfo(name="acme corp"),
    )
    await db.upsert(enriched)
    await db.upsert(thin)
    backend.reset_log()

    removed = await db.delete_duplicates()

    assert removed == 1
    # One batch: a partial apply would collapse some groups and not others.
    assert len(backend.batch_requests) == 1
    assert await db.count_all() == 1
    survivor = await db.get(enriched.id)
    assert survivor is not None and survivor.contact.email == "jane@acme.com"


@pytest.mark.asyncio
async def test_delete_duplicates_is_a_noop_without_duplicates(d1) -> None:
    db, backend = d1
    await db.upsert(_lead())
    backend.reset_log()

    assert await db.delete_duplicates() == 0
    assert backend.batch_requests == []
    assert await db.count_all() == 1


@pytest.mark.asyncio
async def test_delete_all_counts_and_deletes_in_one_batch(d1) -> None:
    db, backend = d1
    await db.upsert(_lead())
    await db.upsert(
        _lead(
            contact=ContactInfo(full_name="B B", email="b@b.com"),
            company=CompanyInfo(name="b co"),
        )
    )
    backend.reset_log()

    removed = await db.delete_all()

    assert removed == 2
    assert len(backend.batch_requests) == 1
    assert [s["sql"] for s in backend.batch_requests[0]["batch"]] == [
        "SELECT COUNT(*) AS n FROM leads",
        "DELETE FROM leads",
    ]
    assert await db.count_all() == 0


@pytest.mark.asyncio
async def test_delete_by_ids_in_one_batch(d1) -> None:
    db, backend = d1
    keep = _lead()
    drop = _lead(
        contact=ContactInfo(full_name="B B", email="b@b.com"),
        company=CompanyInfo(name="b co"),
    )
    await db.upsert(keep)
    await db.upsert(drop)
    backend.reset_log()

    assert await db.delete_by_ids([drop.id]) == 1
    assert len(backend.batch_requests) == 1
    assert await db.get(keep.id) is not None
    assert await db.get(drop.id) is None

    backend.reset_log()
    assert await db.delete_by_ids([]) == 0
    assert backend.requests == []


@pytest.mark.asyncio
async def test_delete_by_status_leaves_other_statuses_alone(d1) -> None:
    db, backend = d1
    doomed = _lead(status=LeadStatus.CLOSED_LOST)
    safe = _lead(
        status=LeadStatus.NEW,
        contact=ContactInfo(full_name="B B", email="b@b.com"),
        company=CompanyInfo(name="b co"),
    )
    await db.upsert(doomed)
    await db.upsert(safe)
    backend.reset_log()

    assert await db.delete_by_status(LeadStatus.CLOSED_LOST) == 1
    assert len(backend.batch_requests) == 1
    assert await db.get(safe.id) is not None
    assert await db.get(doomed.id) is None


@pytest.mark.asyncio
async def test_backfill_company_display_names_batches_repairs(d1) -> None:
    db, backend = d1
    shouty = _lead(company=CompanyInfo(name="protect realty", display_name="PROTECT REALTY"))
    await db.upsert(shouty)
    backend.reset_log()

    assert await db.backfill_company_display_names() == 1
    assert len(backend.batch_requests) == 1

    fixed = await db.get(shouty.id)
    assert fixed.company.display_name == "Protect Realty"


# ── Suppressions ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_suppression_add_is_idempotent_and_removable(d1) -> None:
    db, _ = d1
    assert await db.add_suppression("name:jane doe|company:acme corp", "replied-no") is True
    assert await db.add_suppression("name:jane doe|company:acme corp", "replied-no") is False

    suppressed, reason = await db.is_suppression_key("name:jane doe|company:acme corp")
    assert suppressed is True
    assert reason == "replied-no"

    assert [s["suppression_key"] for s in await db.list_suppressions()] == [
        "name:jane doe|company:acme corp"
    ]

    assert await db.remove_suppression("name:jane doe|company:acme corp") is True
    assert await db.remove_suppression("name:jane doe|company:acme corp") is False
    assert await db.is_suppression_key("name:jane doe|company:acme corp") == (False, None)


@pytest.mark.asyncio
async def test_check_lead_suppressed_matches_identity_then_domain(d1) -> None:
    db, _ = d1
    lead = _lead()

    assert await db.check_lead_suppressed(lead) == (False, None)

    await db.add_suppression(db.identity_key_from_lead(lead), "closed_lost")
    assert await db.check_lead_suppressed(lead) == (True, "closed_lost")

    await db.remove_suppression(db.identity_key_from_lead(lead))
    await db.add_suppression(db.domain_suppression_key("acme.com"), "icp_mismatch")
    assert await db.check_lead_suppressed(lead) == (True, "icp_mismatch")


@pytest.mark.asyncio
async def test_backfill_suppressions_seeds_terminal_leads_in_one_batch(d1) -> None:
    db, backend = d1
    closed = _lead(status=LeadStatus.CLOSED_LOST)
    await db.upsert(closed)

    # Force a re-run of the cached one-shot backfill.
    db._initialized = False
    backend.reset_log()
    await db.init()

    assert len(backend.batch_requests) == 1
    suppressed, reason = await db.is_suppression_key(db.identity_key_from_lead(closed))
    assert suppressed is True
    assert reason == "closed_lost"


# ── Outreach supersede (the send-guardrail-relevant path) ─────────────────────

@pytest.mark.asyncio
async def test_supersede_and_append_persist_in_one_update(d1) -> None:
    """Re-drafting must not leave a stale approvable draft. Because outreach
    history is one JSON column, the drop-and-append is a single UPDATE — there
    is no window where both the stale and the fresh draft are approvable."""
    db, backend = d1
    lead = _lead(status=LeadStatus.SCORED)
    stale = OutreachRecord(subject="Stale", body="old", sequence_step=0)
    lead.outreach_history.append(stale)
    await db.upsert(lead)

    backend.reset_log()
    fresh = OutreachRecord(subject="Fresh", body="new", sequence_step=0)
    assert lead.supersede_unapproved_drafts_at_step(0) == 1
    lead.outreach_history.append(fresh)
    await db.upsert(lead)

    writes = [s for s in backend.statements() if s.strip().startswith("UPDATE")]
    assert len(writes) == 1

    stored = await db.get(lead.id)
    assert [r.subject for r in stored.outreach_history] == ["Fresh"]
    approvable = [r for r in stored.outreach_history if not r.approved_at and not r.sent_at]
    assert len(approvable) == 1


# ── Cross-backend parity ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_d1_and_sqlite_reach_identical_lead_state(tmp_db_path) -> None:
    """The same call sequence must leave both backends in the same state.

    This is the regression guard against the two adapters drifting: if a future
    change touches one dedup path and not the other, local dev and production
    would disagree about what a duplicate is.
    """
    async def drive(db) -> dict:
        enriched = _lead(status=LeadStatus.ENRICHED)
        await db.upsert(enriched)
        thin = Lead(
            source=LeadSource.PDL,
            contact=ContactInfo(first_name="Jane", last_name="Doe", full_name="Jane Doe"),
            company=CompanyInfo(name="acme corp"),
        )
        await db.upsert(thin, dedupe_on_identity=True)
        other = _lead(
            status=LeadStatus.CLOSED_LOST,
            contact=ContactInfo(full_name="Bob Roe", email="bob@roe.com"),
            company=CompanyInfo(name="roe llc"),
        )
        await db.upsert(other)

        stored = await db.get(enriched.id)
        return {
            "count": await db.count_all(),
            "by_status": await db.count_by_status(),
            "email": stored.contact.email,
            "status": stored.status.value,
            "domain": stored.company.domain,
            "duplicates": sorted(k for k, _ in await db.find_duplicates()),
        }

    sqlite_db = LeadDatabase(tmp_db_path)
    await sqlite_db.init()
    sqlite_state = await drive(sqlite_db)

    backend = FakeD1Backend()
    with respx.mock:
        respx.post(DEFAULT_D1_URL).mock(side_effect=backend.handle)
        d1_db = D1Database()
        try:
            await d1_db.init()
            d1_state = await drive(d1_db)
        finally:
            await d1_db.aclose()
            backend.close()

    assert d1_state == sqlite_state


# ── Backend switch ────────────────────────────────────────────────────────────

def test_create_database_defaults_to_sqlite(test_config) -> None:
    db = create_database(test_config)
    assert isinstance(db, LeadDatabase)
    assert describe_backend(test_config)["backend"] == "sqlite"


def test_create_database_returns_d1_when_configured(test_config, monkeypatch) -> None:
    monkeypatch.delenv("LEADGEN_D1_URL", raising=False)
    test_config.database = DatabaseConfig(backend="d1")
    db = create_database(test_config)
    assert isinstance(db, D1Database)
    assert db.client.url == DEFAULT_D1_URL
    assert describe_backend(test_config) == {"backend": "d1", "url": DEFAULT_D1_URL}


def test_d1_url_can_be_pinned_in_config(test_config) -> None:
    test_config.database = DatabaseConfig(backend="d1", d1_url="http://db.internal:8080/query")
    db = create_database(test_config)
    assert db.client.url == "http://db.internal:8080/query"


def test_unknown_backend_is_rejected_at_config_load() -> None:
    with pytest.raises(ValueError, match="database.backend"):
        DatabaseConfig(backend="postgres")


def test_backend_value_is_normalized() -> None:
    assert DatabaseConfig(backend="  D1  ").backend == "d1"
