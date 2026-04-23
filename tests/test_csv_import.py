"""CSV import tests — pure parsing, no network.

The CSV importer is the safest source to exercise fully because it
doesn't touch any external service; it's also the most likely to
regress silently when headers drift or a new column alias is added.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leadgen.models import LeadSource
from leadgen.sources.csv_import import CSVImportConnector


def _write_csv(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_import_file_parses_basic_columns(
    test_config, test_keys, tmp_path: Path
) -> None:
    """A CSV with standard headers produces Lead objects with the expected
    ContactInfo / CompanyInfo fields and source=CSV_IMPORT."""
    csv_path = tmp_path / "leads.csv"
    _write_csv(
        csv_path,
        "first_name,last_name,email,title,company,company_domain\n"
        "Jane,Smith,jane@acme.com,VP Ops,Acme Corp,acme.com\n"
        "Bob,Jones,bob@beta.com,Director,Beta Inc,beta.com\n",
    )
    conn = CSVImportConnector(test_config, test_keys)
    leads = await conn.import_file(csv_path)

    assert len(leads) == 2
    assert leads[0].source == LeadSource.CSV_IMPORT
    assert leads[0].contact.email == "jane@acme.com"
    assert leads[0].contact.full_name == "Jane Smith"
    assert leads[0].company.name == "Acme Corp"
    assert leads[0].company.domain == "acme.com"


@pytest.mark.asyncio
async def test_import_file_handles_header_aliases(
    test_config, test_keys, tmp_path: Path
) -> None:
    """Common spelling variants must all map correctly — this is the
    biggest UX hazard for CSV imports from messy real-world exports."""
    csv_path = tmp_path / "aliased.csv"
    _write_csv(
        csv_path,
        "First Name,Last Name,E-Mail,Job Title,Organization,Website\n"
        "Alex,Li,alex@corp.com,Head of Sales,CorpCo,https://corp.com/about\n",
    )
    conn = CSVImportConnector(test_config, test_keys)
    leads = await conn.import_file(csv_path)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.contact.first_name == "Alex"
    assert lead.contact.last_name == "Li"
    assert lead.contact.email == "alex@corp.com"
    assert lead.contact.title == "Head of Sales"
    assert lead.company.name == "CorpCo"
    # Website → domain derivation strips scheme and path
    assert lead.company.domain == "corp.com"


@pytest.mark.asyncio
async def test_import_file_skips_rows_with_no_email_and_no_company(
    test_config, test_keys, tmp_path: Path
) -> None:
    """A row with neither email nor company name has no usable identifier
    and must be dropped — otherwise we'd pollute the DB with blank rows
    that can't be deduped."""
    csv_path = tmp_path / "skips.csv"
    _write_csv(
        csv_path,
        "first_name,last_name,email,company\n"
        "Good,User,good@x.com,CompanyA\n"
        ",,,\n"  # fully empty
        "OnlyName,,,\n"  # name but no email + no company → skip
        "Has,Company,,CompanyB\n"  # no email but has company → keep
        ",,has@email.com,\n",  # no company but has email → keep
    )
    conn = CSVImportConnector(test_config, test_keys)
    leads = await conn.import_file(csv_path)

    emails_or_companies = [
        (l.contact.email, l.company.name) for l in leads
    ]
    assert len(leads) == 3, f"Expected 3 parsed rows, got {emails_or_companies}"
    assert ("good@x.com", "CompanyA") in emails_or_companies
    assert any(c == "CompanyB" for _, c in emails_or_companies)
    assert any(e == "has@email.com" for e, _ in emails_or_companies)


@pytest.mark.asyncio
async def test_import_file_missing_path_returns_empty(
    test_config, test_keys, tmp_path: Path
) -> None:
    """A missing file logs but returns []; callers rely on this being
    non-raising so a batch folder import isn't halted by one bad path."""
    conn = CSVImportConnector(test_config, test_keys)
    leads = await conn.import_file(tmp_path / "does_not_exist.csv")
    assert leads == []


@pytest.mark.asyncio
async def test_import_from_folder_scans_multiple_csvs_respecting_limit(
    test_config, test_keys, tmp_path: Path
) -> None:
    """import_from_folder walks *.csv in the configured watch folder and
    stops at `limit`."""
    folder = tmp_path / "imports"
    folder.mkdir()
    _write_csv(
        folder / "a.csv",
        "email,company\n1@x.com,A\n2@x.com,B\n",
    )
    _write_csv(
        folder / "b.csv",
        "email,company\n3@x.com,C\n4@x.com,D\n",
    )

    test_config.sources.csv_import = {"enabled": True, "watch_folder": str(folder)}
    conn = CSVImportConnector(test_config, test_keys)

    all_leads = await conn.import_from_folder(limit=10)
    assert len(all_leads) == 4

    limited = await conn.import_from_folder(limit=2)
    assert len(limited) == 2
