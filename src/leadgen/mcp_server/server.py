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

from leadgen.config.loader import load_config, load_api_keys
from leadgen.crm.database import LeadDatabase
from leadgen.models import LeadStatus

logger = logging.getLogger(__name__)

# ── Server init ───────────────────────────────────────────────────────────────

app = Server("leadgen")
config = load_config()
keys = load_api_keys()
db = LeadDatabase(config.database.sqlite_path)


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
            description="Pull fresh leads from Apollo.io matching the configured ICP.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of leads to fetch (default 25)",
                    }
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
        limit = arguments.get("limit", 25)

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
        from leadgen.ai.scorer import LeadScorer
        limit = arguments.get("limit", 20)

        unscored = await db.list(status=LeadStatus.NEW, limit=limit)
        scorer = LeadScorer(config, keys)
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
        from leadgen.ai.drafter import OutreachDrafter
        drafter = OutreachDrafter(config, keys)

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

async def main():
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting LeadGen MCP server...")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
