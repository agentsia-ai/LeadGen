"""LeadGen CRM — Cloudflare D1 backend.

Implements the same interface as :class:`leadgen.crm.database.LeadDatabase`,
but issues SQL over HTTP instead of talking to a local SQLite file. The engine
runs inside a Cloudflare Container, which cannot hold a D1 binding itself, so
it POSTs statements to a virtual hostname (default ``http://db.internal/query``)
that the parent Worker's outbound handler intercepts and executes against its
D1 binding. No Cloudflare SDK and no API token ever enter the container.

Wire protocol
-------------
Single statement::

    -> {"sql": "SELECT ...", "params": [...]}
    <- {"success": true, "results": [{col: val}, ...], "meta": {"changes": 0}}

Batch (the atomic unit — see below)::

    -> {"batch": [{"sql": ..., "params": [...]}, ...]}
    <- {"success": true, "batch": [{"results": [...], "meta": {...}}, ...]}

Errors are non-2xx with ``{"success": false, "error": "<message>"}``.

D1 transaction semantics
------------------------
**D1 has no multi-statement transactions.** ``batch()`` is the atomic unit: the
statements in one batch run in a single implicit transaction and either all
apply or none do. Every SQLite code path that relied on accumulating statements
and issuing one ``commit()`` had to be audited. The results:

*Converted to a batch* (multi-statement writes that must not half-apply):

- ``delete_duplicates`` — deletes N rows across M duplicate groups. A partial
  apply would leave a group collapsed on one side only, so a retry would pick a
  different survivor. Now one batch of DELETEs.
- ``delete_all`` / ``delete_by_ids`` / ``delete_by_status`` — each pairs a
  COUNT with a DELETE. Batching makes the count the caller is told about
  exactly the set that was removed, instead of a count that another writer
  could invalidate between the two round trips.
- ``backfill_company_display_names`` — N repair UPDATEs; a partial apply would
  leave the table half-normalized with no record of where it stopped.
- ``_backfill_suppressions`` — N INSERT OR IGNOREs seeded from terminal-status
  leads. Also collapses what was an HTTP round trip per row into one call.

*Deliberately NOT converted* (already a single statement, therefore already
atomic — batching would add nothing):

- ``upsert`` — the SELECTs are reads; the write is exactly one INSERT or one
  UPDATE. The SQLite version's ``rollback()`` existed only to unwind the open
  transaction after a failed INSERT; in D1 a failed statement simply does not
  apply. The check-then-write window between the dedup SELECTs and the write is
  NOT closed by a transaction in either backend — the ``UNIQUE(contact_email)``
  index is the real backstop, which is exactly why
  :class:`~leadgen.crm.database.EmailCollisionError` exists.
- **Outreach supersede** — ``Lead.supersede_unapproved_drafts_at_step`` drops
  the stale draft and appends the new one *in memory*; both land in the single
  ``outreach_json`` column via one UPDATE. The JSON-column design is what makes
  the supersede caveat atomic here for free: there is no window in which a
  lead has both the stale and the fresh draft approvable.
- **Send bookkeeping** — ``EmailSender.send_approved_record`` sets
  ``record.sent_at`` and ``lead.status`` together, persisted by one UPDATE.
  (The genuine non-atomicity on this path is that SMTP delivery happens
  *before* the DB write, so a write failure after a successful send loses the
  record. That is unchanged from SQLite and is not a D1 regression.)
- ``update_lead_status``'s suppression sync — writes the suppressions row and
  the leads row separately. On SQLite these were already two independent
  connections with two independent commits, so this is pre-existing behavior,
  not something D1 introduced. Left as-is rather than silently changing
  semantics during a migration.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from leadgen._time import now_utc
from leadgen.crm._shared import (
    LeadIdentityMixin,
    lead_from_mapping,
    lead_insert_values,
    lead_update_values,
)
from leadgen.crm.database import EmailCollisionError
from leadgen.models import Lead, LeadStatus
from leadgen.text import normalize_company_display_name

logger = logging.getLogger(__name__)

DEFAULT_D1_URL = "http://db.internal/query"

# D1 surfaces SQLite's constraint message verbatim, e.g.
# "D1_ERROR: UNIQUE constraint failed: leads.contact_email: SQLITE_CONSTRAINT".
_UNIQUE_VIOLATION = "unique constraint failed"


class D1Error(RuntimeError):
    """A statement was rejected by the outbound D1 proxy."""

    def __init__(self, message: str, *, sql: str | None = None):
        self.sql = sql
        super().__init__(message)

    @property
    def is_unique_violation(self) -> bool:
        return _UNIQUE_VIOLATION in str(self).lower()


class D1Result:
    """Normalized result of one statement."""

    __slots__ = ("rows", "meta")

    def __init__(self, rows: list[dict], meta: dict):
        self.rows = rows
        self.meta = meta

    @property
    def changes(self) -> int:
        return int(self.meta.get("changes") or 0)

    def first(self) -> dict | None:
        return self.rows[0] if self.rows else None

    def scalar(self, default: Any = None) -> Any:
        """First column of the first row — for COUNT(*) and similar."""
        row = self.first()
        if not row:
            return default
        return next(iter(row.values()), default)


def resolve_d1_url(explicit: str | None = None) -> str:
    """Resolve the outbound proxy URL: explicit > env > documented default."""
    return (
        (explicit or "").strip()
        or os.getenv("LEADGEN_D1_URL", "").strip()
        or DEFAULT_D1_URL
    )


class D1Client:
    """Minimal HTTP client for the Worker's D1 outbound handler."""

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.url = resolve_d1_url(url)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post(self, payload: dict) -> dict:
        try:
            response = await self._http().post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise D1Error(f"D1 proxy unreachable at {self.url}: {exc}") from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            raise D1Error(detail, sql=_first_sql(payload))

        data = response.json()
        if data.get("success") is False:
            raise D1Error(
                data.get("error") or "D1 proxy reported failure",
                sql=_first_sql(payload),
            )
        return data

    async def query(self, sql: str, params: list | None = None) -> D1Result:
        """Execute one statement."""
        data = await self._post({"sql": sql, "params": list(params or [])})
        return D1Result(data.get("results") or [], data.get("meta") or {})

    async def batch(self, statements: list[tuple[str, list]]) -> list[D1Result]:
        """Execute statements as ONE atomic unit. Empty input is a no-op."""
        if not statements:
            return []
        data = await self._post({
            "batch": [
                {"sql": sql, "params": list(params or [])} for sql, params in statements
            ]
        })
        return [
            D1Result(item.get("results") or [], item.get("meta") or {})
            for item in (data.get("batch") or [])
        ]


def _first_sql(payload: dict) -> str | None:
    if "sql" in payload:
        return payload["sql"]
    batch = payload.get("batch") or []
    return batch[0]["sql"] if batch else None


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"D1 proxy HTTP {response.status_code}: {response.text[:400]}"
    message = body.get("error") or body
    return f"D1 proxy HTTP {response.status_code}: {message}"


# ── Schema ────────────────────────────────────────────────────────────────────
# Byte-for-byte the same shape the SQLite backend creates, so a `.dump` from
# one imports cleanly into the other.

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
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
        company_name TEXT,
        contact_email TEXT,
        score_total REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)",
    "CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score_total DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email "
    "ON leads(contact_email) WHERE contact_email IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS suppressions (
        suppression_key TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        source_lead_id TEXT,
        display_name TEXT,
        company_name TEXT,
        created_at TEXT NOT NULL,
        notes TEXT DEFAULT ''
    )
    """,
)


class D1Database(LeadIdentityMixin):
    """Lead store backed by Cloudflare D1 over the container outbound handler.

    Mirrors :class:`leadgen.crm.database.LeadDatabase` method for method so
    callers (MCP handlers, CLI, the container's internal API) are backend-blind.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: D1Client | None = None,
    ):
        self.client = client or D1Client(url)
        self._initialized = False

    async def aclose(self) -> None:
        await self.client.aclose()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Create tables if absent, then seed suppressions. Runs once per process.

        The MCP layer calls ``init()`` at the top of every tool invocation,
        which is free against a local file but would be a burst of HTTP round
        trips per request here. The schema is idempotent and the suppression
        backfill is a full scan, so both are cached for the life of the
        process (i.e. re-run once per container wake).
        """
        if self._initialized:
            return
        # DDL is issued one statement at a time rather than as a batch: it runs
        # once per process, so the extra round trips are irrelevant, and it
        # avoids depending on DDL being accepted inside a D1 batch.
        for statement in _SCHEMA_STATEMENTS:
            await self.client.query(statement)
        await self._backfill_suppressions()
        self._initialized = True
        logger.info("D1 database initialized via %s", self.client.url)

    # ── Suppressions ─────────────────────────────────────────────────────────

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
        result = await self.client.query(
            """
            INSERT OR IGNORE INTO suppressions
                (suppression_key, reason, source_lead_id, display_name,
                 company_name, created_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                suppression_key,
                reason,
                source_lead_id,
                display_name,
                company_name,
                now_utc().isoformat(),
                notes,
            ],
        )
        return result.changes > 0

    async def is_suppression_key(self, suppression_key: str) -> tuple[bool, str | None]:
        """Return (True, reason) if *suppression_key* is on the suppression set."""
        result = await self.client.query(
            "SELECT reason FROM suppressions WHERE suppression_key = ?",
            [suppression_key],
        )
        row = result.first()
        if row:
            return True, row["reason"]
        return False, None

    async def list_suppressions(self, limit: int = 100) -> list[dict]:
        """Return suppression records for operator review."""
        result = await self.client.query(
            "SELECT * FROM suppressions ORDER BY created_at DESC LIMIT ?",
            [limit],
        )
        return result.rows

    async def remove_suppression(self, suppression_key: str) -> bool:
        """Delete a suppression record. Returns True if a row was removed."""
        result = await self.client.query(
            "DELETE FROM suppressions WHERE suppression_key = ?",
            [suppression_key],
        )
        return result.changes > 0

    async def _backfill_suppressions(self) -> None:
        """Seed suppressions from existing terminal-status leads (idempotent).

        BATCHED: the SQLite version issued one INSERT per qualifying lead on its
        own connection. Over HTTP that is a round trip per row, and a partial
        apply would leave the suppression set inconsistent with the lead
        statuses that justify it.
        """
        from leadgen.crm.suppression import SUPPRESSION_TAGS

        terminal_statuses = {"closed_lost", "unsubscribed"}
        result = await self.client.query(
            "SELECT id, status, contact_json, company_json, tags_json, "
            "company_name FROM leads"
        )

        statements: list[tuple[str, list]] = []
        now = now_utc().isoformat()
        for row in result.rows:
            tags = json.loads(row.get("tags_json") or "[]")
            tag_reason = next(
                (t.lower() for t in tags if t.lower() in SUPPRESSION_TAGS),
                None,
            )
            if row["status"] not in terminal_statuses and not tag_reason:
                continue
            reason = row["status"] if row["status"] in terminal_statuses else tag_reason
            contact = json.loads(row["contact_json"])
            company = json.loads(row["company_json"])
            key = self._name_company_key(contact, company)
            if not key:
                continue
            first = contact.get("first_name") or ""
            last = contact.get("last_name") or ""
            full_name = (contact.get("full_name") or f"{first} {last}").strip()
            statements.append((
                """
                INSERT OR IGNORE INTO suppressions
                    (suppression_key, reason, source_lead_id, display_name,
                     company_name, created_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    key,
                    reason,
                    row["id"],
                    full_name or None,
                    row.get("company_name"),
                    now,
                    "",
                ],
            ))

        await self.client.batch(statements)

    # ── Maintenance ──────────────────────────────────────────────────────────

    async def backfill_company_display_names(self) -> int:
        """Repair stored all-caps multi-word company display_name values.

        BATCHED: a partial apply would leave the table half-normalized with no
        marker of where it stopped, so a rerun could not tell repaired rows
        from untouched ones.
        """
        result = await self.client.query("SELECT id, company_json FROM leads")
        statements: list[tuple[str, list]] = []
        now = now_utc().isoformat()
        for row in result.rows:
            company = json.loads(row["company_json"])
            display = company.get("display_name")
            if not display:
                continue
            fixed = normalize_company_display_name(display)
            if fixed == display:
                continue
            company["display_name"] = fixed
            statements.append((
                "UPDATE leads SET company_json = ?, updated_at = ? WHERE id = ?",
                [json.dumps(company), now, row["id"]],
            ))
        await self.client.batch(statements)
        return len(statements)

    # ── Reads ────────────────────────────────────────────────────────────────

    async def get(self, lead_id: str) -> Lead | None:
        """Fetch a single lead by ID."""
        result = await self.client.query(
            "SELECT * FROM leads WHERE id = ?", [lead_id]
        )
        row = result.first()
        return lead_from_mapping(row) if row else None

    async def list(
        self,
        status: LeadStatus | None = None,
        min_score: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Lead]:
        """List leads with optional filters."""
        conditions: list[str] = []
        params: list = []

        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if min_score is not None:
            conditions.append("score_total >= ?")
            params.append(min_score)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params += [limit, offset]

        result = await self.client.query(
            f"SELECT * FROM leads {where} ORDER BY score_total DESC LIMIT ? OFFSET ?",
            params,
        )
        return [lead_from_mapping(row) for row in result.rows]

    async def get_by_ids(self, ids: list[str]) -> list[Lead]:
        """Fetch leads whose ids are in *ids* (order not guaranteed)."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        result = await self.client.query(
            f"SELECT * FROM leads WHERE id IN ({placeholders})", list(ids)
        )
        return [lead_from_mapping(row) for row in result.rows]

    async def count_all(self) -> int:
        """Return the total number of leads in the table."""
        result = await self.client.query("SELECT COUNT(*) AS n FROM leads")
        return int(result.scalar(0) or 0)

    async def count_by_status(self) -> dict[str, int]:
        """Return lead counts grouped by status."""
        result = await self.client.query(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
        )
        return {row["status"]: int(row["n"]) for row in result.rows}

    async def find_duplicates(self) -> list[tuple[str, list[str]]]:
        """Find duplicate leads. Returns (dedupe_key, [lead_ids]) for groups with >1.

        Groups on the email-INDEPENDENT name+company key, so a null-email row
        and an already-enriched row for the *same person* are recognized as
        duplicates even though only one of them has an email.
        """
        result = await self.client.query(
            "SELECT id, contact_json, company_json FROM leads"
        )
        key_to_ids: dict[str, list[str]] = {}
        for row in result.rows:
            key = self._name_company_key(
                json.loads(row["contact_json"]), json.loads(row["company_json"])
            )
            if not key:
                continue  # No identifying info, skip (can't safely dedupe)
            key_to_ids.setdefault(key, []).append(row["id"])
        return [(k, ids) for k, ids in key_to_ids.items() if len(ids) > 1]

    # ── Writes ───────────────────────────────────────────────────────────────

    async def upsert(self, lead: Lead, dedupe_on_identity: bool = False) -> bool:
        """Insert or update a lead. Returns True if new, False if updated.

        Semantics are identical to the SQLite backend; see
        :meth:`leadgen.crm.database.LeadDatabase.upsert`. NOT batched: the
        write is a single statement and therefore already atomic (see module
        docstring).
        """
        existing_id: str | None = None
        matched_by_id = False

        found = await self.client.query(
            "SELECT id FROM leads WHERE id = ?", [lead.id]
        )
        if found.first():
            existing_id = found.first()["id"]
            matched_by_id = True

        if not existing_id and lead.contact.email:
            found = await self.client.query(
                "SELECT id FROM leads WHERE contact_email = ?", [lead.contact.email]
            )
            if found.first():
                existing_id = found.first()["id"]

        if dedupe_on_identity and not existing_id:
            new_key = self._name_company_key(
                lead.contact.model_dump(), lead.company.model_dump()
            )
            if new_key:
                candidates = await self.client.query(
                    "SELECT id, contact_json, company_json FROM leads "
                    "WHERE LOWER(company_name) = ?",
                    [(lead.company.name or "").lower()],
                )
                for row in candidates.rows:
                    cand_key = self._name_company_key(
                        json.loads(row["contact_json"]), json.loads(row["company_json"])
                    )
                    if cand_key == new_key:
                        existing_id = row["id"]
                        break

        if existing_id and not matched_by_id:
            existing = await self.client.query(
                "SELECT * FROM leads WHERE id = ?", [existing_id]
            )
            target = lead_from_mapping(existing.first())
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

        try:
            if existing_id:
                await self.client.query(
                    """
                    UPDATE leads SET
                        status=?, contact_json=?, company_json=?, score_json=?,
                        outreach_json=?, notes=?, tags_json=?, updated_at=?,
                        score_total=?, company_name=?, contact_email=?
                    WHERE id=?
                    """,
                    [*lead_update_values(target), existing_id],
                )
                return False
            await self.client.query(
                "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                lead_insert_values(target),
            )
            return True
        except D1Error as exc:
            if not exc.is_unique_violation:
                raise
            # The only writable UNIQUE constraint is idx_leads_email, so this
            # means the email already belongs to another lead. Surface WHICH
            # lead so the operator can reconcile.
            holder = None
            if target.contact.email:
                found = await self.client.query(
                    "SELECT id FROM leads WHERE contact_email = ? AND id != ?",
                    [target.contact.email, existing_id or target.id],
                )
                row = found.first()
                holder = row["id"] if row else None
            raise EmailCollisionError(target.contact.email, holder) from exc

    # ── Destructive operations ───────────────────────────────────────────────
    # All batched: each pairs a count with a delete, and the caller reports the
    # count as "rows removed". Splitting them across round trips would let the
    # reported number describe a different set than the one actually deleted.

    async def delete_duplicates(self, keep: str = "oldest") -> int:
        """Remove duplicate leads. keep='oldest' keeps first created; 'newest' last updated.

        Within each duplicate group an ENRICHED row (one that has an email) is
        always preferred over an email-less row regardless of the keep order,
        so the keep order only breaks ties among rows in the same email state.

        BATCHED: all DELETEs across all groups apply together. A partial apply
        would collapse some groups and not others, and a rerun could then pick
        a different survivor for the groups that did land.
        """
        order_key = "created_at" if keep == "oldest" else "updated_at"
        result = await self.client.query(
            "SELECT id, contact_json, company_json, contact_email, "
            "created_at, updated_at FROM leads"
        )

        groups: dict[str, list[dict]] = {}
        for row in result.rows:
            key = self._name_company_key(
                json.loads(row["contact_json"]), json.loads(row["company_json"])
            )
            if not key:
                continue
            groups.setdefault(key, []).append(row)

        statements: list[tuple[str, list]] = []
        for rows in groups.values():
            if len(rows) < 2:
                continue
            # Rows WITH an email sort first, then by the keep order — the same
            # ordering the SQLite backend expresses as
            # `ORDER BY (contact_email IS NULL) ASC, <order_key> ASC`.
            ordered = sorted(
                rows,
                key=lambda r: (r.get("contact_email") is None, r.get(order_key) or ""),
            )
            for row in ordered[1:]:
                statements.append(
                    ("DELETE FROM leads WHERE id = ?", [row["id"]])
                )

        await self.client.batch(statements)
        return len(statements)

    async def delete_all(self) -> int:
        """Delete every lead. Returns rows removed.

        Destructive and irreversible — intended for the CLI `purge` command,
        which gates it behind an interactive confirmation. Deliberately not
        exposed as an MCP tool so the agent can never call it.
        """
        results = await self.client.batch([
            ("SELECT COUNT(*) AS n FROM leads", []),
            ("DELETE FROM leads", []),
        ])
        return int(results[0].scalar(0) or 0)

    async def delete_by_ids(self, ids: list[str]) -> int:
        """Delete leads by id. Returns rows removed.

        Destructive and irreversible — intended for the CLI `delete` command.
        Deliberately not exposed as an MCP tool.
        """
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        results = await self.client.batch([
            (
                f"SELECT COUNT(*) AS n FROM leads WHERE id IN ({placeholders})",
                list(ids),
            ),
            (f"DELETE FROM leads WHERE id IN ({placeholders})", list(ids)),
        ])
        return int(results[0].scalar(0) or 0)

    async def delete_by_status(self, status: LeadStatus) -> int:
        """Delete all leads with the given status. Returns rows removed.

        Only rows whose status exactly matches *status* are deleted.
        Destructive and irreversible — intended for the CLI `delete` command.
        Deliberately not exposed as an MCP tool.
        """
        results = await self.client.batch([
            ("SELECT COUNT(*) AS n FROM leads WHERE status = ?", [status.value]),
            ("DELETE FROM leads WHERE status = ?", [status.value]),
        ])
        return int(results[0].scalar(0) or 0)
