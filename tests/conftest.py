"""Shared pytest fixtures for LeadGen tests.

All fixtures here must work offline — no real Anthropic, Hunter, Apollo,
PDL, or SMTP calls. Tests that touch HTTP code use `respx` to intercept;
tests that touch Claude use `unittest.mock.AsyncMock` on
`client.messages.create`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from leadgen.config.loader import (
    AIConfig,
    APIKeys,
    DatabaseConfig,
    ICPConfig,
    LeadGenConfig,
    OutreachConfig,
    ScoringConfig,
    SourcesConfig,
    ValuePropConfig,
)
from leadgen.crm.database import LeadDatabase
from leadgen.models import (
    CompanyInfo,
    ContactInfo,
    Lead,
    LeadSource,
    LeadStatus,
    ScoringBreakdown,
)


@pytest.fixture
def tmp_db_path() -> str:
    """Temporary SQLite path. Cleanup handled by tempdir teardown."""
    tmp = tempfile.mkdtemp()
    return str(Path(tmp) / "test_leadgen.db")


@pytest.fixture
def test_config() -> LeadGenConfig:
    """A realistic, offline-safe LeadGenConfig.

    ICP / value_prop fields are populated so scorer + drafter prompt
    builders have something non-empty to render. Sources are all marked
    disabled so if something accidentally tried to run them, it'd at
    least fail loudly.
    """
    return LeadGenConfig(
        client_name="TestCo",
        operator_name="Tester",
        operator_title="Founder",
        operator_email="tester@example.com",
        icp=ICPConfig(
            industries=["SaaS", "Fintech"],
            company_size={"min_employees": 10, "max_employees": 500},
            geography={"countries": ["US"], "states": ["California"], "cities": []},
            pain_points=["manual lead research", "slow follow-up"],
            positive_signals=["hiring SDRs"],
            negative_signals=["outsourced sales"],
        ),
        value_prop=ValuePropConfig(
            headline="Replace manual prospecting",
            one_liner="We automate the top of your sales funnel so your reps sell, not search.",
            proof_points=["3x pipeline at Acme", "50% less time on list-building"],
        ),
        outreach=OutreachConfig(
            tone="friendly-professional",
            daily_email_limit=5,
            follow_up_days=[3, 7],
            max_follow_ups=2,
            require_approval=True,
            signature="— {operator_name}, {operator_title}",
        ),
        sources=SourcesConfig(),
        scoring=ScoringConfig(),
        database=DatabaseConfig(backend="sqlite", sqlite_path="./data/test.db"),
        ai=AIConfig(model="claude-sonnet-4-20250514"),
    )


@pytest.fixture
def test_keys() -> APIKeys:
    """Offline-safe keys. Strings look plausible but are never used by a
    real client because every test either mocks Anthropic's
    `messages.create` or mocks httpx via respx."""
    return APIKeys(
        ANTHROPIC_API_KEY="sk-ant-test-offline",
        APOLLO_API_KEY="apollo-test",
        HUNTER_API_KEY="hunter-test",
        PDL_API_KEY="pdl-test",
        SMTP_USERNAME="",
        SMTP_PASSWORD="",
        SMTP_FROM_EMAIL="",
    )


@pytest_asyncio.fixture
async def initialized_db(tmp_db_path: str) -> LeadDatabase:
    """Async-initialized LeadDatabase on a temp path."""
    db = LeadDatabase(tmp_db_path)
    await db.init()
    return db


@pytest.fixture
def sample_lead() -> Lead:
    """Canonical test Lead with a verified email and a complete company."""
    return Lead(
        source=LeadSource.HUNTER,
        status=LeadStatus.NEW,
        contact=ContactInfo(
            first_name="Jane",
            last_name="Doe",
            full_name="Jane Doe",
            title="VP Engineering",
            email="jane@acmecorp.com",
            email_verified=True,
            linkedin_url="https://linkedin.com/in/janedoe",
        ),
        company=CompanyInfo(
            name="Acme Corp",
            domain="acmecorp.com",
            website="https://acmecorp.com",
            industry="SaaS",
            employee_count=120,
            annual_revenue=25_000_000,
            city="San Francisco",
            state="California",
            country="US",
            technologies=["python", "postgres", "kubernetes"],
        ),
    )


@pytest.fixture
def scored_lead(sample_lead: Lead) -> Lead:
    """A sample_lead with a passing ScoringBreakdown attached."""
    sample_lead.score = ScoringBreakdown(
        industry_match=0.95,
        company_size_match=0.80,
        geography_match=0.90,
        pain_point_signals=0.75,
        contact_quality=0.70,
        total=0.82,
        reasoning="Strong industry and geography match; senior contact with verified email.",
    )
    sample_lead.status = LeadStatus.SCORED
    return sample_lead
