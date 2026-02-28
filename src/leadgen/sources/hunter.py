"""
Hunter.io Lead Source Connector
Two use cases:
  1. Find email addresses for contacts sourced elsewhere (Apollo, CSV, etc.)
  2. Domain search — find all contacts at a target company

Docs: https://hunter.io/api-documentation
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import ContactInfo, CompanyInfo, Lead, LeadSource

logger = logging.getLogger(__name__)

HUNTER_BASE_URL = "https://api.hunter.io/v2"


class HunterConnector:
    """
    Finds and verifies email addresses via Hunter.io.

    Primary use: given a name + company domain, find a verified email.
    Secondary use: domain search to find all contacts at a company.
    """

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.api_key = keys.hunter
        self.client = httpx.AsyncClient(
            base_url=HUNTER_BASE_URL,
            timeout=15.0,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    # ── Core API Methods ──────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _get(self, endpoint: str, params: dict) -> dict:
        params["api_key"] = self.api_key
        response = await self.client.get(endpoint, params=params)
        response.raise_for_status()
        return response.json()

    async def find_email(
        self,
        first_name: str,
        last_name: str,
        domain: str,
    ) -> Optional[str]:
        """
        Find the most likely email for a person at a company domain.

        Returns the email string if found and confidence >= 70, else None.
        """
        if not self.api_key:
            logger.warning("HUNTER_API_KEY not set — skipping email lookup")
            return None

        try:
            data = await self._get("/email-finder", {
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
            })
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("Hunter rate limit reached")
            else:
                logger.error(f"Hunter find_email error: {e.response.status_code}")
            return None

        result = data.get("data", {})
        email = result.get("email")
        confidence = result.get("score", 0)

        if email and confidence >= 70:
            logger.debug(f"Hunter found {email} (confidence: {confidence})")
            return email

        logger.debug(f"Hunter confidence too low ({confidence}) for {first_name} {last_name} @ {domain}")
        return None

    async def verify_email(self, email: str) -> bool:
        """
        Verify whether an email address is deliverable.

        Returns True if Hunter considers it deliverable, False otherwise.
        """
        if not self.api_key:
            logger.warning("HUNTER_API_KEY not set — skipping verification")
            return False

        try:
            data = await self._get("/email-verifier", {"email": email})
        except httpx.HTTPStatusError as e:
            logger.error(f"Hunter verify_email error: {e.response.status_code}")
            return False

        result = data.get("data", {})
        status = result.get("status", "")

        # Hunter statuses: valid, invalid, accept_all, webmail, disposable, unknown
        deliverable = status in ("valid", "accept_all", "webmail")
        logger.debug(f"Hunter verified {email}: {status} → deliverable={deliverable}")
        return deliverable

    async def domain_search(
        self,
        domain: str,
        limit: int = 10,
        seniority: list[str] | None = None,
        department: list[str] | None = None,
    ) -> list[Lead]:
        """
        Find contacts at a company by their domain.

        Useful for targeting a specific company you already know you want to reach.

        Args:
            domain:     Company domain (e.g. "acmecorp.com")
            limit:      Max contacts to return
            seniority:  Filter by seniority: ["senior", "executive", "director", "manager"]
            department: Filter by dept: ["executive", "it", "finance", "management", "sales", "legal"]
        """
        if not self.api_key:
            raise ValueError("HUNTER_API_KEY is not set. Add it to your .env file.")

        params: dict = {"domain": domain, "limit": limit}
        if seniority:
            params["seniority"] = ",".join(seniority)
        if department:
            params["department"] = ",".join(department)

        try:
            data = await self._get("/domain-search", params)
        except httpx.HTTPStatusError as e:
            logger.error(f"Hunter domain_search error: {e.response.status_code} — {e.response.text}")
            return []

        result = data.get("data", {})
        org_name = result.get("organization", domain)
        emails = result.get("emails", [])

        leads = []
        for entry in emails[:limit]:
            lead = self._parse_email_entry(entry, domain, org_name)
            leads.append(lead)

        logger.info(f"Hunter domain search for {domain} returned {len(leads)} contacts")
        return leads

    # ── Enrichment Helper ─────────────────────────────────────────────────────

    async def enrich_lead_email(self, lead: "Lead") -> "Lead":
        """
        Attempt to find and verify an email for a lead that doesn't have one.
        Mutates the lead in place if an email is found.

        Returns the (possibly updated) lead.
        """
        if lead.contact.email and lead.contact.email_verified:
            return lead  # already has a verified email

        domain = lead.company.domain
        first = lead.contact.first_name
        last = lead.contact.last_name

        if not domain or not first or not last:
            logger.debug(f"Skipping Hunter lookup for {lead.display_name} — missing domain or name")
            return lead

        # Find email
        if not lead.contact.email:
            email = await self.find_email(first, last, domain)
            if email:
                lead.contact.email = email
                lead.touch()

        # Verify email
        if lead.contact.email and not lead.contact.email_verified:
            verified = await self.verify_email(lead.contact.email)
            lead.contact.email_verified = verified
            lead.touch()

        return lead

    async def enrich_leads_batch(self, leads: list["Lead"]) -> list["Lead"]:
        """Enrich emails for a batch of leads. Respects rate limits with small delays."""
        import asyncio
        enriched = []
        for i, lead in enumerate(leads):
            enriched_lead = await self.enrich_lead_email(lead)
            enriched.append(enriched_lead)
            # Small delay to be kind to Hunter's rate limits
            if i > 0 and i % 10 == 0:
                await asyncio.sleep(1.0)

        verified_count = sum(1 for l in enriched if l.contact.email_verified)
        logger.info(f"Hunter enrichment: {verified_count}/{len(enriched)} leads have verified emails")
        return enriched

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _parse_email_entry(self, entry: dict, domain: str, org_name: str) -> Lead:
        """Map a Hunter domain-search email entry to a Lead model."""
        contact = ContactInfo(
            first_name=entry.get("first_name"),
            last_name=entry.get("last_name"),
            full_name=f"{entry.get('first_name', '')} {entry.get('last_name', '')}".strip() or None,
            title=entry.get("position"),
            email=entry.get("value"),
            email_verified=(entry.get("verification", {}).get("status") == "valid"),
            linkedin_url=entry.get("linkedin"),
        )

        company = CompanyInfo(
            name=org_name,
            domain=domain,
            website=f"https://{domain}",
        )

        return Lead(
            source=LeadSource.HUNTER,
            contact=contact,
            company=company,
            raw_data=entry,
        )
