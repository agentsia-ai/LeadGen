"""Contact-page email scraper tests — fixture HTML, respx-mocked HTTP."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from leadgen.crm.suppression import add_operator_suppression
from leadgen.mcp_server import server as mcp_server
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource, LeadStatus
from leadgen.sources.email_scraper import (
    ContactEmailScraper,
    discover_candidate_urls,
    extract_emails_from_html,
    normalize_domain,
    rank_candidates,
    scrape_lead_email,
    scrape_lead_email_by_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_site(
    respx_mock,
    *,
    domain: str,
    homepage_html: str,
    page_html: dict[str, str] | None = None,
) -> str:
    """Register respx routes for a fake firm site (exact URL matching)."""
    base = f"https://{domain}"
    page_html = page_html or {}
    respx_mock.get(url__regex=rf"^{base}/robots\.txt$").mock(
        return_value=httpx.Response(404, text="")
    )
    respx_mock.get(url__regex=rf"^{base}/?$").mock(
        return_value=httpx.Response(200, text=homepage_html)
    )
    for path, html in page_html.items():
        respx_mock.get(url__regex=rf"^{base}{path}$").mock(
            return_value=httpx.Response(200, text=html)
        )
    for path in (
        "/contact",
        "/contact-us",
        "/about",
        "/about-us",
        "/team",
        "/staff",
        "/appointments",
        "/attorneys",
        "/agents",
    ):
        if path not in page_html:
            respx_mock.get(url__regex=rf"^{base}{path}$").mock(
                return_value=httpx.Response(404, text="not found")
            )
    return base


def _lead(
    *,
    lead_id: str = "lead-scrape-1",
    domain: str = "wcalvertlaw.com",
    first_name: str = "Wendy",
    last_name: str = "Calvert",
) -> Lead:
    return Lead(
        id=lead_id,
        source=LeadSource.PDL,
        status=LeadStatus.NEW,
        contact=ContactInfo(
            first_name=first_name,
            last_name=last_name,
            full_name=f"{first_name} {last_name}",
        ),
        company=CompanyInfo(
            name="Calvert Law",
            domain=domain,
            website=f"https://{domain}",
        ),
    )


def test_normalize_domain_strips_protocol_and_path() -> None:
    assert normalize_domain("https://www.example.com/about") == "example.com"


def test_extract_emails_filters_prose_artifact_tlds() -> None:
    html = _read_fixture("prose_artifacts.html")
    emails = extract_emails_from_html(html)
    assert "de@h.preserve" not in emails
    assert "expect@ions.wendy" not in emails
    assert "knowledgeable@torney.the" not in emails
    assert emails == set()


@pytest.mark.asyncio
@respx.mock
async def test_scrape_prose_only_site_returns_no_email() -> None:
    domain = "prosefirm.com"
    prose = _read_fixture("prose_artifacts.html")
    _mock_site(respx, domain=domain, homepage_html=prose, page_html={})

    async with ContactEmailScraper() as scraper:
        result = await scraper.scrape_domain(domain)

    assert result.status == "no_email"
    assert result.candidates == []
    assert result.to_dict()["best_email"] is None


@pytest.mark.asyncio
@respx.mock
async def test_apply_skips_low_confidence_candidate(initialized_db) -> None:
    domain = "lowconf.com"
    lead = _lead(lead_id="lead-lowconf", domain=domain, first_name="Pat", last_name="Lee")
    await initialized_db.upsert(lead)

    _mock_site(
        respx,
        domain=domain,
        homepage_html=_read_fixture("homepage_gmail_only.html"),
        page_html={},
    )

    review = await scrape_lead_email_by_id(initialized_db, lead.id, apply=False)
    assert review["status"] == "found"
    assert review["best_email"] == "partner@gmail.com"

    applied = await scrape_lead_email_by_id(initialized_db, lead.id, apply=True)
    assert applied["applied"] is False
    assert applied["apply_skipped"] is True
    assert "no confident candidate" in applied["reason"]

    stored = await initialized_db.get(lead.id)
    assert stored.contact.email is None


def test_extract_emails_from_mailto_link() -> None:
    html = _read_fixture("contact_mailto.html")
    emails = extract_emails_from_html(html)
    assert "jane@acmecorp.com" in emails


def test_extract_emails_from_obfuscated_text() -> None:
    html = _read_fixture("obfuscated_email.html")
    emails = extract_emails_from_html(html)
    assert "wendy@wcalvertlaw.com" in emails


def test_extract_emails_filters_placeholder_addresses() -> None:
    html = _read_fixture("placeholder_only.html")
    emails = extract_emails_from_html(html)
    assert "info@godaddy.com" not in emails
    assert "noreply@wix.com" not in emails
    assert "example@example.com" not in emails
    assert emails == set()


def test_discover_candidate_urls_includes_appointments_from_nav() -> None:
    homepage = _read_fixture("homepage_with_nav.html")
    urls = discover_candidate_urls(
        homepage,
        "https://wcalvertlaw.com",
        "wcalvertlaw.com",
    )
    assert "https://wcalvertlaw.com" in urls
    assert "https://wcalvertlaw.com/appointments" in urls


def test_rank_candidates_prefers_domain_match_and_named_inbox() -> None:
    raw = {
        "info@wcalvertlaw.com": "https://wcalvertlaw.com/contact",
        "wendy@wcalvertlaw.com": "https://wcalvertlaw.com/appointments",
        "other@gmail.com": "https://wcalvertlaw.com/about",
    }
    ranked = rank_candidates(
        raw,
        "wcalvertlaw.com",
        first_name="Wendy",
        last_name="Calvert",
    )
    assert ranked[0].email == "wendy@wcalvertlaw.com"
    assert ranked[0].domain_match is True


@pytest.mark.asyncio
@respx.mock
async def test_scrape_recovers_email_from_appointments_subpage() -> None:
    _mock_site(
        respx,
        domain="wcalvertlaw.com",
        homepage_html=_read_fixture("homepage_with_nav.html"),
        page_html={
            "/appointments": _read_fixture("appointments_page.html"),
        },
    )

    lead = _lead()
    result = await scrape_lead_email(lead)

    assert result.status == "found"
    assert result.candidates
    assert result.candidates[0].email == "wendy@wcalvertlaw.com"
    assert "/appointments" in result.candidates[0].page_url
    assert result.candidates[0].domain_match is True


@pytest.mark.asyncio
@respx.mock
async def test_scrape_placeholder_only_site_returns_no_email() -> None:
    domain = "templatesite.com"
    placeholder = _read_fixture("placeholder_only.html")
    _mock_site(respx, domain=domain, homepage_html=placeholder, page_html={})

    async with ContactEmailScraper() as scraper:
        result = await scraper.scrape_domain(domain)

    assert result.status == "no_email"
    assert result.candidates == []


@pytest.mark.asyncio
async def test_scrape_lead_email_by_id_skips_suppressed(initialized_db) -> None:
    lead = _lead()
    await initialized_db.upsert(lead)
    await add_operator_suppression(initialized_db, lead_id=lead.id, reason="icp_mismatch")

    result = await scrape_lead_email_by_id(initialized_db, lead.id)

    assert result["status"] == "suppressed"
    assert "icp_mismatch" in result["reason"]


@pytest.fixture
def mcp_env(initialized_db, test_config, test_keys, monkeypatch):
    monkeypatch.setattr(mcp_server, "config", test_config)
    monkeypatch.setattr(mcp_server, "keys", test_keys)
    monkeypatch.setattr(mcp_server, "db", initialized_db)
    return initialized_db


@pytest.mark.asyncio
@respx.mock
async def test_mcp_scrape_lead_email_returns_candidates(mcp_env) -> None:
    lead = _lead()
    await mcp_env.upsert(lead)

    _mock_site(
        respx,
        domain="wcalvertlaw.com",
        homepage_html=_read_fixture("homepage_with_nav.html"),
        page_html={
            "/appointments": _read_fixture("appointments_page.html"),
        },
    )

    result = await mcp_server.call_tool(
        "scrape_lead_email",
        {"lead_id": lead.id},
    )
    payload = json.loads(result[0].text)

    assert payload["status"] == "found"
    assert payload["best_email"] == "wendy@wcalvertlaw.com"
    assert payload["candidates"][0]["page_url"].endswith("/appointments")
    assert payload["candidates"][0]["domain_match"] is True
