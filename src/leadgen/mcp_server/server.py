"""
LeadGen MCP Server
Exposes LeadGen as an MCP tool server so Claude Desktop (or any MCP client)
can query leads, trigger searches, score, and draft outreach conversationally.

Usage:
    leadgen mcp
    # or: python -m leadgen.mcp

Then add to Claude Desktop config:
    {
      "mcpServers": {
        "leadgen": {
          "command": "python",
          "args": ["-m", "leadgen.mcp"]
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from leadgen._time import now_utc
from leadgen.ai.drafter import OutreachDrafter
from leadgen.ai.scorer import LeadScorer
from leadgen.config.loader import display_agent_name, load_api_keys, load_config
from leadgen.crm.database import EmailCollisionError, LeadDatabase
from leadgen.models import INERT_STATUSES, LeadStatus

logger = logging.getLogger(__name__)


def _json(obj) -> list[TextContent]:
    """Serialize tool output; mode='json' on model dumps keeps datetimes safe."""
    return [TextContent(type="text", text=json.dumps(obj, indent=2))]


# ── Server init ───────────────────────────────────────────────────────────────

app = Server("leadgen")

# Config / keys / database are initialized in main() rather than at import time
# so that the cwd has had a chance to be set by the caller (e.g. agentsia-core's
# AgentContext.activate() chdir's into agents/<agent>/ before invoking main()).
# Loading at import time would search for `config.yaml` in whatever cwd happened
# to be active when the module first got imported, which is rarely correct.
# Tool handlers below read these as module globals at call time, so they see
# the values main() assigns.
config = None  # type: ignore[assignment]
keys = None    # type: ignore[assignment]
db = None      # type: ignore[assignment]

# ── Pluggable scorer / drafter classes ────────────────────────────────────────
# These default to the generic engine implementations. Downstream productized
# agents (e.g. agentsia-core's Rex) override them by passing scorer_cls /
# drafter_cls to main(), which sets the module-level globals before the MCP
# server starts handling requests. Tool handlers below read these globals at
# request time, so injection takes effect for every subsequent call.
SCORER_CLASS: type[LeadScorer] = LeadScorer
DRAFTER_CLASS: type[OutreachDrafter] = OutreachDrafter


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_pipeline",
            description="Get a summary of the current lead pipeline — counts by status and top scored leads.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="search_leads",
            description="Search and filter leads in the database by status, minimum score, or limit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: new, enriched, scored, queued, contacted, responded, etc.",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Minimum ICP score (0.0–1.0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of leads to return (default 10)",
                    },
                },
            },
        ),
        Tool(
            name="fetch_new_leads",
            description="Pull fresh leads from Apollo, Hunter, or People Data Labs. Use domain for Hunter; use source='pdl' for PDL (100 free credits/mo); omit both for Apollo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of leads to fetch (default 25)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Company domain for Hunter search (e.g. acmecorp.com). When provided, uses Hunter.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["apollo", "hunter", "pdl"],
                        "description": "Lead source: apollo, hunter (requires domain), or pdl (100 free credits/month).",
                    },
                    "relax_industry": {
                        "type": "boolean",
                        "description": "PDL only. If the industry filter matches nothing, broaden to geography+size. "
                        "Returned leads are flagged industry_relaxed and are NOT on-vertical. Default false (fail closed).",
                    },
                },
            },
        ),
        Tool(
            name="enrich_lead",
            description=(
                "Find and verify a deliverable email for existing lead(s) via Hunter. "
                "Resolves the company domain first (uses any domain PDL provided, else "
                "Hunter name->domain, else a derived guess), then runs Hunter "
                "email-finder (>=70 confidence) + email-verifier and writes the result "
                "back onto the SAME lead record. REQUIRED before outreach since PDL's "
                "free tier returns no email. Pass lead_id or lead_ids; keep batches small "
                "(1-2) to conserve Hunter credits."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "A single lead ID to enrich.",
                    },
                    "lead_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple lead IDs to enrich in one call.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "If neither lead_id nor lead_ids is given, enrich up to this many leads that still lack a verified email (default 2).",
                    },
                },
            },
        ),
        Tool(
            name="scrape_lead_email",
            description=(
                "Scrape publicly published contact emails from a lead's own website "
                "(homepage + contact/about/appointments subpages). Use as a fallback "
                "after enrich_lead (Hunter) misses — Hunter first, scraper second. "
                "Returns candidate emails with page provenance; does NOT auto-verify. "
                "Pass apply=true to write the top candidate as an unverified email "
                "(operator review still required before send). Requires lead_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Lead to scrape (required — one explicit target per call)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Optional domain override (e.g. wcalvertlaw.com)",
                    },
                    "apply": {
                        "type": "boolean",
                        "description": (
                            "If true, write the top-ranked candidate to the lead as "
                            "email_verified=false with a scrape provenance note. Default false."
                        ),
                    },
                },
                "required": ["lead_id"],
            },
        ),
        Tool(
            name="score_leads",
            description=(
                "Run AI scoring on leads against your ICP. Pass lead_id or lead_ids "
                "to score exactly those records regardless of pipeline status; "
                "otherwise scores up to limit unscored leads in status=new."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "A single lead ID to score.",
                    },
                    "lead_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple lead IDs to score in one call.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "If neither lead_id nor lead_ids is given, score up to this many leads in status=new (default 20).",
                    },
                },
            },
        ),
        Tool(
            name="draft_outreach",
            description="Generate personalized outreach email drafts for high-scoring leads.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Specific lead ID to draft for. If omitted, drafts for top queued leads.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of drafts to generate (default 5)",
                    },
                },
            },
        ),
        Tool(
            name="draft_followup",
            description=(
                "Draft the next follow-up email for a lead that already has sent outreach. "
                "Uses the lead's sent-history step count (1=first follow-up, 2=second, "
                "3=third/final). Requires lead_id. Refuses at max_follow_ups. Does not "
                "reset contacted status or overwrite sent records."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Lead ID to draft the next follow-up for (required)",
                    },
                },
                "required": ["lead_id"],
            },
        ),
        Tool(
            name="approve_outreach",
            description="Approve drafted outreach messages and queue them for sending.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string", "description": "Lead ID to approve outreach for"},
                },
                "required": ["lead_id"],
            },
        ),
        Tool(
            name="send_outreach",
            description=(
                "Send one APPROVED outreach email to the real prospect (production send). "
                "Requires an explicit lead_id — one targeted send per call, never a batch "
                "sweep. The draft must already be approved (approve_outreach) and not yet "
                "sent. Refuses if the daily send cap would be exceeded. Returns the exact "
                "recipient address, sent_at, and new lead status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Lead to send approved outreach for (required)",
                    },
                    "outreach_id": {
                        "type": "string",
                        "description": (
                            "Optional OutreachRecord id; defaults to the first "
                            "approved, unsent draft on this lead"
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Simulate send without delivering email",
                    },
                },
                "required": ["lead_id"],
            },
        ),
        Tool(
            name="send_test_draft",
            description=(
                "Send a draft outreach email to a test inbox (e.g. your own address) "
                "for preview. Does NOT send to the prospect, does NOT modify the lead "
                "record, and does NOT mark the draft as sent. Uses the same SMTP/HTML "
                "rendering as production sends. Real prospect sends use send_outreach "
                "(one lead at a time) or the leadgen send CLI batch sweep."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Lead whose draft to preview-send",
                    },
                    "test_recipient": {
                        "type": "string",
                        "description": "Email address to deliver the test to (required)",
                    },
                    "outreach_id": {
                        "type": "string",
                        "description": "Optional OutreachRecord id; defaults to the latest draft",
                    },
                },
                "required": ["lead_id", "test_recipient"],
            },
        ),
        Tool(
            name="update_lead",
            description=(
                "Manually set or correct a lead's contact info (name, email, domain, phone). "
                "Use when enrichment pointed at the wrong domain, PDL has the wrong contact name, "
                "or an operator found an email on a company site. Only passed fields are updated. "
                "Manual emails default to email_verified=false; pass email_verified=true with "
                "verification_source (e.g. 'manual: published on site') to assert a published "
                "address Hunter cannot verify, or verify=true to run Hunter email-verifier "
                "(not finder) on the address."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "first_name": {
                        "type": "string",
                        "description": "Contact first name (title-cased on write)",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Contact last name (title-cased on write)",
                    },
                    "full_name": {
                        "type": "string",
                        "description": "Contact full name (title-cased; splits into first/last)",
                    },
                    "email": {"type": "string", "description": "Contact email to set"},
                    "domain": {
                        "type": "string",
                        "description": "Company domain override (e.g. americorpre.com)",
                    },
                    "phone": {"type": "string", "description": "Contact phone to set"},
                    "email_verified": {
                        "type": "boolean",
                        "description": "Mark email as verified (default false when email is set)",
                    },
                    "verification_source": {
                        "type": "string",
                        "description": (
                            "Provenance when email_verified=true (default 'manual'). "
                            "Use e.g. 'manual: published on site' for operator-confirmed addresses."
                        ),
                    },
                    "verify": {
                        "type": "boolean",
                        "description": "Run Hunter email-verifier on the email and set email_verified from the result",
                    },
                    "status": {
                        "type": "string",
                        "description": "Optional pipeline status (e.g. enriched)",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note appended to the lead for provenance",
                    },
                },
                "required": ["lead_id"],
            },
        ),
        Tool(
            name="update_lead_status",
            description="Update the status of a lead in the pipeline.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "status": {"type": "string", "description": "New status value (e.g. deferred, new, scored)"},
                    "notes": {"type": "string", "description": "Optional notes to add"},
                },
                "required": ["lead_id", "status"],
            },
        ),
        Tool(
            name="get_lead_detail",
            description="Get full details for a specific lead including score and outreach history.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                },
                "required": ["lead_id"],
            },
        ),
        Tool(
            name="suppress_lead",
            description=(
                "Permanently suppress a lead (or company domain) from future fetch/enrich. "
                "Non-destructive and reversible via unsuppress_lead. Requires exactly one "
                "identifier — no bulk suppress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "string",
                        "description": "Existing lead id (uses name+company identity)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Person name (requires company)",
                    },
                    "company": {
                        "type": "string",
                        "description": "Company name (requires name)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Suppress all leads at this company domain",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason recorded (e.g. icp_mismatch, replied_no, exhausted, manual)",
                    },
                },
            },
        ),
        Tool(
            name="unsuppress_lead",
            description=(
                "Remove a suppression entry so the identity can be fetched/enriched again. "
                "Requires exactly one identifier — no bulk unsuppress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "suppression_key": {
                        "type": "string",
                        "description": "Exact suppression_key from suppress_lead or the suppressions table",
                    },
                    "lead_id": {
                        "type": "string",
                        "description": "Existing lead id (resolves to name+company identity key)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Person name (requires company)",
                    },
                    "company": {
                        "type": "string",
                        "description": "Company name (requires name)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Domain-level suppression key to remove",
                    },
                },
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await db.init()

    if name == "get_pipeline":
        counts = await db.count_by_status()
        top_leads = await db.list(min_score=0.7, limit=5)
        top_leads = [l for l in top_leads if l.status not in INERT_STATUSES]

        summary = {
            "agent": display_agent_name(config),
            "pipeline_counts": counts,
            "total": sum(counts.values()),
            "top_leads": [
                {
                    "id": l.id,
                    "name": l.display_name,
                    "company": l.company.name,
                    "title": l.contact.title,
                    "score": round(l.score.total, 2) if l.score else None,
                    "status": l.status.value,
                }
                for l in top_leads
            ],
        }
        return [TextContent(type="text", text=json.dumps(summary, indent=2))]

    elif name == "search_leads":
        status = LeadStatus(arguments["status"]) if "status" in arguments else None
        leads = await db.list(
            status=status,
            min_score=arguments.get("min_score"),
            limit=arguments.get("limit", 10),
        )
        result = [
            {
                "id": l.id,
                "name": l.display_name,
                "company": l.company.name,
                "title": l.contact.title,
                "email": l.contact.email,
                "score": round(l.score.total, 2) if l.score else None,
                "status": l.status.value,
                "score_reasoning": l.score.reasoning if l.score else None,
            }
            for l in leads
        ]
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "fetch_new_leads":
        from leadgen.sources.apollo import ApolloConnector
        from leadgen.sources.hunter import HunterConnector
        from leadgen.sources.pdl import PDLConnector
        limit = arguments.get("limit", 25)
        domain = arguments.get("domain")
        src = arguments.get("source")

        use_hunter = src == "hunter" or (src is None and domain)
        use_pdl = src == "pdl"

        if use_hunter:
            if not domain:
                return [TextContent(type="text", text=json.dumps({"error": "Hunter requires domain parameter"}))]
            if not keys.hunter:
                return [TextContent(type="text", text=json.dumps({"error": "HUNTER_API_KEY is not set. Provide it as an environment variable (set it in Doppler for production, or a local .env for development)."}))]
            async with HunterConnector(config, keys) as hunter:
                leads = await hunter.domain_search(domain=domain, limit=limit)
        elif use_pdl:
            if not keys.pdl:
                return [TextContent(type="text", text=json.dumps({"error": "PDL_API_KEY is not set. Provide it as an environment variable (set it in Doppler for production, or a local .env for development)."}))]
            async with PDLConnector(config, keys) as pdl:
                leads = await pdl.search(
                    limit=limit, relax_industry=arguments.get("relax_industry", False)
                )
        else:
            if not keys.apollo:
                return [TextContent(type="text", text=json.dumps({"error": "APOLLO_API_KEY is not set. Provide it as an environment variable (set it in Doppler for production, or a local .env for development)."}))]
            async with ApolloConnector(config, keys) as apollo:
                leads = await apollo.search(limit=limit)

        added = 0
        suppressed = 0
        from leadgen.crm.suppression import check_lead_suppressed

        for lead in leads:
            is_blocked, _reason = await check_lead_suppressed(db, lead)
            if is_blocked:
                suppressed += 1
                continue
            is_new = await db.upsert(lead, dedupe_on_identity=True)
            if is_new:
                added += 1

        # Echo back a preview of the records THIS call actually fetched so the
        # result can be verified at a glance — without having to re-query the
        # DB (where older, higher-scored leads sort to the top and can be
        # mistaken for the fresh pull).
        preview = [
            {
                "name": lead.display_name,
                "company": lead.company.name,
                "title": lead.contact.title,
                "industry": lead.company.industry,
                "industry_relaxed": lead.industry_relaxed,
                "location": ", ".join(p for p in (lead.company.city, lead.company.state) if p) or None,
                "email": lead.contact.email,
                "email_verified": lead.contact.email_verified,
                "source": lead.source.value,
                "employee_count": (
                    lead.company.employee_count
                    if not lead.company.employee_count_unknown
                    else "unknown"
                ),
            }
            for lead in leads
        ]

        return [TextContent(
            type="text",
            text=json.dumps({
                "source": src or ("hunter" if domain else "apollo"),
                "fetched": len(leads),
                "new_leads_added": added,
                "suppressed_skipped": suppressed,
                "preview": preview,
            }, indent=2)
        )]

    elif name == "enrich_lead":
        from leadgen.sources.hunter import HunterConnector

        if not keys.hunter:
            return [TextContent(type="text", text=json.dumps({
                "error": "HUNTER_API_KEY is not set. Provide it as an environment variable (set it in Doppler for production, or a local .env for development)."
            }))]

        # Resolve target lead ids: explicit id(s), else fall back to leads that
        # still lack a verified email (small default batch to save credits).
        limit = arguments.get("limit", 2)
        ids: list[str] = []
        if arguments.get("lead_id"):
            ids.append(arguments["lead_id"])
        if arguments.get("lead_ids"):
            ids.extend(arguments["lead_ids"])
        if not ids:
            candidates = await db.list(limit=max(limit * 5, 10))
            ids = [
                l.id for l in candidates
                if not (l.contact.email and l.contact.email_verified)
            ][:limit]

        results = []
        async with HunterConnector(config, keys) as hunter:
            for lead_id in ids:
                lead = await db.get(lead_id)
                if not lead:
                    results.append({"lead_id": lead_id, "status": "not_found",
                                    "reason": "no lead with that id"})
                    continue

                company = lead.company.name or "unknown company"
                from leadgen.crm.suppression import check_lead_suppressed

                is_blocked, suppress_reason = await check_lead_suppressed(db, lead)
                if is_blocked:
                    results.append({
                        "lead_id": lead.id, "name": lead.display_name, "company": company,
                        "status": "suppressed",
                        "reason": f"previously {suppress_reason}",
                    })
                    continue

                if lead.contact.email and lead.contact.email_verified:
                    # Skip — don't re-spend Hunter on an already-verified lead.
                    results.append({
                        "lead_id": lead.id, "name": lead.display_name, "company": company,
                        "email": lead.contact.email, "email_verified": True,
                        "status": "already_verified",
                    })
                    continue

                # Each lead is written independently: a UNIQUE(contact_email)
                # collision (the resolved email already belongs to another row)
                # skips ONLY this lead and surfaces which row holds it, instead
                # of aborting the whole batch. (Problem 2)
                try:
                    domain, source = await hunter.resolve_company_domain_with_source(lead)
                    if not domain:
                        await db.upsert(lead)  # nothing changed, but keep record consistent
                        results.append({
                            "lead_id": lead.id, "name": lead.display_name, "company": company,
                            "status": "no_domain",
                            "reason": f"no domain resolvable for {company}",
                        })
                        continue

                    # Reuse the engine's find+verify path; domain is already set
                    # on the lead, so this consumes no extra name->domain credit.
                    await hunter.enrich_lead_email(lead)

                    if lead.contact.email:
                        lead.status = LeadStatus.ENRICHED
                        lead.touch()
                        await db.upsert(lead)
                        results.append({
                            "lead_id": lead.id, "name": lead.display_name, "company": company,
                            "domain": domain, "domain_source": source,
                            "email": lead.contact.email,
                            "email_verified": lead.contact.email_verified,
                            "status": "enriched" if lead.contact.email_verified else "found_unverified",
                        })
                    else:
                        # Persist the resolved domain even when no email was found,
                        # so a later retry doesn't re-pay for domain resolution.
                        await db.upsert(lead)
                        results.append({
                            "lead_id": lead.id, "name": lead.display_name, "company": company,
                            "domain": domain, "domain_source": source,
                            "status": "no_email",
                            "reason": f"no Hunter match >=70 confidence for {lead.display_name} at {domain}",
                        })
                except EmailCollisionError as exc:
                    results.append({
                        "lead_id": lead.id, "name": lead.display_name, "company": company,
                        "email": exc.email, "status": "collision",
                        "reason": f"collision: email already on lead {exc.existing_lead_id}",
                    })
                except Exception as exc:  # noqa: BLE001 — isolate one lead, keep batch alive
                    logger.exception("enrich_lead failed for %s", lead.id)
                    results.append({
                        "lead_id": lead.id, "name": lead.display_name, "company": company,
                        "status": "error", "reason": str(exc),
                    })

        return [TextContent(type="text", text=json.dumps({
            "enriched": sum(1 for r in results if r.get("email_verified")),
            "processed": len(results),
            "results": results,
        }, indent=2))]

    elif name == "scrape_lead_email":
        lead_id = arguments.get("lead_id")
        if not lead_id:
            return _json({
                "error": "lead_id is required — one explicit target per call",
            })

        from leadgen.sources.email_scraper import scrape_lead_email_by_id

        result = await scrape_lead_email_by_id(
            db,
            lead_id,
            domain=arguments.get("domain"),
            apply=arguments.get("apply", False),
        )
        return _json(result)

    elif name == "score_leads":
        limit = arguments.get("limit", 20)

        # Resolve target lead ids: explicit id(s), else sweep status=new.
        ids: list[str] = []
        if arguments.get("lead_id"):
            ids.append(arguments["lead_id"])
        if arguments.get("lead_ids"):
            ids.extend(arguments["lead_ids"])

        not_found: list[str] = []
        if ids:
            leads = []
            for lead_id in ids:
                lead = await db.get(lead_id)
                if lead:
                    leads.append(lead)
                else:
                    not_found.append(lead_id)
        else:
            leads = await db.list(status=LeadStatus.NEW, limit=limit)

        skipped_inert = [l.id for l in leads if l.status in INERT_STATUSES]
        leads = [l for l in leads if l.status not in INERT_STATUSES]

        if not leads:
            payload: dict = {
                "leads_scored": 0,
                "passed_threshold": 0,
                "threshold": config.scoring.threshold,
            }
            if skipped_inert:
                payload["skipped_inert"] = skipped_inert
            if not_found:
                payload["not_found"] = not_found
            return [TextContent(type="text", text=json.dumps(payload))]

        scorer = SCORER_CLASS(config, keys)
        scored = await scorer.score_batch(leads)

        for lead in leads:
            lead.status = LeadStatus.SCORED
            await db.upsert(lead)

        payload = {
            "leads_scored": len(leads),
            "passed_threshold": len(scored),
            "threshold": config.scoring.threshold,
        }
        if skipped_inert:
            payload["skipped_inert"] = skipped_inert
        if not_found:
            payload["not_found"] = not_found
        return [TextContent(type="text", text=json.dumps(payload))]

    elif name == "draft_outreach":
        drafter = DRAFTER_CLASS(config, keys)

        if "lead_id" in arguments:
            lead = await db.get(arguments["lead_id"])
            leads = [lead] if lead else []
        else:
            leads = await db.list(status=LeadStatus.SCORED, limit=arguments.get("limit", 5))

        drafted = []
        for lead in leads:
            if lead.status in INERT_STATUSES:
                continue
            record = await drafter.draft_initial(lead)
            lead.supersede_unapproved_drafts_at_step(record.sequence_step)
            lead.outreach_history.append(record)
            lead.status = LeadStatus.QUEUED
            lead.touch()
            await db.upsert(lead)
            drafted.append({
                "lead_id": lead.id,
                "name": lead.display_name,
                "company": lead.company.name,
                "outreach_id": record.id,
                "subject": record.subject,
                "body": record.body,
                "body_preview": record.body[:200] + ("..." if len(record.body) > 200 else ""),
            })

        return _json(drafted)

    elif name == "draft_followup":
        lead_id = arguments.get("lead_id")
        if not lead_id:
            return _json({"error": "lead_id is required — one explicit target per call"})

        lead = await db.get(lead_id)
        if not lead:
            return _json({"error": "Lead not found", "lead_id": lead_id})

        if lead.status in INERT_STATUSES:
            return _json({
                "error": f"Lead is {lead.status.value} — inert, not eligible for follow-up drafts",
                "lead_id": lead_id,
            })

        if lead.next_follow_up_step < 1:
            return _json({
                "error": (
                    "No sent outreach on this lead — use draft_outreach for the initial "
                    "message first"
                ),
                "lead_id": lead_id,
            })

        if lead.next_follow_up_step >= config.outreach.max_follow_ups:
            return _json({
                "error": (
                    f"Lead has reached max follow-ups ({config.outreach.max_follow_ups})"
                ),
                "lead_id": lead_id,
                "next_follow_up_step": lead.next_follow_up_step,
            })

        drafter = DRAFTER_CLASS(config, keys)
        try:
            record = await drafter.draft_followup(lead)
        except ValueError as exc:
            return _json({"error": str(exc), "lead_id": lead_id})

        prior_status = lead.status
        lead.supersede_unapproved_drafts_at_step(record.sequence_step)
        lead.outreach_history.append(record)
        lead.status = prior_status
        lead.touch()
        await db.upsert(lead)

        return _json([{
            "lead_id": lead.id,
            "name": lead.display_name,
            "company": lead.company.name,
            "outreach_id": record.id,
            "sequence_step": record.sequence_step,
            "follow_up_number": record.sequence_step,
            "status": lead.status.value,
            "subject": record.subject,
            "body": record.body,
            "body_preview": record.body[:200] + ("..." if len(record.body) > 200 else ""),
        }])

    elif name == "approve_outreach":
        lead = await db.get(arguments["lead_id"])
        if not lead:
            return [TextContent(type="text", text=json.dumps({"error": "Lead not found"}))]

        pending = [r for r in lead.outreach_history if r.approved_at is None]
        for record in pending:
            record.approved_at = now_utc()

        lead.touch()
        await db.upsert(lead)
        return _json({
            "approved": len(pending),
            "lead": lead.display_name,
        })

    elif name == "send_outreach":
        from leadgen.outreach.email import DailyLimitReached, EmailSender

        lead_id = arguments.get("lead_id")
        if not lead_id:
            return _json({"error": "lead_id is required — one explicit target per call"})

        lead = await db.get(lead_id)
        if not lead:
            return _json({"error": "Lead not found", "lead_id": lead_id})

        if lead.status in INERT_STATUSES:
            return _json({
                "error": f"Lead is {lead.status.value} — inert, not eligible for send",
                "lead_id": lead_id,
            })

        outreach_id = arguments.get("outreach_id")
        if outreach_id:
            record = next(
                (r for r in lead.outreach_history if r.id == outreach_id), None
            )
            if not record:
                return _json({
                    "error": f"No outreach record with id {outreach_id}",
                    "lead_id": lead_id,
                })
        else:
            record = next(
                (r for r in lead.outreach_history if r.approved_at and not r.sent_at),
                None,
            )
            if not record:
                has_unapproved = any(
                    r for r in lead.outreach_history if not r.approved_at and not r.sent_at
                )
                if has_unapproved:
                    return _json({
                        "error": "Outreach not approved — call approve_outreach first",
                        "lead_id": lead_id,
                    })
                has_sent = any(r.sent_at for r in lead.outreach_history)
                if has_sent:
                    return _json({
                        "error": (
                            "No pending approved outreach — call draft_followup to "
                            "draft the next message in the sequence"
                        ),
                        "lead_id": lead_id,
                        "sent_count": len(
                            [r for r in lead.outreach_history if r.sent_at]
                        ),
                    })
                return _json({
                    "error": "No approved, unsent outreach draft on this lead",
                    "lead_id": lead_id,
                })

        if not record.approved_at:
            return _json({
                "error": "Outreach not approved — call approve_outreach first",
                "lead_id": lead_id,
                "outreach_id": record.id,
            })
        if record.sent_at:
            return _json({
                "error": "Outreach already sent — refusing duplicate send",
                "lead_id": lead_id,
                "outreach_id": record.id,
                "sent_at": record.sent_at.isoformat(),
            })
        if not lead.contact.email:
            return _json({
                "error": "Lead has no email address — cannot send",
                "lead_id": lead_id,
                "outreach_id": record.id,
            })

        sender = EmailSender(config, keys, db, dry_run=arguments.get("dry_run", False))
        if not sender.use_smtp and not sender.use_sendgrid:
            return _json({
                "error": "No email backend configured. Set SMTP_* or SENDGRID_API_KEY.",
                "lead_id": lead_id,
            })

        await sender.sync_sent_today()
        remaining = sender.remaining_sends_today()
        if remaining < 1:
            return _json({
                "error": "Daily send limit reached — refusing send",
                "daily_limit": config.outreach.daily_email_limit,
                "sent_today": sender._sent_today,
                "remaining": 0,
                "lead_id": lead_id,
            })

        try:
            ok = await sender.send_approved_record(lead, record)
        except DailyLimitReached:
            return _json({
                "error": "Daily send limit reached — refusing send",
                "daily_limit": config.outreach.daily_email_limit,
                "sent_today": sender._sent_today,
                "remaining": 0,
                "lead_id": lead_id,
            })

        if not ok:
            return _json({
                "error": "Send failed",
                "lead_id": lead_id,
                "outreach_id": record.id,
            })

        after = await db.get(lead_id)
        after_record = next(
            (r for r in after.outreach_history if r.id == record.id), record
        )

        return _json({
            "sent": True,
            "lead_id": lead_id,
            "outreach_id": record.id,
            "recipient": after.contact.email,
            "sent_at": (
                after_record.sent_at.isoformat() if after_record.sent_at else None
            ),
            "status": after.status.value,
            "remaining_today": sender.remaining_sends_today(),
        })

    elif name == "send_test_draft":
        from leadgen.outreach.email import EmailSender

        lead = await db.get(arguments["lead_id"])
        if not lead:
            return _json({"error": "Lead not found"})

        test_recipient = arguments["test_recipient"].strip()
        if not test_recipient:
            return _json({"error": "test_recipient is required"})

        outreach_id = arguments.get("outreach_id")
        if outreach_id:
            record = next(
                (r for r in lead.outreach_history if r.id == outreach_id), None
            )
            if not record:
                return _json({"error": f"No outreach record with id {outreach_id}"})
        elif lead.outreach_history:
            record = lead.outreach_history[-1]
        else:
            return _json({"error": "Lead has no draft outreach to preview"})

        email_before = lead.contact.email
        status_before = lead.status.value
        sent_at_before = record.sent_at

        sender = EmailSender(config, keys, db, dry_run=arguments.get("dry_run", False))
        ok = await sender.send_test_draft(
            subject=record.subject or "(no subject)",
            body=record.body,
            test_recipient=test_recipient,
            to_name=lead.display_name,
        )

        # Re-read lead to confirm the test send did not mutate persisted state.
        after = await db.get(lead.id)
        if after is None:
            return _json({"error": "Lead disappeared after test send"})

        after_record = next((r for r in after.outreach_history if r.id == record.id), None)

        return _json({
            "test_sent": ok,
            "test_recipient": test_recipient,
            "lead_id": lead.id,
            "outreach_id": record.id,
            "subject_sent": f"[TEST] {record.subject or '(no subject)'}",
            "lead_unchanged": (
                after.contact.email == email_before
                and after.status.value == status_before
                and after_record is not None
                and after_record.sent_at == sent_at_before
            ),
        })

    elif name == "update_lead":
        from leadgen.crm.update_lead import update_lead as apply_update_lead
        from leadgen.sources.hunter import HunterConnector

        lead_id = arguments["lead_id"]
        kwargs: dict = {}
        if "email" in arguments:
            kwargs["email"] = arguments["email"]
        if "domain" in arguments:
            kwargs["domain"] = arguments["domain"]
        if "phone" in arguments:
            kwargs["phone"] = arguments["phone"]
        if "first_name" in arguments:
            kwargs["first_name"] = arguments["first_name"]
        if "last_name" in arguments:
            kwargs["last_name"] = arguments["last_name"]
        if "full_name" in arguments:
            kwargs["full_name"] = arguments["full_name"]
        if "email_verified" in arguments:
            kwargs["email_verified"] = arguments["email_verified"]
        if "verification_source" in arguments:
            kwargs["verification_source"] = arguments["verification_source"]
        if "status" in arguments:
            kwargs["status"] = arguments["status"]
        if "note" in arguments:
            kwargs["note"] = arguments["note"]

        verify = arguments.get("verify", False)
        if verify:
            if not keys.hunter:
                return [TextContent(type="text", text=json.dumps({
                    "error": "HUNTER_API_KEY is not set. Provide it as an environment variable "
                    "(set it in Doppler for production, or a local .env for development).",
                    "lead_id": lead_id,
                }))]
            async with HunterConnector(config, keys) as hunter:
                result = await apply_update_lead(
                    db, lead_id, verify=True, hunter=hunter, **kwargs
                )
        else:
            result = await apply_update_lead(db, lead_id, **kwargs)

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "update_lead_status":
        from leadgen.crm.suppression import sync_suppression_from_lead

        lead = await db.get(arguments["lead_id"])
        if not lead:
            return [TextContent(type="text", text=json.dumps({"error": "Lead not found"}))]

        lead.status = LeadStatus(arguments["status"])
        if "notes" in arguments:
            lead.notes = (lead.notes + "\n" + arguments["notes"]).strip()
        lead.touch()
        await sync_suppression_from_lead(db, lead)
        await db.upsert(lead)
        return [TextContent(type="text", text=json.dumps({"updated": True, "status": lead.status.value}))]

    elif name == "get_lead_detail":
        lead = await db.get(arguments["lead_id"])
        if not lead:
            return _json({"error": "Lead not found"})

        return _json({
            "id": lead.id,
            "name": lead.display_name,
            "title": lead.contact.title,
            "email": lead.contact.email,
            "email_verified": lead.contact.email_verified,
            "company": lead.company.name,
            "industry": lead.company.industry,
            "employees": (
                lead.company.employee_count
                if not lead.company.employee_count_unknown
                else "unknown"
            ),
            "location": f"{lead.company.city}, {lead.company.state}",
            "score": lead.score.model_dump(mode="json") if lead.score else None,
            "status": lead.status.value,
            "outreach_count": len(lead.outreach_history),
            "outreach_history": [
                r.model_dump(mode="json") for r in lead.outreach_history
            ],
            "notes": lead.notes,
        })

    elif name == "suppress_lead":
        from leadgen.crm.suppression import add_operator_suppression

        result = await add_operator_suppression(
            db,
            lead_id=arguments.get("lead_id"),
            name=arguments.get("name"),
            company=arguments.get("company"),
            domain=arguments.get("domain"),
            reason=arguments.get("reason", "manual"),
        )
        return _json(result)

    elif name == "unsuppress_lead":
        from leadgen.crm.suppression import remove_operator_suppression

        result = await remove_operator_suppression(
            db,
            suppression_key=arguments.get("suppression_key"),
            lead_id=arguments.get("lead_id"),
            name=arguments.get("name"),
            company=arguments.get("company"),
            domain=arguments.get("domain"),
        )
        return _json(result)

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(
    scorer_cls: type[LeadScorer] | None = None,
    drafter_cls: type[OutreachDrafter] | None = None,
) -> None:
    """Start the MCP server.

    Optionally inject custom `LeadScorer` / `OutreachDrafter` subclasses for
    use by productized agents (e.g. `RexScorer`, `RexDrafter`). Defaults
    use the generic engine classes.
    """
    global SCORER_CLASS, DRAFTER_CLASS, config, keys, db
    if scorer_cls is not None:
        SCORER_CLASS = scorer_cls
        logger.info(f"MCP scorer class overridden: {scorer_cls.__module__}.{scorer_cls.__name__}")
    if drafter_cls is not None:
        DRAFTER_CLASS = drafter_cls
        logger.info(f"MCP drafter class overridden: {drafter_cls.__module__}.{drafter_cls.__name__}")

    # Load config/keys/db now that cwd is final. Doing this here (instead of
    # at module import) is what lets agentsia-core's AgentContext.activate()
    # chdir into agents/<agent>/ before the engine reads `config.yaml`.
    config = load_config()
    keys = load_api_keys()
    db = LeadDatabase(config.database.sqlite_path)

    logging.basicConfig(level=logging.INFO)
    agent_label = display_agent_name(config)
    logger.info("Starting LeadGen MCP server (agent=%s)...", agent_label)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
