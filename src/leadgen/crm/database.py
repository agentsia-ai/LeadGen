"""
LeadGen CRM — SQLite Database Layer
Stores leads, scores, and outreach history locally.
Swap backend to Supabase by changing DATABASE_URL in .env.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import aiosqlite

from leadgen._time import now_utc, parse_iso
from leadgen.models import Lead, LeadStatus
from leadgen.text import normalize_company_display_name

logger = logging.getLogger(__name__)


class EmailCollisionError(Exception):
    """A write would violate the ``UNIQUE(contact_email)`` index.

    Raised by :meth:`LeadDatabase.upsert` when the email being written
    already belongs to a *different* lead row. Carries the colliding email
    and the id of the lead that currently holds it so batch callers (e.g.
    the MCP ``enrich_lead`` tool) can skip just that one lead, keep the rest
    of the batch going, and tell the operator which row owns the address.
    """

    def __init__(self, email: str | None, existing_lead_id: str | None):
        self.email = email
        self.existing_lead_id = existing_lead_id
        super().__init__(f"email {email!r} already on lead {existing_lead_id}")


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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS suppressions (
                    suppression_key TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    source_lead_id TEXT,
                    display_name TEXT,
                    company_name TEXT,
                    created_at TEXT NOT NULL,
                    notes TEXT DEFAULT ''
                )
            """)
            await db.commit()
        await self._backfill_suppressions()
        logger.info(f"Database initialized: {self.db_path}")

    @staticmethod
    def _name_company_key(contact: dict, company: dict) -> str | None:
        """Email-INDEPENDENT identity key: person name + company.

        Deliberately ignores email so the *same person at the same company*
        collapses to one row regardless of whether the incoming email is
        null. PDL's free tier returns null emails, so an email-bearing key
        let an already-known (enriched) person look brand-new when PDL
        re-returned them with ``email=None`` — stacking duplicate rows and
        wasting PDL/Hunter credits re-fetching someone we already had.

        Returns None unless BOTH a person name and a company name are
        present: matching on company alone would wrongly merge different
        people at the same firm, and matching on name alone would merge
        namesakes across companies.

        Shared by `upsert` (insert-time dedupe) and `find_duplicates` /
        `delete_duplicates` (cleanup) so prevention and cure use exactly the
        same notion of identity.
        """
        first = contact.get("first_name") or ""
        last = contact.get("last_name") or ""
        full_name = (contact.get("full_name") or f"{first} {last}").strip()
        company_name = (company.get("name") or "").strip()
        if not full_name or not company_name:
            return None
        return f"name:{full_name.lower()}|company:{company_name.lower()}"

    @staticmethod
    def domain_suppression_key(domain: str) -> str:
        """Suppression key for an entire company domain."""
        return f"domain:{domain.lower().strip()}"

    def identity_key_from_lead(self, lead: Lead) -> str | None:
        """Email-independent identity key for a Lead model instance."""
        return self._name_company_key(
            lead.contact.model_dump(), lead.company.model_dump()
        )

    async def add_suppression(
        self,
        suppression_key: str,
        reason: str,
        *,
        source_lead_id: str | None = None,
        display_name: str | None = None,
        company_name: str | None = None,
        notes: str = "",
    ) -> bool:
        """Insert a suppression record. Returns True if newly added."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO suppressions
                    (suppression_key, reason, source_lead_id, display_name,
                     company_name, created_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suppression_key,
                    reason,
                    source_lead_id,
                    display_name,
                    company_name,
                    now_utc().isoformat(),
                    notes,
                ),
            )
            await db.commit()
            return cur.rowcount > 0

    async def is_suppression_key(self, suppression_key: str) -> tuple[bool, str | None]:
        """Return (True, reason) if *suppression_key* is on the suppression set."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT reason FROM suppressions WHERE suppression_key = ?",
                (suppression_key,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return True, row[0]
                return False, None

    async def check_lead_suppressed(self, lead: Lead) -> tuple[bool, str | None]:
        """Check name+company identity and company domain against suppressions."""
        identity_key = self.identity_key_from_lead(lead)
        if identity_key:
            suppressed, reason = await self.is_suppression_key(identity_key)
            if suppressed:
                return True, reason
        if lead.company.domain:
            suppressed, reason = await self.is_suppression_key(
                self.domain_suppression_key(lead.company.domain)
            )
            if suppressed:
                return True, reason
        return False, None

    async def list_suppressions(self, limit: int = 100) -> list[dict]:
        """Return suppression records for operator review."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM suppressions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(row) for row in rows]

    async def remove_suppression(self, suppression_key: str) -> bool:
        """Delete a suppression record. Returns True if a row was removed."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM suppressions WHERE suppression_key = ?",
                (suppression_key,),
            )
            await db.commit()
            return cur.rowcount > 0

    async def backfill_company_display_names(self) -> int:
        """Repair stored all-caps multi-word company display_name values."""
        updated = 0
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id, company_json FROM leads") as cur:
                rows = await cur.fetchall()
            for lead_id, company_json in rows:
                company = json.loads(company_json)
                display = company.get("display_name")
                if not display:
                    continue
                fixed = normalize_company_display_name(display)
                if fixed == display:
                    continue
                company["display_name"] = fixed
                await db.execute(
                    "UPDATE leads SET company_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(company), now_utc().isoformat(), lead_id),
                )
                updated += 1
            if updated:
                await db.commit()
        return updated

    async def _backfill_suppressions(self) -> None:
        """Seed suppressions from existing terminal-status leads (idempotent)."""
        from leadgen.crm.suppression import SUPPRESSION_TAGS

        terminal_statuses = {"closed_lost", "unsubscribed"}
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, status, contact_json, company_json, tags_json, "
                "company_name FROM leads"
            ) as cur:
                rows = await cur.fetchall()

        for lead_id, status, contact_json, company_json, tags_json, company_name in rows:
            tags = json.loads(tags_json)
            tag_reason = next(
                (t.lower() for t in tags if t.lower() in SUPPRESSION_TAGS),
                None,
            )
            if status not in terminal_statuses and not tag_reason:
                continue
            reason = status if status in terminal_statuses else tag_reason
            contact = json.loads(contact_json)
            company = json.loads(company_json)
            key = self._name_company_key(contact, company)
            if not key:
                continue
            first = contact.get("first_name") or ""
            last = contact.get("last_name") or ""
            full_name = (contact.get("full_name") or f"{first} {last}").strip()
            await self.add_suppression(
                key,
                reason,
                source_lead_id=lead_id,
                display_name=full_name or None,
                company_name=company_name,
            )

    async def upsert(self, lead: Lead, dedupe_on_identity: bool = False) -> bool:
        """Insert or update a lead. Returns True if new, False if updated.

        By default, dedupe is by id then by email. Pass
        ``dedupe_on_identity=True`` (used by the fetch-new-leads paths) to also
        collapse leads onto an existing record sharing the same name+company
        identity — regardless of whether the incoming email is null. This stops
        repeated PDL pulls (which return null emails on the free tier) from
        stacking duplicate rows for someone we already enriched, and saves the
        PDL/Hunter credits we'd otherwise burn re-fetching a known person.

        When a lead collapses onto a *different* existing row (matched by email
        or by identity, not by id), the merge is NON-DESTRUCTIVE: a thinner
        re-fetch can never overwrite an existing email with null or downgrade
        the stored status. Only genuinely-missing fields (email, domain) are
        filled in from the incoming record.

        Raises :class:`EmailCollisionError` if the email being written already
        belongs to a different lead (the ``UNIQUE(contact_email)`` index), so
        batch callers can isolate the one colliding lead instead of aborting.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Check for existing: by id first (e.g. lead loaded from DB,
            # enriched, re-saved), then by email.
            existing_id: str | None = None
            matched_by_id = False
            async with db.execute("SELECT id FROM leads WHERE id = ?", (lead.id,)) as cur:
                row = await cur.fetchone()
                if row:
                    existing_id = row[0]
                    matched_by_id = True
            if not existing_id and lead.contact.email:
                async with db.execute(
                    "SELECT id FROM leads WHERE contact_email = ?", (lead.contact.email,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        existing_id = row[0]
            # Email-INDEPENDENT identity fallback. Runs whether or not the
            # incoming email is null, and matches existing rows in ANY status
            # with ANY email value — so a re-fetched person who comes back with
            # email=None still collapses onto the already-enriched row instead
            # of stacking a duplicate. (Problem 1)
            if dedupe_on_identity and not existing_id:
                new_key = self._name_company_key(
                    lead.contact.model_dump(), lead.company.model_dump()
                )
                if new_key:
                    async with db.execute(
                        "SELECT id, contact_json, company_json FROM leads "
                        "WHERE LOWER(company_name) = ?",
                        ((lead.company.name or "").lower(),),
                    ) as cur:
                        candidates = await cur.fetchall()
                    for cand_id, contact_json, company_json in candidates:
                        cand_key = self._name_company_key(
                            json.loads(contact_json), json.loads(company_json)
                        )
                        if cand_key == new_key:
                            existing_id = cand_id
                            break

            # Decide what to persist. A dedupe COLLAPSE (matched a different
            # row, not by id) merges non-destructively onto the stored record;
            # everything else persists the incoming lead as-is.
            if existing_id and not matched_by_id:
                async with db.execute(
                    "SELECT * FROM leads WHERE id = ?", (existing_id,)
                ) as cur:
                    erow = await cur.fetchone()
                target = self._row_to_lead(erow)
                if lead.contact.email and not target.contact.email:
                    target.contact.email = lead.contact.email
                    target.contact.email_verified = lead.contact.email_verified
                if lead.company.domain and not target.company.domain:
                    target.company.domain = lead.company.domain
                if lead.company.display_name and not target.company.display_name:
                    target.company.display_name = lead.company.display_name
                target.touch()
            else:
                target = lead

            row = (
                target.id,
                target.source.value,
                target.status.value,
                target.contact.model_dump_json(),
                target.company.model_dump_json(),
                target.score.model_dump_json() if target.score else None,
                json.dumps([r.model_dump(mode="json") for r in target.outreach_history]),
                target.notes,
                json.dumps(target.tags),
                json.dumps(target.raw_data),
                target.created_at.isoformat(),
                target.updated_at.isoformat(),
                target.company.name,
                target.contact.email,
                target.score.total if target.score else None,
            )

            try:
                if existing_id:
                    await db.execute("""
                        UPDATE leads SET
                            status=?, contact_json=?, company_json=?, score_json=?,
                            outreach_json=?, notes=?, tags_json=?, updated_at=?,
                            score_total=?, company_name=?, contact_email=?
                        WHERE id=?
                    """, (
                        target.status.value, target.contact.model_dump_json(),
                        target.company.model_dump_json(),
                        target.score.model_dump_json() if target.score else None,
                        json.dumps([r.model_dump(mode="json") for r in target.outreach_history]),
                        target.notes, json.dumps(target.tags),
                        target.updated_at.isoformat(),
                        target.score.total if target.score else None,
                        target.company.name, target.contact.email,
                        existing_id,
                    ))
                    await db.commit()
                    return False
                else:
                    await db.execute("""
                        INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, row)
                    await db.commit()
                    return True
            except sqlite3.IntegrityError as exc:
                # The only writable UNIQUE constraint is idx_leads_email, so an
                # IntegrityError here means the email already belongs to another
                # lead. Surface WHICH lead so the operator can reconcile. (P2)
                await db.rollback()
                holder = None
                if target.contact.email:
                    async with db.execute(
                        "SELECT id FROM leads WHERE contact_email = ? AND id != ?",
                        (target.contact.email, existing_id or target.id),
                    ) as cur:
                        r = await cur.fetchone()
                        holder = r[0] if r else None
                raise EmailCollisionError(target.contact.email, holder) from exc

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
        """Find duplicate leads. Returns list of (dedupe_key, [lead_ids]) for groups with >1.

        Groups on the email-INDEPENDENT name+company key, so a null-email row
        and an already-enriched row for the *same person* are recognized as
        duplicates (e.g. the two 'Lloyd Chatfield / Outside General Counsel'
        rows) even though only one of them has an email. (Problem 3)
        """
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
            key = self._name_company_key(contact, company)
            if not key:
                continue  # No identifying info, skip (can't safely dedupe)
            key_to_ids.setdefault(key, []).append(lead_id)
        return [(k, ids) for k, ids in key_to_ids.items() if len(ids) > 1]

    async def delete_duplicates(self, keep: str = "oldest") -> int:
        """Remove duplicate leads. keep='oldest' keeps first created; 'newest' keeps last updated.

        Within each duplicate group, an ENRICHED row (one that has an email) is
        always preferred over an email-less row regardless of the keep order,
        so collapsing the two-Lloyd case keeps the row carrying the verified
        email and drops the null-email re-fetch. The keep order only breaks ties
        among rows in the same email state. (Problem 3)
        """
        dupes = await self.find_duplicates()
        if not dupes:
            return 0
        order_col = "created_at" if keep == "oldest" else "updated_at"
        deleted = 0
        async with aiosqlite.connect(self.db_path) as db:
            for _key, ids in dupes:
                # Order so rows WITH an email sort first (contact_email IS NULL
                # is 0 for emails, 1 for nulls), then by the keep order. Keep
                # the first, delete the rest.
                async with db.execute(
                    f"SELECT id FROM leads WHERE id IN ({','.join('?'*len(ids))}) "
                    f"ORDER BY (contact_email IS NULL) ASC, {order_col} ASC",
                    ids,
                ) as cur:
                    ordered = [r[0] for r in await cur.fetchall()]
                keep_id = ordered[0]
                for lead_id in ordered[1:]:
                    await db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
                    deleted += 1
            await db.commit()
        return deleted

    async def count_all(self) -> int:
        """Return the total number of leads in the table."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM leads") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def delete_all(self) -> int:
        """Delete every lead from the table. Returns the number of rows removed.

        Destructive and irreversible — intended for the CLI `purge` command,
        which gates it behind an interactive confirmation. Deliberately not
        exposed as an MCP tool so the agent can never call it.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM leads") as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute("DELETE FROM leads")
            await db.commit()
            return count

    async def get_by_ids(self, ids: list[str]) -> list[Lead]:
        """Fetch leads whose ids are in *ids* (order not guaranteed)."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT * FROM leads WHERE id IN ({placeholders})",
                ids,
            ) as cur:
                rows = await cur.fetchall()
                return [self._row_to_lead(row) for row in rows]

    async def delete_by_ids(self, ids: list[str]) -> int:
        """Delete leads by id. Returns the number of rows removed.

        Destructive and irreversible — intended for the CLI `delete` command,
        which gates it behind an interactive confirmation. Deliberately not
        exposed as an MCP tool so the agent can never call it.
        """
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM leads WHERE id IN ({placeholders})",
                ids,
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute(
                f"DELETE FROM leads WHERE id IN ({placeholders})",
                ids,
            )
            await db.commit()
            return count

    async def delete_by_status(self, status: LeadStatus) -> int:
        """Delete all leads with the given status. Returns rows removed.

        Only rows whose status exactly matches *status* are deleted — other
        statuses are untouched. Destructive and irreversible — intended for
        the CLI `delete` command. Deliberately not exposed as an MCP tool.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM leads WHERE status = ?",
                (status.value,),
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute(
                "DELETE FROM leads WHERE status = ?",
                (status.value,),
            )
            await db.commit()
            return count

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
            created_at=parse_iso(created_at),
            updated_at=parse_iso(updated_at),
        )
