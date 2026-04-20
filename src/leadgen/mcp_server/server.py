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

from leadgen.ai.drafter import OutreachDrafter
from leadgen.ai.scorer import LeadScorer
from leadgen.config.loader import load_config, load_api_keys
from leadgen.crm.database import LeadDatabase
from leadgen.models import LeadStatus

logger = logging.getLogger(__name__)

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
                },
            },
        ),
        Tool(
            name="score_leads",
            description="Run AI scoring on unscored leads in the database.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max leads to score in this batch (default 20)",
                    }
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
            name="update_lead_status",
            description="Update the status of a lead in the pipeline.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "status": {"type": "string", "description": "New status value"},
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
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await db.init()

    if name == "get_pipeline":
        counts = await db.count_by_status()
        top_leads = await db.list(min_score=0.7, limit=5)

        summary = {
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
                return [TextContent(type="text", text=json.dumps({"error": "HUNTER_API_KEY is not set. Add it to .env"}))]
            async with HunterConnector(config, keys) as hunter:
                leads = await hunter.domain_search(domain=domain, limit=limit)
        elif use_pdl:
            if not keys.pdl:
                return [TextContent(type="text", text=json.dumps({"error": "PDL_API_KEY is not set. Add it to .env"}))]
            async with PDLConnector(config, keys) as pdl:
                leads = await pdl.search(limit=limit)
        else:
            if not keys.apollo:
                return [TextContent(type="text", text=json.dumps({"error": "APOLLO_API_KEY is not set. Add it to .env"}))]
            async with ApolloConnector(config, keys) as apollo:
                leads = await apollo.search(limit=limit)

        added = 0
        for lead in leads:
            is_new = await db.upsert(lead)
            if is_new:
                added += 1

        return [TextContent(
            type="text",
            text=json.dumps({"fetched": len(leads), "new_leads_added": added})
        )]

    elif name == "score_leads":
        limit = arguments.get("limit", 20)

        unscored = await db.list(status=LeadStatus.NEW, limit=limit)
        scorer = SCORER_CLASS(config, keys)
        scored = await scorer.score_batch(unscored)

        for lead in unscored:
            lead.status = LeadStatus.SCORED
            await db.upsert(lead)

        return [TextContent(type="text", text=json.dumps({
            "leads_scored": len(unscored),
            "passed_threshold": len(scored),
            "threshold": config.scoring.threshold,
        }))]

    elif name == "draft_outreach":
        drafter = DRAFTER_CLASS(config, keys)

        if "lead_id" in arguments:
            lead = await db.get(arguments["lead_id"])
            leads = [lead] if lead else []
        else:
            leads = await db.list(status=LeadStatus.SCORED, limit=arguments.get("limit", 5))

        drafted = []
        for lead in leads:
            record = await drafter.draft_initial(lead)
            lead.outreach_history.append(record)
            lead.status = LeadStatus.QUEUED
            lead.touch()
            await db.upsert(lead)
            drafted.append({
                "lead_id": lead.id,
                "name": lead.display_name,
                "company": lead.company.name,
                "subject": record.subject,
                "body_preview": record.body[:200] + "...",
            })

        return [TextContent(type="text", text=json.dumps(drafted, indent=2))]

    elif name == "approve_outreach":
        from datetime import datetime
        lead = await db.get(arguments["lead_id"])
        if not lead:
            return [TextContent(type="text", text=json.dumps({"error": "Lead not found"}))]

        pending = [r for r in lead.outreach_history if r.approved_at is None]
        for record in pending:
            record.approved_at = datetime.utcnow()

        lead.touch()
        await db.upsert(lead)
        return [TextContent(type="text", text=json.dumps({
            "approved": len(pending),
            "lead": lead.display_name,
        }))]

    elif name == "update_lead_status":
        lead = await db.get(arguments["lead_id"])
        if not lead:
            return [TextContent(type="text", text=json.dumps({"error": "Lead not found"}))]

        lead.status = LeadStatus(arguments["status"])
        if "notes" in arguments:
            lead.notes = (lead.notes + "\n" + arguments["notes"]).strip()
        lead.touch()
        await db.upsert(lead)
        return [TextContent(type="text", text=json.dumps({"updated": True, "status": lead.status.value}))]

    elif name == "get_lead_detail":
        lead = await db.get(arguments["lead_id"])
        if not lead:
            return [TextContent(type="text", text=json.dumps({"error": "Lead not found"}))]

        return [TextContent(type="text", text=json.dumps({
            "id": lead.id,
            "name": lead.display_name,
            "title": lead.contact.title,
            "email": lead.contact.email,
            "email_verified": lead.contact.email_verified,
            "company": lead.company.name,
            "industry": lead.company.industry,
            "employees": lead.company.employee_count,
            "location": f"{lead.company.city}, {lead.company.state}",
            "score": lead.score.model_dump() if lead.score else None,
            "status": lead.status.value,
            "outreach_count": len(lead.outreach_history),
            "notes": lead.notes,
        }, indent=2))]

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
    logger.info("Starting LeadGen MCP server...")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
