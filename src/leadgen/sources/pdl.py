"""
People Data Labs (PDL) Lead Source Connector
ICP-based people search — Apollo alternative with 100 free credits/month.
Docs: https://docs.peopledatalabs.com/docs/person-search-api
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource, split_company_names
from leadgen.sources.pdl_industries import BROADER_THAN_LABEL, validate_and_resolve

logger = logging.getLogger(__name__)

PDL_BASE_URL = "https://api.peopledatalabs.com/v5"


def _pdl_error_detail(response: httpx.Response) -> str:
    """Extract a human-readable message from a PDL error response body."""
    body = response.text or ""
    if not body:
        return f"HTTP {response.status_code} (empty body)"
    try:
        err = response.json()
    except Exception:
        return body
    if isinstance(err.get("error"), dict):
        msg = err["error"].get("message")
        if msg:
            return f"{err['error'].get('type', 'error')}: {msg}"
    for key in ("message", "error"):
        val = err.get(key)
        if isinstance(val, str) and val:
            return val
    return body


# PDL uses lowercase for location/industry values
COUNTRY_ALIASES = {"us": "united states", "usa": "united states", "uk": "united kingdom"}


class PDLConnector:
    """Fetches leads from People Data Labs Person Search API."""

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.api_key = keys.pdl
        # Set True only when an explicit opt-in industry relaxation actually
        # fires, so every Lead parsed afterwards is flagged industry_relaxed.
        self._industry_relaxed = False
        self._industry_terms: list[str] | None = None
        # job_company_id -> display_name (or "" when enrich returned nothing)
        self._company_display_cache: dict[str, str] = {}
        self.client = httpx.AsyncClient(
            base_url=PDL_BASE_URL,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self.api_key,
            },
            timeout=30.0,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    def _normalize_country(self, country: str) -> str:
        """Map config country to PDL lowercase value."""
        c = country.strip().lower()
        return COUNTRY_ALIASES.get(c, c.replace(" ", "_"))

    def _pdl_industry_terms(self) -> list[str]:
        """Resolve configured ICP industries to valid PDL taxonomy values.

        Validates EVERY configured industry against PDL's fixed vocabulary
        (via the mapping layer) and FAILS LOUDLY on any term that isn't a
        canonical value or a known alias. This runs before any query is sent
        so an invalid term can never silently 404 into a broadened search.

        Result is cached for the lifetime of the connector. Raises
        InvalidIndustryError (a ValueError) on the first invalid term.
        """
        if self._industry_terms is None:
            self._industry_terms = validate_and_resolve(self.config.icp.industries)
            for label in self.config.icp.industries:
                caveat = BROADER_THAN_LABEL.get(label.strip().lower())
                if caveat:
                    logger.warning(
                        "PDL industry %r maps to a BROADER taxonomy value: %s. "
                        "The industry filter alone can't isolate this sub-segment - "
                        "narrow with company size and keyword/title filters.",
                        label,
                        caveat,
                    )
        return self._industry_terms

    def _build_es_query(
        self,
        size: int = 25,
        scroll_token: str | None = None,
        extra_filters: dict | None = None,
        skip_industry: bool = False,
    ) -> dict:
        """Build PDL Elasticsearch query from ICP config."""
        icp = self.config.icp
        geo = icp.geography
        must: list[dict] = []

        # Geography — job_company_location_country, job_company_location_region
        countries = geo.get("countries") or ["US"]
        country_terms = [self._normalize_country(c) for c in countries]
        if len(country_terms) == 1:
            must.append({"term": {"job_company_location_country": country_terms[0]}})
        else:
            must.append({"terms": {"job_company_location_country": country_terms}})

        if geo.get("states"):
            state_terms = [s.strip().lower().replace(" ", "_") for s in geo["states"]]
            if len(state_terms) == 1:
                must.append({"term": {"job_company_location_region": state_terms[0]}})
            else:
                must.append({"terms": {"job_company_location_region": state_terms}})

        # Company size — in-band OR headcount unknown/missing. A plain range in
        # `must` silently drops null job_company_employee_count (common for
        # solopreneurs); wrap as should so unknown-size records stay eligible.
        # No minimum_should_match — PDL forbids it; should-only bools match >=1.
        size_cfg = icp.company_size
        lo = size_cfg.get("min_employees", 1)
        hi = size_cfg.get("max_employees", 10000)
        must.append(
            {
                "bool": {
                    "should": [
                        {"range": {"job_company_employee_count": {"gte": lo, "lte": hi}}},
                        {
                            "bool": {
                                # PDL's ES subset requires must_not as a list, not a
                                # bare clause object — a dict here yields 400.
                                "must_not": [
                                    {"exists": {"field": "job_company_employee_count"}}
                                ]
                            }
                        },
                    ],
                }
            }
        )

        # Industry — exact match against PDL's canonical taxonomy. job_company_industry
        # is a keyword enum. Multi-industry uses bool.should of term clauses (OR),
        # not a terms array — PDL accepts term/bool but rejects some terms[] shapes
        # on this field. Omit minimum_should_match: PDL forbids that key; a
        # should-only bool implicitly requires >=1 match. Terms are pre-validated
        # in search(); skip_industry is only set by an explicit, opt-in relaxation.
        if icp.industries and not skip_industry:
            terms = self._pdl_industry_terms()
            if len(terms) == 1:
                must.append({"term": {"job_company_industry": terms[0]}})
            elif terms:
                must.append(
                    {
                        "bool": {
                            "should": [
                                {"term": {"job_company_industry": t}} for t in terms
                            ],
                        }
                    }
                )

        # Prefer records with email (free tier may not return value, but structure exists)
        must.append({"exists": {"field": "emails"}})

        if extra_filters:
            must.extend(extra_filters.get("must", []))

        es_query: dict[str, Any] = {
            "query": {"bool": {"must": must}},
            "size": min(size, 100),  # PDL max 100 per request
            "dataset": "email",  # Prefer email dataset
        }
        if scroll_token:
            es_query["scroll_token"] = scroll_token

        return es_query

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        retry=retry_if_not_exception_type(httpx.HTTPStatusError),
    )
    async def _person_search(self, payload: dict) -> dict:
        """Call PDL Person Search API (GET with JSON body as params)."""
        # PDL expects query as JSON string in params for GET
        params: dict[str, str | int | bool] = {
            "size": payload.get("size", 25),
            "dataset": payload.get("dataset", "email"),
        }
        if "query" in payload:
            params["query"] = json.dumps(payload["query"])
        if "scroll_token" in payload:
            params["scroll_token"] = payload["scroll_token"]

        response = await self.client.get("/person/search", params=params)
        if response.status_code == 400:
            detail = _pdl_error_detail(response)
            logger.error("PDL API error: 400 — %s", response.text)
            raise httpx.HTTPStatusError(
                f"PDL 400 Bad Request: {detail}",
                request=response.request,
                response=response,
            )
        response.raise_for_status()
        return response.json()

    async def search(
        self,
        limit: int = 50,
        extra_filters: dict | None = None,
        relax_industry: bool = False,
    ) -> list[Lead]:
        """
        Search PDL for leads matching the configured ICP.

        Note: Each returned person consumes 1 credit. Free tier = 100 credits/month.

        Args:
            limit:          Max number of leads to return.
            extra_filters:  Additional ES query filters (must clauses).
            relax_industry: Opt-in escape hatch. When False (the default), an
                            industry query that matches nothing FAILS CLOSED:
                            we return an empty list rather than silently dropping
                            the industry filter and returning off-vertical leads.
                            When True, we explicitly broaden (geography + size
                            only) AND flag every returned Lead with
                            ``industry_relaxed=True`` so relaxed results can't be
                            mistaken for on-vertical matches.

        Returns:
            List of Lead objects.
        """
        if not self.api_key:
            raise ValueError(
                "PDL_API_KEY is not set. Provide it as an environment variable "
                "(set it in Doppler for production, or a local .env for development)."
            )

        # Validate industries up front (FAIL LOUDLY before spending any credits).
        # An invalid term raises here instead of 404-ing into a broadened search.
        if self.config.icp.industries:
            self._pdl_industry_terms()

        leads: list[Lead] = []
        size = min(limit, 100)
        scroll_token: str | None = None

        skip_industry = False  # Only set True by an explicit opt-in relaxation
        while len(leads) < limit:
            payload = self._build_es_query(
                size=min(size, limit - len(leads)),
                scroll_token=scroll_token,
                extra_filters=extra_filters,
                skip_industry=skip_industry,
            )
            logger.info(
                f"Fetching PDL page (want {limit - len(leads)} more leads, "
                f"credits used: {len(leads)}{', relaxed query (no industry)' if skip_industry else ''})"
            )

            try:
                data = await self._person_search(payload)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    if not skip_industry and self.config.icp.industries:
                        if relax_industry:
                            # EXPLICIT opt-in only: broaden to geography + size and
                            # flag every resulting Lead so off-vertical results are
                            # never mistaken for on-vertical matches.
                            skip_industry = True
                            self._industry_relaxed = True
                            scroll_token = None  # Reset for new search
                            logger.warning(
                                "PDL: industry filter %s matched 0 records. "
                                "relax_industry=True -> retrying WITHOUT industry "
                                "(geography + size only). Returned leads are flagged "
                                "industry_relaxed=True and are NOT on-vertical.",
                                self._pdl_industry_terms(),
                            )
                            continue
                        # FAIL CLOSED (default): do NOT silently drop the industry
                        # filter. Return empty rather than off-vertical leads.
                        logger.warning(
                            "PDL: industry filter %s matched 0 records. Failing "
                            "closed — returning 0 leads instead of dropping the "
                            "industry filter and returning off-vertical leads. "
                            "Verify the term is a valid PDL taxonomy value, or pass "
                            "relax_industry=True to explicitly opt into a broadened "
                            "(flagged) search.",
                            self._pdl_industry_terms(),
                        )
                        break
                    logger.info(
                        "PDL: No records matched your search. Try relaxing filters "
                        "(e.g. broader geography)."
                    )
                    break
                if e.response.status_code == 403:
                    logger.error(f"PDL API error: 403 — {e.response.text}")
                    try:
                        err = e.response.json()
                        msg = err.get("error", err.get("message", str(err)))
                    except Exception:
                        msg = e.response.text[:200] if e.response.text else "Access denied"
                    raise ValueError(
                        f"PDL 403 Forbidden: {msg}\n"
                        "Check your API key and plan at https://peopledatalabs.com"
                    ) from e
                if e.response.status_code == 402:
                    raise ValueError(
                        "PDL 402 Payment Required: Out of credits. "
                        "Free tier: 100 credits/month. Upgrade or wait for reset."
                    ) from e
                if e.response.status_code == 400:
                    detail = _pdl_error_detail(e.response)
                    logger.error("PDL API error: 400 — %s", e.response.text)
                    raise ValueError(
                        f"PDL 400 Bad Request (malformed query): {detail}\n"
                        f"Full response body:\n{e.response.text}"
                    ) from e
                logger.error(f"PDL API error: {e.response.status_code} — {e.response.text}")
                raise

            records = data.get("data", [])
            scroll_token = data.get("scroll_token")

            if not records:
                logger.info("No more results from PDL.")
                break

            for person in records:
                lead = self._parse_person(person)
                await self._attach_company_display_name(lead, person)
                leads.append(lead)
                if len(leads) >= limit:
                    break

            if not scroll_token:
                break

        unknown_headcount = sum(1 for lead in leads if lead.company.employee_count_unknown)
        if unknown_headcount:
            logger.info(
                "PDL: %d/%d lead(s) have unknown company headcount "
                "(included via size-band OR-unknown filter, not confirmed in-band)",
                unknown_headcount,
                len(leads),
            )
        logger.info(f"PDL returned {len(leads)} leads ({len(leads)} credits used).")
        return leads

    def _safe_phone(self, val: str | bool | None) -> str | None:
        """Free tier returns True for phone existence; we need None or actual string."""
        return val if isinstance(val, str) else None

    def _first_phone(self, phones: list | None) -> str | None:
        """Extract first phone from PDL phone_numbers (list of strings or dicts)."""
        if not phones:
            return None
        first = phones[0]
        return first.get("number", first) if isinstance(first, dict) else first

    @staticmethod
    def _normalize_domain(value: str | None) -> str | None:
        """Strip protocol / www / path off a raw URL or domain → bare host.

        Returns None for blanks. Mirrors HunterConnector._normalize_domain so a
        domain captured here feeds straight into Hunter without re-cleaning.
        """
        if not value or not isinstance(value, str):
            return None
        value = value.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if value.startswith(prefix):
                value = value[len(prefix):]
        value = value.split("/")[0]
        # Reject obviously non-domain values (PDL sometimes echoes a bare
        # company name into website on partial records).
        return value if value and "." in value else None

    def _company_domain(self, person: dict) -> str | None:
        """Pull a usable company domain from whatever PDL actually returned.

        PDL's person schema only carries a real domain in `job_company_website`
        (free tier frequently nulls it). We still check a couple of historical
        aliases defensively. LinkedIn/Facebook/Twitter URLs are NOT domains and
        are intentionally ignored here — name->domain resolution is Hunter's job.
        """
        for field in ("job_company_website", "job_company_domain", "company_website"):
            domain = self._normalize_domain(person.get(field))
            if domain:
                return domain
        return None

    def _parse_person(self, person: dict) -> Lead:
        """Map PDL person record → Lead model.

        PDL returns flat records with job_*, job_company_* prefixes.
        Free tier may return true/false for emails; Pro returns addresses.
        """
        # Emails: free tier returns true/false (hides actual value); Pro returns addresses
        email = person.get("work_email")
        if not email:
            emails = person.get("emails", [])
            if isinstance(emails, list) and emails:
                first = emails[0]
                if isinstance(first, dict) and "address" in first:
                    email = first.get("address")
                elif isinstance(first, str):
                    email = first
        if not isinstance(email, str):
            email = None  # Free tier returns True; we need None for ContactInfo

        # PDL uses flat job_company_* fields
        raw_emp_count = person.get("job_company_employee_count")
        emp_count = raw_emp_count
        if isinstance(emp_count, str) and "-" in str(emp_count):
            parts = str(emp_count).replace("+", "").split("-")
            try:
                emp_count = (int(parts[0]) + int(parts[1])) // 2
            except (ValueError, IndexError):
                emp_count = None
        elif emp_count is not None and not isinstance(emp_count, int):
            try:
                emp_count = int(emp_count)
            except (TypeError, ValueError):
                emp_count = None
        employee_count_unknown = emp_count is None
        if employee_count_unknown:
            logger.debug(
                "PDL lead %s @ %s: job_company_employee_count missing/unknown",
                person.get("full_name") or person.get("first_name"),
                person.get("job_company_name"),
            )

        contact = ContactInfo(
            first_name=person.get("first_name"),
            last_name=person.get("last_name"),
            full_name=person.get("full_name"),
            title=person.get("job_title"),
            email=email,
            email_verified=False,  # PDL doesn't verify; use Hunter
            linkedin_url=person.get("linkedin_url"),
            phone=self._safe_phone(person.get("mobile_phone") or self._first_phone(person.get("phone_numbers"))),
        )

        domain = self._company_domain(person)
        # Preserve the original website string if present (it may carry a path
        # we want for display), but always store a clean bare host in `domain`
        # so Hunter enrichment has something canonical to search against.
        website = person.get("job_company_website") or (f"https://{domain}" if domain else None)
        match_key, display_name = split_company_names(person.get("job_company_name", "Unknown"))
        comp_info = CompanyInfo(
            name=match_key,
            display_name=display_name,
            domain=domain,
            website=website,
            industry=person.get("job_company_industry"),
            employee_count=emp_count,
            employee_count_unknown=employee_count_unknown,
            city=person.get("job_company_location_locality"),
            state=person.get("job_company_location_region"),
            country=person.get("job_company_location_country", "us").replace("_", " "),
            linkedin_url=person.get("job_company_linkedin_url"),
        )

        return Lead(
            source=LeadSource.PDL,
            contact=contact,
            company=comp_info,
            raw_data=person,
            # True only when an explicit relax_industry opt-in dropped the
            # industry filter — marks this lead as NOT verified on-vertical.
            industry_relaxed=self._industry_relaxed,
        )

    async def _fetch_company_display_name(self, company_id: str) -> str | None:
        """PDL Person Search lowercases job_company_name; proper casing lives on
        the Company record's display_name field (one enrich credit per company)."""
        if company_id in self._company_display_cache:
            cached = self._company_display_cache[company_id]
            return cached or None
        if not self.api_key:
            return None
        try:
            response = await self.client.get(
                "/company/enrich",
                params={"pdl_id": company_id},
            )
            if response.status_code == 404:
                self._company_display_cache[company_id] = ""
                return None
            response.raise_for_status()
            display = (response.json().get("display_name") or "").strip()
            self._company_display_cache[company_id] = display
            return display or None
        except httpx.HTTPStatusError as e:
            logger.debug(
                "PDL company enrich failed for %s: %s",
                company_id,
                e.response.status_code,
            )
            self._company_display_cache[company_id] = ""
            return None

    async def _attach_company_display_name(self, lead: Lead, person: dict) -> None:
        company_id = person.get("job_company_id")
        if not company_id or lead.company.display_name:
            return
        display = await self._fetch_company_display_name(company_id)
        if display:
            lead.company.display_name = display
