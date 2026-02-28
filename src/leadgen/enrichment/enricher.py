"""
LeadGen Company/Contact Enricher
Enriches leads with firmographic data from Clearbit or similar providers.

Stub: API integration not yet implemented.
Docs: https://clearbit.com/docs (or alternative enrichment APIs)
"""

from __future__ import annotations

import logging

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import Lead

logger = logging.getLogger(__name__)


class Enricher:
    """
    Enriches leads with company and contact data.

    Intended to use Clearbit Company API, Clearbit Enrichment, or similar
    to add: industry, employee_count, annual_revenue, technologies, etc.
    """

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.api_key = keys.clearbit

    async def enrich(self, lead: Lead) -> Lead:
        """
        Enrich a single lead with company/contact data.

        Returns the lead (possibly updated). Does not mutate if no API key
        or enrichment fails.
        """
        if not self.api_key:
            logger.debug("CLEARBIT_API_KEY not set — skipping enrichment")
            return lead

        # TODO: Implement Clearbit Company API lookup by domain
        # TODO: Update lead.company with enriched data
        # TODO: Optionally update lead.contact
        logger.warning("Enricher not yet implemented — returning lead unchanged")
        return lead

    async def enrich_batch(self, leads: list[Lead]) -> list[Lead]:
        """
        Enrich a batch of leads. Respects rate limits with delays.
        """
        enriched = []
        for lead in leads:
            result = await self.enrich(lead)
            enriched.append(result)
        return enriched
