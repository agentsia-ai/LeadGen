"""
LeadGen CRM — SQLite Database Layer
Stores leads, scores, and outreach history locally.
Swap backend to Supabase by changing DATABASE_URL in .env.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from leadgen.models import Lead, LeadStatus

logger = logging.getLogger(__name__)


class LeadDatabase:
    """Simple async SQLite-backed lead store."""

    def __init__(self, db_path: str = "./data/leadgen.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    contact_json TEXT NOT NULL,
                    company_json TEXT NOT NULL,
                    score_json TEXT,
                    outreach_json TEXT DEFAULT '[]',
                    notes TEXT DEFAULT '',
                    tags_json TEXT DEFAULT '[]',
                    raw_data_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    -- Denormalized for quick filtering
                    company_name TEXT,
                    contact_email TEXT,
                    score_total REAL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score_total DESC);
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email 
                ON leads(contact_email) WHERE contact_email IS NOT NULL;
            """)
            await db.commit()
        logger.info(f"Database initialized: {self.db_path}")

    async def upsert(self, lead: Lead) -> bool:
        """Insert or update a lead. Returns True if new, False if updated."""
        async with aiosqlite.connect(self.db_path) as db:
            # Check for existing: by id first (e.g. lead loaded from DB, enriched), then by email
            existing = None
            async with db.execute("SELECT id FROM leads WHERE id = ?", (lead.id,)) as cur:
                existing = await cur.fetchone()
            if not existing and lead.contact.email:
                async with db.execute(
                    "SELECT id FROM leads WHERE contact_email = ?", (lead.contact.email,)
                ) as cur:
                    existing = await cur.fetchone()

            row = (
                lead.id,
                lead.source.value,
                lead.status.value,
                lead.contact.model_dump_json(),
                lead.company.model_dump_json(),
                lead.score.model_dump_json() if lead.score else None,
                json.dumps([r.model_dump(mode="json") for r in lead.outreach_history]),
                lead.notes,
                json.dumps(lead.tags),
                json.dumps(lead.raw_data),
                lead.created_at.isoformat(),
                lead.updated_at.isoformat(),
                lead.company.name,
                lead.contact.email,
                lead.score.total if lead.score else None,
            )

            if existing:
                await db.execute("""
                    UPDATE leads SET
                        status=?, contact_json=?, company_json=?, score_json=?,
                        outreach_json=?, notes=?, tags_json=?, updated_at=?,
                        score_total=?, company_name=?, contact_email=?
                    WHERE id=?
                """, (
                    lead.status.value, lead.contact.model_dump_json(),
                    lead.company.model_dump_json(),
                    lead.score.model_dump_json() if lead.score else None,
                    json.dumps([r.model_dump(mode="json") for r in lead.outreach_history]),
                    lead.notes, json.dumps(lead.tags),
                    lead.updated_at.isoformat(),
                    lead.score.total if lead.score else None,
                    lead.company.name, lead.contact.email,
                    existing[0],
                ))
                await db.commit()
                return False
            else:
                await db.execute("""
                    INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, row)
                await db.commit()
                return True

    async def get(self, lead_id: str) -> Lead | None:
        """Fetch a single lead by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_lead(row)

    async def list(
        self,
        status: LeadStatus | None = None,
        min_score: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Lead]:
        """List leads with optional filters."""
        conditions = []
        params: list = []

        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if min_score is not None:
            conditions.append("score_total >= ?")
            params.append(min_score)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params += [limit, offset]

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT * FROM leads {where} ORDER BY score_total DESC LIMIT ? OFFSET ?",
                params,
            ) as cur:
                rows = await cur.fetchall()
                return [self._row_to_lead(r) for r in rows]

    async def find_duplicates(self) -> list[tuple[str, list[str]]]:
        """Find duplicate leads. Returns list of (dedupe_key, [lead_ids]) for groups with >1."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, contact_json, company_json FROM leads"
            ) as cur:
                rows = await cur.fetchall()
        key_to_ids: dict[str, list[str]] = {}
        for row in rows:
            lead_id = row[0]
            contact = json.loads(row[1])
            company = json.loads(row[2])
            email = contact.get("email") or None
            full_name = contact.get("full_name") or f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
            company_name = company.get("name") or ""
            domain = company.get("domain") or company.get("website") or ""
            if email:
                key = f"email:{email.lower().strip()}"
            elif full_name or company_name or domain:
                key = f"name:{full_name.lower()}|company:{company_name.lower()}|domain:{domain.lower()}"
            else:
                continue  # No identifying info, skip (can't safely dedupe)
            key_to_ids.setdefault(key, []).append(lead_id)
        return [(k, ids) for k, ids in key_to_ids.items() if len(ids) > 1]

    async def delete_duplicates(self, keep: str = "oldest") -> int:
        """Remove duplicate leads. keep='oldest' keeps first created; 'newest' keeps last updated."""
        dupes = await self.find_duplicates()
        if not dupes:
            return 0
        order_col = "created_at" if keep == "oldest" else "updated_at"
        deleted = 0
        async with aiosqlite.connect(self.db_path) as db:
            for _key, ids in dupes:
                # Get ids in order - keep first, delete rest
                async with db.execute(
                    f"SELECT id FROM leads WHERE id IN ({','.join('?'*len(ids))}) ORDER BY {order_col} ASC",
                    ids,
                ) as cur:
                    ordered = [r[0] for r in await cur.fetchall()]
                keep_id = ordered[0]
                for lead_id in ordered[1:]:
                    await db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
                    deleted += 1
            await db.commit()
        return deleted

    async def count_by_status(self) -> dict[str, int]:
        """Return lead counts grouped by status."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM leads GROUP BY status"
            ) as cur:
                rows = await cur.fetchall()
                return {row[0]: row[1] for row in rows}

    def _row_to_lead(self, row) -> Lead:
        """Convert a DB row tuple back to a Lead model."""
        from datetime import datetime
        from leadgen.models import ContactInfo, CompanyInfo, OutreachRecord, ScoringBreakdown, LeadSource

        (id_, source, status, contact_json, company_json, score_json,
         outreach_json, notes, tags_json, raw_json, created_at, updated_at,
         *_denorm) = row

        return Lead(
            id=id_,
            source=LeadSource(source),
            status=LeadStatus(status),
            contact=ContactInfo(**json.loads(contact_json)),
            company=CompanyInfo(**json.loads(company_json)),
            score=ScoringBreakdown(**json.loads(score_json)) if score_json else None,
            outreach_history=[OutreachRecord(**r) for r in json.loads(outreach_json)],
            notes=notes,
            tags=json.loads(tags_json),
            raw_data=json.loads(raw_json),
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
        )
