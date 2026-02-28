"""
Apollo.io Lead Source Connector
Pulls leads matching your ICP from the Apollo.io People Search API.
Docs: https://apolloio.github.io/apollo-api-docs/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.loader import APIKeys, LeadGenConfig
from src.models import CompanyInfo, ContactInfo, Lead, LeadSource

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/v1"


class ApolloConnector:
    """Fetches leads from Apollo.io People Search API."""

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.api_key = keys.apollo
        self.client = httpx.AsyncClient(
            base_url=APOLLO_BASE_URL,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            timeout=30.0,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _post(self, endpoint: str, payload: dict) -> dict:
        payload["api_key"] = self.api_key
        response = await self.client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.json()

    def _build_search_payload(
        self,
        page: int = 1,
        per_page: int = 25,
        extra_filters: dict | None = None,
    ) -> dict:
        """Build Apollo people search payload from ICP config."""
        icp = self.config.icp
        geo = icp.geography

        payload: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "person_titles": [],           # populate if you want specific titles
            "organization_industry_tag_ids": [],  # map industries if needed
            "person_locations": [],
            "organization_num_employees_ranges": [],
        }

        # Geography
        if geo.get("cities"):
            payload["person_locations"] = geo["cities"]
        elif geo.get("states"):
            payload["person_locations"] = [f"{s}, {geo.get('countries', ['US'])[0]}"
                                           for s in geo["states"]]
        elif geo.get("countries"):
            payload["person_locations"] = geo["countries"]

        # Employee range
        size = icp.company_size
        lo = size.get("min_employees", 1)
        hi = size.get("max_employees", 10000)
        payload["organization_num_employees_ranges"] = [f"{lo},{hi}"]

        # Industry keywords → Apollo q_organization_keyword_tags
        if icp.industries:
            payload["q_organization_keyword_tags"] = icp.industries

        if extra_filters:
            payload.update(extra_filters)

        return payload

    async def search(
        self,
        limit: int = 50,
        extra_filters: dict | None = None,
    ) -> list[Lead]:
        """
        Search Apollo for leads matching the configured ICP.

        Args:
            limit:         Max number of leads to return.
            extra_filters: Additional Apollo filter overrides.

        Returns:
            List of Lead objects.
        """
        if not self.api_key:
            raise ValueError("APOLLO_API_KEY is not set. Add it to your .env file.")

        leads: list[Lead] = []
        per_page = min(limit, 25)   # Apollo max per page is 25 on free tier
        page = 1

        while len(leads) < limit:
            payload = self._build_search_payload(
                page=page, per_page=per_page, extra_filters=extra_filters
            )
            logger.info(f"Fetching Apollo page {page} (want {limit - len(leads)} more leads)")

            try:
                data = await self._post("/mixed_people/search", payload)
            except httpx.HTTPStatusError as e:
                logger.error(f"Apollo API error: {e.response.status_code} — {e.response.text}")
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
        """Map Apollo person dict → Lead model."""
        org = person.get("organization") or {}

        contact = ContactInfo(
            first_name=person.get("first_name"),
            last_name=person.get("last_name"),
            full_name=person.get("name"),
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
