"""
LeadGen CSV Import Lead Source
Imports leads from CSV files (manual upload or watch folder).

Supports flexible column mapping for common header variations.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import CompanyInfo, ContactInfo, Lead, LeadSource

logger = logging.getLogger(__name__)

# Flexible column name mapping (case-insensitive, strip whitespace)
COLUMN_ALIASES = {
    "first_name": ["first_name", "firstname", "first name", "given_name"],
    "last_name": ["last_name", "lastname", "last name", "surname", "family_name"],
    "full_name": ["full_name", "fullname", "full name", "name"],
    "email": ["email", "email_address", "e-mail", "mail"],
    "title": ["title", "job_title", "position", "role"],
    "company": ["company", "company_name", "organization", "org"],
    "company_domain": ["company_domain", "domain", "website_domain"],
    "company_website": ["company_website", "website", "url"],
    "phone": ["phone", "phone_number", "telephone"],
    "linkedin_url": ["linkedin_url", "linkedin", "linkedin_url"],
    "city": ["city"],
    "state": ["state", "region"],
    "country": ["country"],
}


def _normalize_header(h: str) -> str:
    return h.strip().lower().replace(" ", "_").replace("-", "_")


def _find_column(headers: list[str], aliases: list[str]) -> str | None:
    """Find first header matching any alias."""
    normalized = {_normalize_header(h): h for h in headers}
    for alias in aliases:
        key = _normalize_header(alias)
        if key in normalized:
            return normalized[key]
    return None


def _parse_row(row: dict, headers: list[str]) -> Lead | None:
    """Map a CSV row to a Lead. Returns None if row has no usable data."""
    col_map = {}
    for field, aliases in COLUMN_ALIASES.items():
        col_map[field] = _find_column(headers, aliases)

    def get(field: str) -> str | None:
        col = col_map.get(field)
        if col and col in row:
            val = row.get(col, "").strip()
            return val if val else None
        return None

    email = get("email")
    company = get("company")
    if not company and not email:
        return None

    first = get("first_name")
    last = get("last_name")
    full = get("full_name")
    if not full and (first or last):
        full = f"{first or ''} {last or ''}".strip() or None

    contact = ContactInfo(
        first_name=first,
        last_name=last,
        full_name=full,
        title=get("title"),
        email=email,
        linkedin_url=get("linkedin_url"),
        phone=get("phone"),
    )

    domain = get("company_domain")
    website = get("company_website")
    if not domain and website:
        domain = website.replace("https://", "").replace("http://", "").split("/")[0]

    company_info = CompanyInfo(
        name=company or "Unknown",
        domain=domain,
        website=website,
        city=get("city"),
        state=get("state"),
        country=get("country") or "US",
    )

    return Lead(
        source=LeadSource.CSV_IMPORT,
        contact=contact,
        company=company_info,
        raw_data=dict(row),
    )


class CSVImportConnector:
    """
    Imports leads from CSV files.

    Supports flexible column names. See COLUMN_ALIASES for mappings.
    """

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.watch_folder = Path(
            config.sources.csv_import.get("watch_folder", "./imports")
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def import_file(self, path: str | Path) -> list[Lead]:
        """Import leads from a single CSV file."""
        path = Path(path)
        if not path.exists():
            logger.error(f"CSV file not found: {path}")
            return []

        leads: list[Lead] = []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                for row in reader:
                    lead = _parse_row(row, headers)
                    if lead:
                        leads.append(lead)
            logger.info(f"Parsed {len(leads)} leads from {path}")
        except Exception as e:
            logger.error(f"Failed to parse CSV {path}: {e}")
        return leads

    async def import_from_folder(self, limit: int = 500) -> list[Lead]:
        """Scan watch folder for CSV files and import leads."""
        if not self.watch_folder.exists():
            logger.warning(f"CSV watch folder does not exist: {self.watch_folder}")
            return []

        all_leads: list[Lead] = []
        for csv_file in sorted(self.watch_folder.glob("*.csv")):
            file_leads = await self.import_file(csv_file)
            all_leads.extend(file_leads)
            if len(all_leads) >= limit:
                break
        return all_leads[:limit]


SAMPLE_CSV_CONTENT = """first_name,last_name,email,title,company,company_domain
Jane,Smith,jane@example.com,VP Operations,Example Corp,example.com
Bob,Jones,bob@acmecorp.com,Director of Sales,Acme Corp,acmecorp.com
"""
