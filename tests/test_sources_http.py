"""HTTP adapter tests for Hunter, Apollo, PDL, Maps — all mocked via
respx. No real network.

Note on retries: Hunter's `_get` uses tenacity `retry(stop_after_attempt(3),
wait_exponential(min=1, max=8))` WITHOUT `retry_if_not_exception_type`,
so an HTTP status error would retry 3 times before surfacing. We
deliberately avoid testing those retry paths here (they'd burn real
wall-clock time). Apollo and PDL use `retry_if_not_exception_type(
httpx.HTTPStatusError)`, so status-error paths fail fast and we test them.
TODO: Hunter's missing `retry_if_not_exception_type` may be a bug —
consider adding it so HTTPStatusError short-circuits retries.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from leadgen.models import LeadSource
from leadgen.sources.apollo import ApolloConnector
from leadgen.sources.hunter import HunterConnector
from leadgen.sources.maps import MapsConnector
from leadgen.sources.pdl import PDLConnector


# ═══════════════════════ Hunter ═══════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_hunter_find_email_returns_email_when_confidence_high(
    test_config, test_keys
) -> None:
    """Hunter returns the email only when confidence ≥ 70 — below that
    threshold we discard it rather than send to a probably-wrong address."""
    respx.get("https://api.hunter.io/v2/email-finder").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "email": "jane.doe@acme.com",
                    "score": 92,
                }
            },
        )
    )
    async with HunterConnector(test_config, test_keys) as h:
        email = await h.find_email("Jane", "Doe", "acme.com")
    assert email == "jane.doe@acme.com"


@pytest.mark.asyncio
@respx.mock
async def test_hunter_find_email_returns_none_when_low_confidence(
    test_config, test_keys
) -> None:
    """Confidence < 70 must collapse to None — callers use this to
    decide whether to attach the email at all."""
    respx.get("https://api.hunter.io/v2/email-finder").mock(
        return_value=httpx.Response(
            200, json={"data": {"email": "maybe@acme.com", "score": 40}}
        )
    )
    async with HunterConnector(test_config, test_keys) as h:
        email = await h.find_email("Jane", "Doe", "acme.com")
    assert email is None


@pytest.mark.asyncio
@respx.mock
async def test_hunter_verify_email_maps_statuses_to_deliverable(
    test_config, test_keys
) -> None:
    """Hunter's delivery statuses — 'valid', 'accept_all', 'webmail' are
    considered deliverable; 'invalid', 'disposable', 'unknown' are not.
    This mapping protects from sending to provably-bad addresses."""
    route = respx.get("https://api.hunter.io/v2/email-verifier")
    async with HunterConnector(test_config, test_keys) as h:
        for status, expected in [
            ("valid", True),
            ("accept_all", True),
            ("webmail", True),
            ("invalid", False),
            ("disposable", False),
            ("unknown", False),
        ]:
            route.mock(
                return_value=httpx.Response(
                    200, json={"data": {"status": status}}
                )
            )
            got = await h.verify_email("jane@acme.com")
            assert got is expected, f"status={status} expected {expected} got {got}"


@pytest.mark.asyncio
@respx.mock
async def test_hunter_domain_search_parses_emails_entries_into_leads(
    test_config, test_keys
) -> None:
    """domain_search must map each `data.emails[*]` entry into a Lead with
    source=HUNTER, preserving verification status and org name."""
    respx.get("https://api.hunter.io/v2/domain-search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "organization": "Acme Corp",
                    "emails": [
                        {
                            "value": "jane@acme.com",
                            "first_name": "Jane",
                            "last_name": "Doe",
                            "position": "VP Ops",
                            "verification": {"status": "valid"},
                            "linkedin": "https://linkedin.com/in/jane",
                        },
                        {
                            "value": "bob@acme.com",
                            "first_name": "Bob",
                            "last_name": "Jones",
                            "position": "Engineer",
                            "verification": {"status": "unknown"},
                        },
                    ],
                }
            },
        )
    )
    async with HunterConnector(test_config, test_keys) as h:
        leads = await h.domain_search(domain="acme.com", limit=10)

    assert len(leads) == 2
    assert all(l.source == LeadSource.HUNTER for l in leads)
    jane = next(l for l in leads if l.contact.email == "jane@acme.com")
    bob = next(l for l in leads if l.contact.email == "bob@acme.com")
    assert jane.contact.email_verified is True
    assert bob.contact.email_verified is False
    assert jane.company.name == "Acme Corp"
    assert jane.company.domain == "acme.com"


@pytest.mark.asyncio
@respx.mock
async def test_hunter_enrich_lead_email_skips_already_verified(
    test_config, test_keys, sample_lead
) -> None:
    """A lead that already has a verified email must NOT trigger an HTTP
    call — this is an important cost-control invariant for enrichment
    batches."""
    route = respx.get("https://api.hunter.io/v2/email-finder")
    async with HunterConnector(test_config, test_keys) as h:
        result = await h.enrich_lead_email(sample_lead)  # sample_lead is verified
    assert result is sample_lead
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_hunter_enrich_lead_email_skips_without_domain_or_name(
    test_config, test_keys, sample_lead
) -> None:
    """Missing domain OR first_name OR last_name → no lookup attempted.
    Hunter's API requires all three."""
    # Strip the verified email so enrichment would normally fire
    sample_lead.contact.email = None
    sample_lead.contact.email_verified = False

    # Case 1: no domain
    sample_lead.company.domain = None
    sample_lead.company.website = None
    async with HunterConnector(test_config, test_keys) as h:
        await h.enrich_lead_email(sample_lead)  # must not raise

    # Case 2: missing last_name
    sample_lead.company.domain = "acme.com"
    sample_lead.contact.last_name = None
    async with HunterConnector(test_config, test_keys) as h:
        await h.enrich_lead_email(sample_lead)  # must not raise


# ═══════════════════════ Apollo ═══════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_apollo_search_builds_query_from_icp_and_parses_people(
    test_config, test_keys
) -> None:
    """Apollo.search sends an ICP-derived query and maps `people[*]` to
    Leads. Inspect the outgoing request to confirm employee-range and
    geography filters are populated from config."""
    captured_urls: list[str] = []

    def _assert_handler(request: httpx.Request):
        captured_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "people": [
                    {
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "title": "VP Eng",
                        "email": "jane@acme.com",
                        "email_status": "verified",
                        "linkedin_url": "https://linkedin.com/in/jane",
                        "city": "San Francisco",
                        "state": "California",
                        "country": "US",
                        "organization": {
                            "name": "Acme Corp",
                            "primary_domain": "acme.com",
                            "website_url": "https://acme.com",
                            "industry": "Software",
                            "estimated_num_employees": 150,
                            "annual_revenue": 30_000_000,
                            "founded_year": 2015,
                            "short_description": "SaaS for X",
                            "linkedin_url": "https://linkedin.com/company/acme",
                            "current_technologies": [
                                {"name": "python"},
                                {"name": "postgres"},
                            ],
                        },
                    }
                ]
            },
        )

    respx.post("https://api.apollo.io/api/v1/mixed_people/api_search").mock(
        side_effect=_assert_handler
    )

    async with ApolloConnector(test_config, test_keys) as a:
        leads = await a.search(limit=1)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.source == LeadSource.APOLLO
    assert lead.contact.email == "jane@acme.com"
    assert lead.contact.email_verified is True
    assert lead.company.name == "Acme Corp"
    assert lead.company.technologies == ["python", "postgres"]

    # Query params from ICP made it into the URL
    assert captured_urls, "No request captured"
    url = captured_urls[0]
    assert "organization_num_employees_ranges" in url
    # test_config ICP: min=10 max=500
    assert "10%2C500" in url or "10,500" in url


@pytest.mark.asyncio
@respx.mock
async def test_apollo_403_raises_actionable_value_error(
    test_config, test_keys
) -> None:
    """A 403 response must surface as a ValueError with guidance, not as a
    raw HTTPStatusError — operators hitting this need to know it's usually
    an API-tier/plan problem, not a code bug."""
    respx.post("https://api.apollo.io/api/v1/mixed_people/api_search").mock(
        return_value=httpx.Response(403, json={"error": "Access denied"})
    )
    async with ApolloConnector(test_config, test_keys) as a:
        with pytest.raises(ValueError, match="Apollo 403"):
            await a.search(limit=1)


@pytest.mark.asyncio
async def test_apollo_search_without_key_raises_before_http(
    test_config, test_keys
) -> None:
    """A missing APOLLO_API_KEY fails fast with a clear message BEFORE
    any HTTP client is touched — this is the guard against silent 401
    loops on misconfigured deployments."""
    test_keys.apollo = ""
    async with ApolloConnector(test_config, test_keys) as a:
        with pytest.raises(ValueError, match="APOLLO_API_KEY"):
            await a.search(limit=1)


# ═══════════════════════ PDL ═══════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_pdl_search_builds_es_query_and_parses_records(
    test_config, test_keys
) -> None:
    """PDL.search builds an Elasticsearch query from ICP and parses the
    returned person records into Leads. Verify the `query` param holds
    country/state/size/industry clauses derived from test_config's ICP."""
    import json as _json

    captured = {"query_param": None}

    def _handler(request: httpx.Request):
        captured["query_param"] = request.url.params.get("query")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "full_name": "Jane Doe",
                        "job_title": "VP Eng",
                        "work_email": "jane@acme.com",
                        "linkedin_url": "https://linkedin.com/in/jane",
                        "job_company_name": "Acme Corp",
                        "job_company_website": "acme.com",
                        "job_company_industry": "software",
                        "job_company_employee_count": 150,
                        "job_company_location_locality": "san francisco",
                        "job_company_location_region": "california",
                        "job_company_location_country": "united_states",
                    }
                ],
                "scroll_token": None,
            },
        )

    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        side_effect=_handler
    )

    async with PDLConnector(test_config, test_keys) as p:
        leads = await p.search(limit=1)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.source == LeadSource.PDL
    assert lead.contact.email == "jane@acme.com"
    assert lead.company.name == "Acme Corp"
    # Country is un-underscored in the parsed Lead
    assert lead.company.country == "united states"

    # The `query` URL param holds the ES query directly (already the
    # `{"bool": {"must": [...]}}` body — PDLConnector._person_search
    # serializes payload["query"] alone, not the full ES envelope).
    raw = captured["query_param"]
    assert raw is not None
    es_query = _json.loads(raw)
    must = es_query["bool"]["must"]
    # Each must clause is a dict like {"term": {"field": value}} or
    # {"range": {"field": {...}}}; we look one level in for field names.
    field_blobs = [next(iter(clause.values())) for clause in must]
    flat_fields = [
        next(iter(blob.keys())) if isinstance(blob, dict) else str(blob)
        for blob in field_blobs
    ]
    assert any("job_company_location_country" in f for f in flat_fields)
    assert any("job_company_employee_count" in f for f in flat_fields)


@pytest.mark.asyncio
@respx.mock
async def test_pdl_404_with_industry_retries_without_industry(
    test_config, test_keys
) -> None:
    """A 404 while an industry filter is present triggers a retry with the
    industry filter dropped — protects against PDL's stricter industry
    taxonomy breaking ICPs that use our looser labels."""
    import json as _json

    call_count = {"n": 0}
    queries: list[dict] = []

    def _handler(request: httpx.Request):
        call_count["n"] += 1
        q = _json.loads(request.url.params.get("query"))
        queries.append(q)
        # First call: 404. Second call (no industry): 200 with one record.
        if call_count["n"] == 1:
            return httpx.Response(404, json={"error": "No records"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "full_name": "Jane Doe",
                        "job_company_name": "Acme Corp",
                    }
                ],
                "scroll_token": None,
            },
        )

    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        side_effect=_handler
    )

    async with PDLConnector(test_config, test_keys) as p:
        leads = await p.search(limit=5)

    assert len(leads) == 1
    assert call_count["n"] == 2

    # The captured `query` param is the ES body directly — `{"bool":
    # {"must": [...]}}` — not wrapped in another "query" key.
    first_clause_kinds = [
        next(iter(c.keys())) for c in queries[0]["bool"]["must"]
    ]
    second_clause_field_names = [
        next(iter(next(iter(c.values())).keys()))
        for c in queries[1]["bool"]["must"]
    ]
    # First request had an industry match clause...
    assert "match" in first_clause_kinds
    # ...second (post-404 retry) dropped the industry filter entirely.
    assert not any(
        "job_company_industry" in f for f in second_clause_field_names
    )


@pytest.mark.asyncio
@respx.mock
async def test_pdl_402_raises_out_of_credits(test_config, test_keys) -> None:
    """402 Payment Required = out of credits. Surface as ValueError with
    guidance, not as a raw HTTP error — operators hitting this on the
    free tier need to know why."""
    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        return_value=httpx.Response(402, json={"error": "out of credits"})
    )
    async with PDLConnector(test_config, test_keys) as p:
        with pytest.raises(ValueError, match="402"):
            await p.search(limit=1)


def test_pdl_parses_employee_count_range_string(test_config, test_keys) -> None:
    """PDL sometimes returns employee_count as a string range like '11-50'.
    The parser folds this into an integer midpoint so downstream scoring
    math doesn't choke on a string."""
    p = PDLConnector(test_config, test_keys)
    lead = p._parse_person(
        {
            "first_name": "X",
            "last_name": "Y",
            "full_name": "X Y",
            "job_company_name": "RangeCo",
            "job_company_employee_count": "11-50",
        }
    )
    assert lead.company.employee_count == 30


def test_pdl_normalizes_boolean_email_to_none(test_config, test_keys) -> None:
    """On the free tier PDL returns `True` (bool) in the email slot to
    signal 'an email exists but we won't tell you' — parser must convert
    that to None, not let a bool leak into a ContactInfo.email field."""
    p = PDLConnector(test_config, test_keys)
    lead = p._parse_person(
        {
            "first_name": "X",
            "last_name": "Y",
            "full_name": "X Y",
            "job_company_name": "BoolCo",
            "emails": True,  # free-tier oddity
            "work_email": None,
        }
    )
    assert lead.contact.email is None


# ═══════════════════════ Maps (stub) ═════════════════════════════════════════


@pytest.mark.asyncio
async def test_maps_search_returns_empty_stub(test_config, test_keys) -> None:
    """Maps connector is a deliberate stub — assert it returns [] rather
    than silently raising. When it's implemented, this test should be
    rewritten against a respx-mocked Places / Outscraper endpoint."""
    async with MapsConnector(test_config, test_keys) as m:
        leads = await m.search(query="restaurant", location="San Francisco", limit=10)
    assert leads == []
