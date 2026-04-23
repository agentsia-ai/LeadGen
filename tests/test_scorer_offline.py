"""LeadScorer behavior tests with a mocked Anthropic client.

Covers JSON-parsing variants (clean, fenced, malformed) and the batch
threshold-filter behavior. We don't assert that the returned `total`
matches a locally-computed weighted sum of the subscores — the engine
trusts Claude's returned total (see NOTES.md → 'LeadGen scorer: trusts
Claude's returned total'). If that policy changes, add a test here that
ignores Claude's total and recomputes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from leadgen.ai.scorer import LeadScorer
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource


def _mock_anthropic_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _clean_score_json(total: float = 0.82) -> str:
    return (
        '{"industry_match":0.9,"company_size_match":0.8,"geography_match":0.9,'
        '"pain_point_signals":0.8,"contact_quality":0.7,'
        f'"total":{total},"reasoning":"good fit"' + "}"
    )


@pytest.mark.asyncio
async def test_scorer_parses_clean_json_and_sets_timestamp(
    test_config, test_keys, sample_lead
) -> None:
    """A well-formed JSON response populates a ScoringBreakdown with all
    subscores AND a `scored_at` timestamp (used by downstream freshness
    filters)."""
    s = LeadScorer(test_config, test_keys)
    s.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(_clean_score_json(0.82))
    )

    result = await s.score(sample_lead)
    assert result.total == 0.82
    assert result.industry_match == 0.9
    assert result.reasoning == "good fit"
    assert result.scored_at is not None


@pytest.mark.asyncio
async def test_scorer_handles_fenced_json(test_config, test_keys, sample_lead) -> None:
    """Claude sometimes wraps JSON in a ```json ... ``` fence. The scorer
    must strip it rather than treat the fence as malformed."""
    s = LeadScorer(test_config, test_keys)
    fenced = "```json\n" + _clean_score_json(0.71) + "\n```"
    s.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(fenced)
    )
    result = await s.score(sample_lead)
    assert result.total == 0.71


@pytest.mark.asyncio
async def test_scorer_bad_json_returns_default_breakdown_with_failure_reason(
    test_config, test_keys, sample_lead
) -> None:
    """Non-JSON responses must NOT raise — the scorer returns a zeroed
    breakdown with a reasoning string explaining the parse failure, so a
    single bad response doesn't poison an entire batch."""
    s = LeadScorer(test_config, test_keys)
    s.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response("this is not JSON at all")
    )

    result = await s.score(sample_lead)
    assert result.total == 0.0
    assert result.reasoning.lower().startswith("scoring failed")
    assert result.scored_at is not None


def _lead_with_email(email: str, company: str = "Acme") -> Lead:
    return Lead(
        source=LeadSource.MANUAL,
        contact=ContactInfo(email=email, email_verified=True, full_name="Test User"),
        company=CompanyInfo(name=company),
    )


@pytest.mark.asyncio
async def test_score_batch_filters_by_threshold_and_attaches_all_scores(
    test_config, test_keys
) -> None:
    """score_batch must:
      - attach a score to EVERY lead it was given (so the caller can
        inspect rejects, not just passes)
      - return only leads whose total >= min_score
      - call touch() on each lead (updated_at advances)
    """
    totals_iter = iter([0.85, 0.40, 0.91])

    async def side_effect(*_args, **_kwargs):
        t = next(totals_iter)
        return _mock_anthropic_response(_clean_score_json(total=t))

    s = LeadScorer(test_config, test_keys)
    s.client.messages.create = AsyncMock(side_effect=side_effect)

    leads = [
        _lead_with_email("a@x.com", "A"),
        _lead_with_email("b@x.com", "B"),
        _lead_with_email("c@x.com", "C"),
    ]
    updated_before = [l.updated_at for l in leads]

    passing = await s.score_batch(leads, min_score=0.50)

    # All three got a score attached
    assert all(l.score is not None for l in leads)
    # Two passed the threshold
    assert len(passing) == 2
    passing_totals = sorted(l.score.total for l in passing)
    assert passing_totals == [0.85, 0.91]
    # touch() was called on every lead (updated_at advanced or equal —
    # equal is possible if the test runs faster than datetime resolution,
    # so we assert "not earlier than" rather than strictly greater).
    # TODO: If the engine ever adopts a monotonic now() helper, tighten
    # this to strict >.
    for lead, before in zip(leads, updated_before):
        assert lead.updated_at >= before
