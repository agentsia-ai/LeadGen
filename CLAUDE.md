# CLAUDE.md — LeadGen

This file provides context and instructions for Claude (or any AI assistant) 
working in this codebase. Read this before making any changes.

---

## What This Project Is

LeadGen is a generic, AGPL-licensed AI-powered lead generation engine. It is
designed to be the open-source core that any operator (solo consultant,
agency, or in-house team) can configure and deploy. It is designed to:

1. Source leads from APIs (Apollo.io, Hunter.io, People Data Labs) and web crawling
2. Enrich and score leads against a configurable Ideal Customer Profile (ICP) using Claude
3. Draft personalized outreach emails using Claude
4. Track leads through a pipeline in a local SQLite (or cloud Supabase) database
5. Expose all functionality as an MCP server for conversational control via Claude Desktop
6. Support productized deployments via subclassing or config-based prompt overrides
   (see "Customization Patterns" below)

This repository is intentionally **identity-free**. Anything specific to a
particular operator, brand, named agent, ICP, or value proposition belongs in
a downstream private repository or a local `config.yaml` — never in this codebase.

---

## Architecture Overview

```
src/
├── models.py           # Core Lead data model — source of truth for all data shapes
├── config/
│   └── loader.py       # Pydantic config loader; reads config.yaml + .env
├── sources/
│   ├── apollo.py       # Apollo.io People Search API connector
│   ├── hunter.py       # Hunter.io email finder/verifier
│   ├── crawler.py      # Playwright browser automation crawler
│   └── csv_import.py   # Manual CSV import
├── enrichment/
│   └── enricher.py     # Company/contact enrichment (Clearbit, etc.)
├── ai/
│   ├── scorer.py       # Claude-powered ICP lead scoring
│   └── drafter.py      # Claude-powered outreach email drafting
├── outreach/
│   └── email.py        # SMTP/SendGrid email sender
├── crm/
│   └── database.py     # Async SQLite CRM layer
├── mcp_server/
│   └── server.py       # MCP server exposing all tools to Claude Desktop
└── cli.py              # Click CLI entry point
```

### Data Flow

```
Sources → models.Lead (status: NEW)
       → AI Scorer → (status: SCORED, score attached)
       → AI Drafter → (status: QUEUED, outreach_history populated)
       → Human approval → (status: QUEUED, approved_at set)
       → Email sender → (status: CONTACTED, sent_at set)
       → Response tracking → (status: RESPONDED / CLOSED_*)
```

---

## Key Design Principles

### 1. Config-driven, not code-driven
Everything client-specific lives in `config.yaml` and `.env`. The engine code 
should never contain hardcoded ICP details, company names, or messaging. 
When adding features, ask: "should this be configurable?" If yes, add it to 
the config schema in `src/config/loader.py` first.

### 2. The Lead model is the contract
`src/models.py` defines `Lead` and all sub-models. Every layer (sources, 
enrichment, outreach, CRM) must speak in `Lead` objects. Never pass raw dicts 
between layers. If you need a new field, add it to the model first.

### 3. Async everywhere
All I/O is async (httpx, aiosqlite, anthropic async client). Use `async/await` 
consistently. Don't introduce synchronous blocking calls in the hot path.

### 4. MCP tools must be self-describing
The MCP server is the primary interface for the operator. Tool names and 
descriptions must be clear enough that Claude can reason about when to use them 
without additional context. Keep tool schemas tight — prefer fewer, well-named 
parameters over many optional ones.

### 5. Require approval by default
The `outreach.require_approval` config flag defaults to `true`. Never send 
emails autonomously unless this is explicitly disabled. When in doubt, draft 
and queue, don't send.

### 6. White-label ready / identity-free
The engine code must not contain any operator-specific identity, brand name,
named agent persona, ICP details, or value proposition. All identity flows in
through `config.yaml` at runtime, or through a downstream subclass that
overrides the base classes (see "Customization Patterns" below). The only
exception is the LICENSE copyright line, which is required by AGPL-3.0.

If you find yourself wanting to bake "Agent X says..." or "Company Y targets..."
into a prompt or default value here, stop — that belongs in a downstream
private repo, not in this engine.

---

## Working With This Codebase

### Running locally
```bash
# Install dependencies
uv sync

# Set up config
cp .env.example .env          # fill in API keys
cp config.example.yaml config.yaml  # customize ICP

# Initialize database
leadgen pipeline

# Fetch and score leads
leadgen search --limit 20
leadgen score
leadgen pipeline

# Start MCP server
leadgen mcp
```

### Adding a new lead source
1. Create `src/sources/yourservice.py`
2. Implement a class with an async `search(limit) -> list[Lead]` method
3. Map the API response to `Lead` / `ContactInfo` / `CompanyInfo` models
4. Add config fields to `SourcesConfig` in `src/config/loader.py`
5. Wire it into the CLI `search` command in `src/cli.py`
6. Add an MCP tool or extend `fetch_new_leads` in `src/mcp_server/server.py`
7. Add `YOUR_SERVICE_API_KEY` to `.env.example`

### Adding a new MCP tool
1. Add the `Tool` definition in the `list_tools()` handler in `src/mcp_server/server.py`
2. Add the handler branch in `call_tool()`
3. Update `docs/MCP_SETUP.md` with the new tool in the tools table
4. Keep tool names snake_case and descriptions action-oriented

### Modifying the Lead model
- Add new fields with sensible defaults so existing DB rows don't break
- Update `_row_to_lead()` in `src/crm/database.py` if adding persisted fields
- Consider whether the field needs a denormalized column for fast filtering

---

## Claude API Usage in This Project

This project uses Claude for two things:

### 1. Lead Scoring (`src/leadgen/ai/scorer.py`)
- Default model: `claude-sonnet-4-20250514` (override via `ai.model` in config)
- Returns structured JSON with per-dimension scores and reasoning
- Default prompt lives on the `LeadScorer.SYSTEM_PROMPT` class attribute
- Runs in batches of 5 to manage rate limits

### 2. Outreach Drafting (`src/leadgen/ai/drafter.py`)
- Default model: `claude-sonnet-4-20250514` (override via `ai.model` in config)
- Returns JSON `{"subject": "...", "body": "..."}`
- Default prompts live on `OutreachDrafter.INITIAL_SYSTEM_PROMPT` and
  `OutreachDrafter.FOLLOWUP_SYSTEM_PROMPT` class attributes
- Tone and value prop are injected from config at runtime

### Prompt tuning tips
- Scoring quality improves significantly when `icp.pain_points` are specific
- Drafting quality improves when `value_prop.one_liner` is concrete and benefit-focused
- If scores feel off, add examples to the scoring prompt as few-shot demonstrations
- Keep `max_tokens` tight — 500 for scoring, 600 for drafts, 400 for follow-ups

---

## Customization Patterns

There are two supported ways to customize prompt behavior without modifying
this engine:

### Pattern A — Config-based prompt override (no code)
Point at external prompt files in your `config.yaml`:

```yaml
ai:
  model: "claude-sonnet-4-20250514"
  scorer_prompt_path: "./prompts/scorer.txt"
  drafter_prompt_path: "./prompts/drafter.txt"
  followup_prompt_path: "./prompts/followup.txt"
```

The base `LeadScorer` / `OutreachDrafter` will read these files at
construction time and use them as the system prompt.

### Pattern B — Subclassing (for productized agents)
For named agents with personas (e.g. a downstream private repo defining
"Rex" or "ARIA"), subclass and override:

```python
from leadgen.ai.scorer import LeadScorer
from leadgen.ai.drafter import OutreachDrafter

class RexScorer(LeadScorer):
    SYSTEM_PROMPT = "You are Rex, a meticulous lead qualification analyst..."

class RexDrafter(OutreachDrafter):
    INITIAL_SYSTEM_PROMPT = "You are Rex's drafting voice..."
    FOLLOWUP_SYSTEM_PROMPT = "..."
```

Both patterns can be combined (subclass for code-level customization, then
override per-deployment via config). This three-tier model (engine → named
agent → per-client config) is the canonical productization shape.

---

## Environment Variables

See `.env.example` for the full list. Required to run anything:
- `ANTHROPIC_API_KEY` — scoring and drafting
- `APOLLO_API_KEY` — lead sourcing (free tier available)

Optional but recommended:
- `HUNTER_API_KEY` — email verification
- `SMTP_*` or `SENDGRID_API_KEY` — sending emails

Never commit `.env` or `config.yaml` to git. Both are in `.gitignore`.

---

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src

# Run a specific test file
uv run pytest tests/unit/test_scorer.py
```

Tests use `pytest-asyncio` for async test support. Mock external API calls 
with `unittest.mock.AsyncMock` — never make real API calls in tests.

---

## Common Gotchas

- **Apollo pagination:** Free tier caps at 25 results/page and ~50 requests/hour
- **Email deduplication:** The DB deduplicates by `contact.email` — leads without emails can duplicate
- **SQLite concurrency:** Fine for single-user local use; switch to Supabase for multi-user/cloud
- **MCP server must use stdio transport:** Don't change to HTTP transport without updating Claude Desktop config
- **Config reload:** Config is loaded once at startup; restart the MCP server after changing `config.yaml`

---

## Project Status

See `README.md` for the full roadmap. Current phase: **Phase 1 MVP**.

Stubs that still need implementation:
- `src/enrichment/enricher.py` — Clearbit enrichment
- `src/sources/crawler.py` — Playwright web crawler
- `src/sources/csv_import.py` — CSV import with folder watching
- Interactive `leadgen review` CLI command
- Response tracking / email open detection
