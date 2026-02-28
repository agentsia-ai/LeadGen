"""
LeadGen CSV Import Lead Source
Imports leads from CSV files (manual upload or watch folder).

Stub: Folder watching and CSV parsing not yet implemented.
Expected CSV columns: first_name, last_name, email, title, company, company_domain, etc.
"""

from __future__ import annotations

import logging
from pathlib import Path

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import Lead

logger = logging.getLogger(__name__)


class CSVImportConnector:
    """
    Imports leads from CSV files.

    Intended to:
    - Watch a folder for new CSVs (sources.csv_import.watch_folder)
    - Parse CSV with flexible column mapping
    - Map rows to Lead model
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

    async def import_from_folder(self, limit: int = 500) -> list[Lead]:
        """
        Scan watch folder for CSV files and import leads.

        Returns up to `limit` leads. Stub returns empty list.
        """
        if not self.watch_folder.exists():
            logger.warning(f"CSV watch folder does not exist: {self.watch_folder}")
            return []

        # TODO: List CSV files in watch_folder
        # TODO: Parse with pandas or csv module, map columns to Lead
        # TODO: Optionally move/archive processed files
        logger.warning("CSV import not yet implemented — returning empty list")
        return []

    async def import_file(self, path: str | Path) -> list[Lead]:
        """
        Import leads from a single CSV file.

        Stub returns empty list.
        """
        path = Path(path)
        if not path.exists():
            logger.error(f"CSV file not found: {path}")
            return []

        # TODO: Parse CSV, map to Lead model
        logger.warning("CSV import not yet implemented — returning empty list")
        return []
