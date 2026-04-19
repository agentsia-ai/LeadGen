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
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource

logger = logging.getLogger(__name__)

PDL_BASE_URL = "https://api.peopledatalabs.com/v5"


# PDL uses lowercase for location/industry values
COUNTRY_ALIASES = {"us": "united states", "usa": "united states", "uk": "united kingdom"}


class PDLConnector:
    """Fetches leads from People Data Labs Person Search API."""

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.api_key = keys.pdl
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

        # Company size — job_company_employee_count range
        size_cfg = icp.company_size
        lo = size_cfg.get("min_employees", 1)
        hi = size_cfg.get("max_employees", 10000)
        must.append({"range": {"job_company_employee_count": {"gte": lo, "lte": hi}}})

        # Industry keywords — single match on first industry (PDL restricts query options)
        if icp.industries and not skip_industry:
            industry_query = icp.industries[0].lower()
            must.append({"match": {"job_company_industry": industry_query}})

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
        response.raise_for_status()
        return response.json()

    async def search(
        self,
        limit: int = 50,
        extra_filters: dict | None = None,
    ) -> list[Lead]:
        """
        Search PDL for leads matching the configured ICP.

        Note: Each returned person consumes 1 credit. Free tier = 100 credits/month.

        Args:
            limit:         Max number of leads to return.
            extra_filters:  Additional ES query filters (must clauses).

        Returns:
            List of Lead objects.
        """
        if not self.api_key:
            raise ValueError("PDL_API_KEY is not set. Add it to your .env file.")

        leads: list[Lead] = []
        size = min(limit, 100)
        scroll_token: str | None = None

        skip_industry = False  # Set True on 404 fallback
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
                        # Fallback: retry without industry filter (PDL taxonomy may differ)
                        skip_industry = True
                        scroll_token = None  # Reset for new search
                        logger.info("PDL: No results with industry filter. Retrying with broader query (geography + size only)...")
                        continue
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
                logger.error(f"PDL API error: {e.response.status_code} — {e.response.text}")
                raise

            records = data.get("data", [])
            scroll_token = data.get("scroll_token")

            if not records:
                logger.info("No more results from PDL.")
                break

            for person in records:
                lead = self._parse_person(person)
                leads.append(lead)
                if len(leads) >= limit:
                    break

            if not scroll_token:
                break

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
        emp_count = person.get("job_company_employee_count")
        if isinstance(emp_count, str) and "-" in str(emp_count):
            parts = str(emp_count).replace("+", "").split("-")
            try:
                emp_count = (int(parts[0]) + int(parts[1])) // 2
            except (ValueError, IndexError):
                emp_count = None

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

        comp_info = CompanyInfo(
            name=person.get("job_company_name", "Unknown"),
            domain=person.get("job_company_website"),
            website=person.get("job_company_website"),
            industry=person.get("job_company_industry"),
            employee_count=emp_count,
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
        )
