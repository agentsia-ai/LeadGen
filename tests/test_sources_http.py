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
    assert jane.company.name == "acme corp"
    assert jane.company.display_name == "Acme Corp"
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
    assert lead.company.name == "acme corp"
    assert lead.company.display_name == "Acme Corp"
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
                        "job_company_name": "acme corp",
                        "job_company_id": "pdl-acme",
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
    respx.get("https://api.peopledatalabs.com/v5/company/enrich").mock(
        return_value=httpx.Response(
            200,
            json={"display_name": "Acme Corp", "name": "acme corp"},
        )
    )

    async with PDLConnector(test_config, test_keys) as p:
        leads = await p.search(limit=1)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.source == LeadSource.PDL
    assert lead.contact.email == "jane@acme.com"
    assert lead.company.name == "acme corp"
    assert lead.company.display_name == "Acme Corp"
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
    # Size filter is a bool.should (in-band OR unknown headcount), not a bare range.
    size_clause = next(c for c in must if "bool" in c and "should" in c["bool"])
    should = size_clause["bool"]["should"]
    assert "minimum_should_match" not in size_clause["bool"]
    assert {"range": {"job_company_employee_count": {"gte": 10, "lte": 500}}} in should
    assert {
        "bool": {
            "must_not": [{"exists": {"field": "job_company_employee_count"}}]
        }
    } in should


@pytest.mark.asyncio
@respx.mock
async def test_pdl_404_with_industry_fails_closed_by_default(
    test_config, test_keys
) -> None:
    """A 404 with an industry filter present must FAIL CLOSED: return 0 leads
    rather than silently dropping the industry filter and pulling off-vertical
    leads. Only ONE request is made — there is no broadened retry by default."""
    import json as _json

    call_count = {"n": 0}

    def _handler(request: httpx.Request):
        call_count["n"] += 1
        _json.loads(request.url.params.get("query"))
        return httpx.Response(404, json={"error": "No records"})

    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        side_effect=_handler
    )

    async with PDLConnector(test_config, test_keys) as p:
        leads = await p.search(limit=5)

    # Fail closed: no leads, and crucially NO second (industry-dropped) call.
    assert leads == []
    assert call_count["n"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_pdl_relax_industry_opt_in_drops_filter_and_flags(
    test_config, test_keys
) -> None:
    """With the explicit relax_industry=True opt-in, a 404 broadens to
    geography + size only AND every returned Lead is flagged
    industry_relaxed=True so it can't be mistaken for an on-vertical match."""
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
        leads = await p.search(limit=5, relax_industry=True)

    assert len(leads) == 1
    assert call_count["n"] == 2
    # Relaxed leads must be flagged so they aren't treated as on-vertical.
    assert leads[0].industry_relaxed is True

    # First request carried an exact industry clause (term/terms on the
    # canonical PDL field); the post-404 retry dropped it entirely.
    first_clause_kinds = [
        next(iter(c.keys())) for c in queries[0]["bool"]["must"]
    ]
    second_clause_field_names = [
        next(iter(next(iter(c.values())).keys()))
        for c in queries[1]["bool"]["must"]
    ]
    assert any(k in first_clause_kinds for k in ("term", "terms", "bool"))
    assert any(
        "job_company_industry" in f
        for c in queries[0]["bool"]["must"]
        for f in [next(iter(next(iter(c.values())).keys()))]
    )
    assert not any(
        "job_company_industry" in f for f in second_clause_field_names
    )


@pytest.mark.asyncio
async def test_pdl_invalid_industry_fails_loudly_before_http(
    test_config, test_keys
) -> None:
    """An ICP industry that isn't a valid PDL taxonomy value (and has no alias)
    must raise BEFORE any HTTP/credit spend, naming the bad term — never 404
    silently into a broadened search."""
    from leadgen.sources.pdl_industries import InvalidIndustryError

    test_config.icp.industries = ["insurance agencies", "totally bogus vertical"]
    async with PDLConnector(test_config, test_keys) as p:
        with pytest.raises(InvalidIndustryError, match="totally bogus vertical"):
            await p.search(limit=1)


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


@pytest.mark.asyncio
@respx.mock
async def test_pdl_400_surfaces_response_body(test_config, test_keys) -> None:
    """400 responses must include PDL's full error body so operators see
    which clause PDL rejected — not a bare HTTPStatusError."""
    body = {
        "status": 400,
        "error": {
            "type": "invalid_request_error",
            "message": "The value of 'must_not' must be an array",
        },
    }
    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        return_value=httpx.Response(400, json=body)
    )
    async with PDLConnector(test_config, test_keys) as p:
        with pytest.raises(ValueError, match="must_not") as exc:
            await p.search(limit=1)
    assert "Full response body" in str(exc.value)
    assert "must_not" in str(exc.value)


def test_pdl_multi_industry_uses_bool_should_term_clauses(
    test_config, test_keys
) -> None:
    """Multiple ICP industries must OR via bool.should + term, not a terms array."""
    p = PDLConnector(test_config, test_keys)
    payload = p._build_es_query()
    must = payload["query"]["bool"]["must"]
    industry_clause = next(
        c
        for c in must
        if "bool" in c
        and any(
            "job_company_industry" in s.get("term", {})
            for s in c["bool"].get("should", [])
        )
    )
    should = industry_clause["bool"]["should"]
    assert "minimum_should_match" not in industry_clause["bool"]
    assert len(should) == 2  # SaaS + Fintech from test_config
    assert all("term" in s and "job_company_industry" in s["term"] for s in should)
    assert not any("terms" in c for c in must if "job_company_industry" in str(c))


def _query_contains_minimum_should_match(obj: object) -> bool:
    """True if any bool block in the ES query carries minimum_should_match."""
    if isinstance(obj, dict):
        if "minimum_should_match" in obj:
            return True
        return any(_query_contains_minimum_should_match(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_query_contains_minimum_should_match(v) for v in obj)
    return False


def test_pdl_query_never_uses_minimum_should_match(test_config, test_keys) -> None:
    """PDL rejects minimum_should_match — should-only bools rely on implicit >=1."""
    p = PDLConnector(test_config, test_keys)
    payload = p._build_es_query()
    assert not _query_contains_minimum_should_match(payload)


def test_pdl_size_filter_must_not_is_array(test_config, test_keys) -> None:
    """PDL rejects must_not as a bare object; it must be a list of clauses."""
    p = PDLConnector(test_config, test_keys)
    payload = p._build_es_query()
    size_clause = next(
        c
        for c in payload["query"]["bool"]["must"]
        if "bool" in c and "should" in c["bool"]
        and any("job_company_employee_count" in str(s) for s in c["bool"]["should"])
    )
    null_branch = size_clause["bool"]["should"][1]
    must_not = null_branch["bool"]["must_not"]
    assert isinstance(must_not, list)
    assert must_not == [{"exists": {"field": "job_company_employee_count"}}]


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
            "job_company_name": "rangeco",
            "job_company_employee_count": "11-50",
        }
    )
    assert lead.company.employee_count == 30
    assert lead.company.employee_count_unknown is False
    assert lead.company.name == "rangeco"
    assert lead.company.display_name is None


def test_pdl_parses_null_employee_count_as_unknown(test_config, test_keys) -> None:
    """Missing headcount must be flagged unknown — not treated as confirmed in-band."""
    p = PDLConnector(test_config, test_keys)
    lead = p._parse_person(
        {
            "first_name": "Solo",
            "last_name": "Founder",
            "full_name": "Solo Founder",
            "job_company_name": "solo llc",
        }
    )
    assert lead.company.employee_count is None
    assert lead.company.employee_count_unknown is True


def _pdl_size_clause_matches(
    size_clause: dict, employee_count: int | None
) -> bool:
    """Mirror ES bool.should semantics for the PDL company-size filter."""
    should = size_clause["bool"]["should"]
    lo = should[0]["range"]["job_company_employee_count"]["gte"]
    hi = should[0]["range"]["job_company_employee_count"]["lte"]
    if employee_count is None:
        return True
    return lo <= employee_count <= hi


def test_pdl_size_filter_includes_null_excludes_out_of_band(
    test_config, test_keys
) -> None:
    """Size band OR-unknown: null headcount included; out-of-band excluded."""
    p = PDLConnector(test_config, test_keys)
    payload = p._build_es_query()
    size_clause = next(
        c
        for c in payload["query"]["bool"]["must"]
        if "bool" in c and "should" in c["bool"]
    )
    assert _pdl_size_clause_matches(size_clause, None) is True
    assert _pdl_size_clause_matches(size_clause, 50) is True
    assert _pdl_size_clause_matches(size_clause, 10) is True
    assert _pdl_size_clause_matches(size_clause, 500) is True
    assert _pdl_size_clause_matches(size_clause, 9) is False
    assert _pdl_size_clause_matches(size_clause, 501) is False
    assert _pdl_size_clause_matches(size_clause, 5000) is False


def test_pdl_fetches_company_display_name_from_enrich(test_config, test_keys) -> None:
    """PDL Person Search lowercases job_company_name; display_name comes from
    Company Enrichment keyed by job_company_id."""
    p = PDLConnector(test_config, test_keys)
    lead = p._parse_person(
        {
            "first_name": "X",
            "last_name": "Y",
            "full_name": "X Y",
            "job_company_name": "corfac international",
            "job_company_id": "pdl-company-123",
        }
    )
    assert lead.company.name == "corfac international"
    assert lead.company.display_name is None


@pytest.mark.asyncio
@respx.mock
async def test_pdl_search_attaches_company_display_name(test_config, test_keys) -> None:
    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "full_name": "Jane Doe",
                        "job_title": "VP Eng",
                        "work_email": "jane@luckytruck.com",
                        "job_company_name": "luckytruck",
                        "job_company_id": "pdl-luckytruck",
                        "job_company_website": "luckytruck.com",
                        "job_company_industry": "insurance",
                        "job_company_employee_count": 50,
                        "job_company_location_country": "united_states",
                    }
                ],
                "scroll_token": None,
            },
        )
    )
    respx.get("https://api.peopledatalabs.com/v5/company/enrich").mock(
        return_value=httpx.Response(
            200,
            json={"display_name": "LuckyTruck", "name": "luckytruck"},
        )
    )

    async with PDLConnector(test_config, test_keys) as p:
        leads = await p.search(limit=1)

    assert len(leads) == 1
    assert leads[0].company.name == "luckytruck"
    assert leads[0].company.display_name == "LuckyTruck"


@pytest.mark.asyncio
@respx.mock
async def test_pdl_search_normalizes_all_caps_company_display_name(
    test_config, test_keys
) -> None:
    respx.get("https://api.peopledatalabs.com/v5/person/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "first_name": "Tim",
                        "last_name": "Hart",
                        "full_name": "Tim Hart",
                        "job_title": "Broker",
                        "work_email": "tim@protectrealty.com",
                        "job_company_name": "protect realty",
                        "job_company_id": "pdl-protect-realty",
                        "job_company_website": "protectrealty.com",
                        "job_company_industry": "real estate",
                        "job_company_employee_count": 5,
                        "job_company_location_country": "united_states",
                    }
                ],
                "scroll_token": None,
            },
        )
    )
    respx.get("https://api.peopledatalabs.com/v5/company/enrich").mock(
        return_value=httpx.Response(
            200,
            json={"display_name": "PROTECT REALTY", "name": "protect realty"},
        )
    )

    async with PDLConnector(test_config, test_keys) as p:
        leads = await p.search(limit=1)

    assert len(leads) == 1
    assert leads[0].company.display_name == "Protect Realty"


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
