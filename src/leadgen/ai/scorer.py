"""
LeadGen AI Scorer
Uses Claude to score leads against an ICP and explain the reasoning.

This is the generic engine implementation. To customize for a productized
agent (e.g. a named persona with tuned scoring behavior), either:
  1. Subclass `LeadScorer` and override `SYSTEM_PROMPT` (and optionally
     `_build_score_prompt`), or
  2. Point `config.ai.scorer_prompt_path` at an external prompt file.

See CLAUDE.md → Customization Patterns for details.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic

from leadgen._time import now_utc
from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import Lead, ScoringBreakdown

logger = logging.getLogger(__name__)


DEFAULT_SCORE_SYSTEM_PROMPT = """You are a lead qualification expert. Your job is to score a business lead 
against an Ideal Customer Profile (ICP) and return a structured JSON score.

You must respond with ONLY valid JSON — no preamble, no explanation outside the JSON.

The JSON must follow this exact structure:
{
  "industry_match": 0.0,
  "company_size_match": 0.0,
  "geography_match": 0.0,
  "pain_point_signals": 0.0,
  "contact_quality": 0.0,
  "total": 0.0,
  "reasoning": "Brief explanation of the score"
}

Each numeric field is a float between 0.0 and 1.0.
"total" should be the weighted average based on the weights provided.
"reasoning" should be 1-3 sentences explaining why this lead is or isn't a good fit.
"""

SCORE_SYSTEM_PROMPT = DEFAULT_SCORE_SYSTEM_PROMPT


class LeadScorer:
    """Scores leads against the configured ICP using Claude.

    Subclass this and override `SYSTEM_PROMPT` to define a tuned scorer with
    a custom persona or rubric. Per-deployment overrides can also be supplied
    by setting `config.ai.scorer_prompt_path` to a text file.
    """

    SYSTEM_PROMPT: str = DEFAULT_SCORE_SYSTEM_PROMPT

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.weights = config.scoring
        self.threshold = config.scoring.threshold
        self.model = config.ai.model
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolve the active system prompt.

        Resolution order:
          1. `config.ai.scorer_prompt_path` (if set and file exists)
          2. The class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.scorer_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"LeadScorer using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"scorer_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    def _build_score_prompt(self, lead: Lead) -> str:
        icp = self.config.icp
        weights = self.weights

        return f"""Score this lead against the ICP below.

=== IDEAL CUSTOMER PROFILE ===
Target Industries: {', '.join(icp.industries)}
Company Size: {icp.company_size.get('min_employees')}–{icp.company_size.get('max_employees')} employees
Target Geography: {icp.geography}
Pain Points We Solve: {', '.join(icp.pain_points)}
Positive Signals: {', '.join(icp.positive_signals)}
Negative Signals (disqualifiers): {', '.join(icp.negative_signals)}

Value Proposition: {self.config.value_prop.one_liner}

=== SCORING WEIGHTS ===
industry_match: {weights.industry_match}
company_size_match: {weights.company_size_match}
geography_match: {weights.geography_match}
pain_point_signals: {weights.pain_point_signals}
contact_quality: {weights.contact_quality}

=== LEAD TO SCORE ===
Contact: {lead.display_name}
Title: {lead.contact.title or 'Unknown'}
Email: {'✓ verified' if lead.contact.email_verified else ('present but unverified' if lead.contact.email else 'missing')}
LinkedIn: {'present' if lead.contact.linkedin_url else 'missing'}

Company: {lead.company.name}
Industry: {lead.company.industry or 'Unknown'}
Employees: {lead.company.employee_count or 'Unknown'}
Revenue: {lead.company.annual_revenue or 'Unknown'}
Location: {lead.company.city}, {lead.company.state}, {lead.company.country}
Description: {lead.company.description or 'No description available'}
Technologies: {', '.join(lead.company.technologies[:10]) if lead.company.technologies else 'None listed'}

Score this lead now. Return only JSON."""

    async def score(self, lead: Lead) -> ScoringBreakdown:
        """Score a single lead against the ICP."""
        prompt = self._build_score_prompt(lead)

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system=self._system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )

            raw_text = response.content[0].text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            score_data = json.loads(raw_text)
            breakdown = ScoringBreakdown(
                **score_data,
                scored_at=now_utc(),
            )

            logger.info(
                f"Scored '{lead.display_name}' at {lead.company.name}: "
                f"{breakdown.total:.2f} ({'PASS' if breakdown.total >= self.threshold else 'FAIL'})"
            )
            return breakdown

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse Claude scoring response: {e}")
            return ScoringBreakdown(
                reasoning=f"Scoring failed: {e}",
                scored_at=now_utc(),
            )

    async def score_batch(
        self, leads: list[Lead], min_score: float | None = None
    ) -> list[Lead]:
        """
        Score a batch of leads and attach scores.
        Optionally filter to only leads meeting min_score.
        """
        import asyncio

        threshold = min_score if min_score is not None else self.threshold
        scored = []

        for i in range(0, len(leads), 5):
            batch = leads[i : i + 5]
            results = await asyncio.gather(*[self.score(lead) for lead in batch])

            for lead, score in zip(batch, results):
                lead.score = score
                lead.touch()
                if score.total >= threshold:
                    scored.append(lead)

        logger.info(
            f"Scored {len(leads)} leads — {len(scored)} passed threshold {threshold}"
        )
        return scored
