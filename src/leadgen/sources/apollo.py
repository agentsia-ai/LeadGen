"""
Apollo.io Lead Source Connector
Pulls leads matching your ICP from the Apollo.io People Search API.
Docs: https://apolloio.github.io/apollo-api-docs/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"


class ApolloConnector:
    """Fetches leads from Apollo.io People API Search."""

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.api_key = keys.apollo
        self.client = httpx.AsyncClient(
            base_url=APOLLO_BASE_URL,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "x-api-key": self.api_key,
            },
            timeout=30.0,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    def _build_query_params(
        self,
        page: int = 1,
        per_page: int = 25,
        extra_filters: dict | None = None,
    ) -> dict:
        """Build Apollo api_search query params from ICP config."""
        icp = self.config.icp
        geo = icp.geography

        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }

        # Geography — person_locations or organization_locations
        # Apollo expects "California, US" or "Illinois" format
        if geo.get("cities"):
            params["organization_locations"] = geo["cities"]
        elif geo.get("states"):
            params["organization_locations"] = [
                f"{s}, {geo.get('countries', ['US'])[0]}" for s in geo["states"]
            ]
        elif geo.get("countries"):
            params["organization_locations"] = geo["countries"]

        # Employee range
        size = icp.company_size
        lo = size.get("min_employees", 1)
        hi = size.get("max_employees", 10000)
        params["organization_num_employees_ranges"] = [f"{lo},{hi}"]

        # Industry keywords
        if icp.industries:
            params["q_keywords"] = " ".join(icp.industries[:5])

        if extra_filters:
            params.update(extra_filters)

        return params

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        retry=retry_if_not_exception_type(httpx.HTTPStatusError),
    )
    async def _api_search(self, params: dict) -> dict:
        """Call mixed_people/api_search with query params."""
        # Apollo expects array params as key[]=val&key[]=val
        flat_params: list[tuple[str, str]] = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    flat_params.append((f"{k}[]", str(item)))
            elif v is not None and v != "":
                flat_params.append((k, str(v)))

        response = await self.client.post(
            "/mixed_people/api_search",
            params=flat_params,
        )
        response.raise_for_status()
        return response.json()

    async def search(
        self,
        limit: int = 50,
        extra_filters: dict | None = None,
    ) -> list[Lead]:
        """
        Search Apollo for leads matching the configured ICP.

        Args:
            limit:         Max number of leads to return.
            extra_filters:  Additional Apollo filter overrides.

        Returns:
            List of Lead objects.
        """
        if not self.api_key:
            raise ValueError("APOLLO_API_KEY is not set. Add it to your .env file.")

        leads: list[Lead] = []
        per_page = min(limit, 25)   # Apollo max per page is 25 on free tier
        page = 1

        while len(leads) < limit:
            params = self._build_query_params(
                page=page, per_page=per_page, extra_filters=extra_filters
            )
            logger.info(f"Fetching Apollo page {page} (want {limit - len(leads)} more leads)")

            try:
                data = await self._api_search(params)
            except httpx.HTTPStatusError as e:
                logger.error(f"Apollo API error: {e.response.status_code} — {e.response.text}")
                if e.response.status_code == 403:
                    try:
                        err = e.response.json()
                        msg = err.get("error", err.get("message", str(err)))
                    except Exception:
                        msg = (e.response.text[:200] if e.response.text else "Access denied")
                    raise ValueError(
                        f"Apollo 403 Forbidden: {msg}\n"
                        "This may mean: (1) API key lacks People API Search access, "
                        "(2) your Apollo plan doesn't include this endpoint, or "
                        "(3) the key is invalid. Try the health check: leadgen apollo-test"
                    ) from e
                break

            people = data.get("people", [])
            if not people:
                logger.info("No more results from Apollo.")
                break

            for person in people:
                lead = self._parse_person(person)
                leads.append(lead)
                if len(leads) >= limit:
                    break

            page += 1

        logger.info(f"Apollo returned {len(leads)} leads.")
        return leads

    def _parse_person(self, person: dict) -> Lead:
        """Map Apollo api_search person dict → Lead model.

        Note: api_search does not return email/phone. Use Hunter enrichment to find emails.
        """
        org = person.get("organization") or {}
        first = person.get("first_name")
        last = person.get("last_name") or person.get("last_name_obfuscated", "")
        full = f"{first or ''} {last or ''}".strip() if (first or last) else None

        contact = ContactInfo(
            first_name=first,
            last_name=last if last and "*" not in str(last) else None,
            full_name=full,
            title=person.get("title"),
            email=person.get("email"),
            email_verified=(person.get("email_status") == "verified"),
            linkedin_url=person.get("linkedin_url"),
            phone=person.get("sanitized_phone"),
        )

        company = CompanyInfo(
            name=org.get("name", "Unknown"),
            domain=org.get("primary_domain"),
            website=org.get("website_url"),
            industry=org.get("industry"),
            employee_count=org.get("estimated_num_employees"),
            annual_revenue=org.get("annual_revenue"),
            founded_year=org.get("founded_year"),
            description=org.get("short_description"),
            city=person.get("city"),
            state=person.get("state"),
            country=person.get("country", "US"),
            linkedin_url=org.get("linkedin_url"),
            technologies=[t.get("name", "") for t in org.get("current_technologies", [])],
        )

        return Lead(
            source=LeadSource.APOLLO,
            contact=contact,
            company=company,
            raw_data=person,
        )
