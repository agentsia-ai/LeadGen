# Architecture — LeadGen / Legion

A deep dive into design decisions, trade-offs, and the overall system architecture.

---

## System Overview

LeadGen is structured as a pipeline with five distinct layers. Each layer has a 
single responsibility and communicates through the shared `Lead` data model.

```
┌──────────────────────────────────────────────────────────────────┐
│                         MCP SERVER                               │
│              (Claude Desktop ↔ LeadGen bridge)                   │
│    Tools: search, score, draft, approve, pipeline, status        │
└───────────────────────────┬──────────────────────────────────────┘
                            │ calls into all layers below
                            │
┌───────────┐  ┌────────────┴──────┐  ┌──────────────┐  ┌────────┐
│  SOURCES  │  │    ENRICHMENT     │  │   OUTREACH   │  │  CRM   │
│           │  │                   │  │              │  │        │
│ Apollo    │  │ Clearbit          │  │ Email SMTP   │  │SQLite  │
│ Hunter    │→ │ ICP scoring       │→ │ SendGrid     │→ │Supabase│
│ Crawler   │  │ Deduplication     │  │ Drafting     │  │        │
│ CSV       │  │                   │  │ Sequences    │  │        │
└───────────┘  └───────────────────┘  └──────────────┘  └────────┘
                            │
                     ┌──────┴──────┐
                     │  AI LAYER   │
                     │             │
                     │ Claude API  │
                     │ Scoring     │
                     │ Drafting    │
                     └─────────────┘
```

---

## The Lead Model

`src/models.py` is the backbone of the entire system. Every layer reads and 
writes `Lead` objects — nothing passes raw dicts between layers.

```python
Lead
├── ContactInfo      # person details
├── CompanyInfo      # company details  
├── ScoringBreakdown # AI score + reasoning
├── OutreachRecord[] # full history of messages
└── LeadStatus       # where in the pipeline
```

### Status Machine

```
NEW → ENRICHED → SCORED → QUEUED → CONTACTED → FOLLOWING_UP
                                              → RESPONDED → MEETING_BOOKED
                                                          → CLOSED_WON
                                                          → CLOSED_LOST
                                              → BOUNCED
                                              → UNSUBSCRIBED
```

ENRICHED is optional — leads can go NEW → SCORED if enrichment is disabled.

---

## Configuration Architecture

The config system has two layers intentionally kept separate:

**`config.yaml`** — business logic, safe to share with clients
- ICP definition
- Outreach tone and limits
- Source preferences
- Scoring weights

**`.env`** — secrets, never in version control
- API keys
- SMTP credentials
- Database URLs

This separation means you can commit a `clients/acme.yaml` to a private repo 
without ever exposing credentials. The `LeadGenConfig` Pydantic model validates 
config on load and provides clear error messages for missing fields.

### Client Deployment Pattern

Each client gets:
```
clients/
  acme-corp.yaml          # their ICP config
  acme-corp.env           # their API keys (in private repo)
  data/acme-corp.db       # their lead database
```

Run for a specific client:
```bash
CONFIG_PATH=clients/acme-corp.yaml leadgen pipeline
```

---

## Sources Layer

### Apollo.io (primary)

Apollo is the main lead source. Their People Search API accepts filters for:
- Industry keywords
- Employee count ranges
- Geographic location
- Person title keywords

LeadGen translates your `config.yaml` ICP directly into Apollo search params.
Apollo's free tier gives ~50 contacts/month; paid tiers start at ~$49/month
for meaningful volume.

**Pagination:** Apollo returns max 25 results per page. LeadGen loops pages 
automatically until `limit` is reached.

**Deduplication:** Handled at the DB layer by unique index on `contact_email`.
New pulls won't create duplicates for leads already in your database.

### Hunter.io (email verification)

Hunter serves two purposes:
1. **Find emails** for contacts where Apollo didn't provide one
2. **Verify emails** to reduce bounce rate before sending

Hunter's free tier gives 25 searches/month. For meaningful use, the Starter 
plan (~$49/month) gives 500 searches/month.

### Web Crawler (Playwright)

The crawler is for sources that don't have APIs — local business directories,
industry association member lists, LinkedIn (carefully), etc.

Uses Playwright for JavaScript-heavy sites. This layer requires the most 
maintenance as sites change their structure.

**Important:** Always check a site's `robots.txt` and Terms of Service before 
crawling. Many sites prohibit scraping.

---

## AI Layer

### Lead Scoring

The scorer sends each lead to Claude with:
1. Your full ICP definition (industries, size, geo, pain points)
2. Scoring dimension weights from config
3. All available lead data (company, contact, technologies, description)

Claude returns a structured JSON score with per-dimension scores and a 
reasoning string. The reasoning is stored on the lead and surfaced in the 
MCP pipeline view — this is what lets you understand *why* a lead scored well.

**Scoring dimensions:**
- `industry_match` — does their industry match your targets?
- `company_size_match` — are they in your sweet spot?
- `geography_match` — are they where you serve?
- `pain_point_signals` — do their description/technologies signal your pain points?
- `contact_quality` — is this a decision-maker with a verified email?

Weights are fully configurable in `config.yaml`. Typical tuning:
- Raise `pain_point_signals` if you're getting lots of right-size but wrong-need leads
- Raise `contact_quality` if you're getting lots of leads with no email
- Raise `industry_match` if off-industry leads keep slipping through

### Outreach Drafting

The drafter sends each lead to Claude with:
1. Operator identity and value proposition
2. Full lead context (company, title, industry, technologies, score reasoning)
3. Tone instruction
4. Email type (initial vs. follow-up N)

Claude returns `{"subject": "...", "body": "..."}` which gets wrapped in an 
`OutreachRecord` and attached to the lead's `outreach_history`.

**Quality levers:**
- The single biggest improvement is a specific, concrete `value_prop.one_liner`
- Including `score.reasoning` in the draft prompt helps Claude personalize 
  to the specific reason this lead scored well
- Follow-up prompts include the previous email body so Claude can reference it naturally

---

## CRM / Database Layer

### SQLite (default)

SQLite is the right default for a solo operator or small agency:
- Zero infrastructure, file-based
- Fast enough for tens of thousands of leads
- Trivial backup (just copy the `.db` file)
- Works offline

Schema is a single `leads` table with JSON columns for nested objects 
(contact, company, score, outreach history) plus denormalized scalar columns 
for fast filtering (`score_total`, `contact_email`, `company_name`).

### Supabase (cloud option)

Set `DATABASE_URL` in `.env` to a Supabase connection string to switch to 
PostgreSQL-backed cloud storage. This enables:
- Multi-user access (you + client both seeing the same pipeline)
- Web dashboard potential
- Better concurrent access

The database abstraction layer in `src/crm/database.py` is designed to make 
this switch straightforward — the interface doesn't change.

---

## MCP Server

The MCP server is what makes LeadGen conversational. It runs as a subprocess 
that Claude Desktop communicates with over stdio.

### Why MCP over a Web UI?

For a solo operator, the MCP pattern is dramatically faster to build and 
iterate than a full web dashboard. You get:
- Natural language queries instead of form filters
- Claude reasoning about which tools to use in sequence
- No frontend code to maintain
- Works inside your existing Claude Desktop workflow

A web UI is on the roadmap for Phase 4 (productization), but MCP is the 
right MVP interface.

### Tool Design Philosophy

Each MCP tool does one thing. Tools compose. Rather than a single 
`run_full_pipeline` tool, we have `fetch_new_leads` + `score_leads` + 
`draft_outreach` + `approve_outreach`. This lets Claude (and you) mix and 
match — "fetch 10 leads from Apollo and score them but don't draft yet."

---

## Outreach Layer

### Email Sending

Supports two backends:
- **SMTP** — use your own Gmail/Outlook/etc. Best for low volume, most 
  deliverable since it comes from your real inbox
- **SendGrid** — better for higher volume, includes open/click tracking

**Deliverability rules baked in:**
- Daily send limit (configurable, default 30) to avoid spam flags
- Minimum gap between emails to same domain
- Unsubscribe handling marks lead as `UNSUBSCRIBED`
- Bounce handling marks lead as `BOUNCED`

### Sequence Management

Follow-up timing comes from `outreach.follow_up_days` in config (default: 
3, 7, 14 days after initial). The scheduler checks daily for leads that are 
due for follow-up and queues draft generation.

`require_approval: true` (default) means every draft — initial and follow-up —
sits in QUEUED status until you explicitly approve it via MCP or CLI.

---

## Security Considerations

- API keys are environment variables only, never in config files
- `.env` and `config.yaml` are both in `.gitignore`
- The MCP server runs locally over stdio — no network exposure
- Email credentials use app passwords (not account passwords) where possible
- No lead data is sent to any third party except the configured AI/enrichment APIs
- SQLite database file should be excluded from cloud sync (Dropbox, iCloud) 
  to avoid accidental exposure of contact data — add `data/` to those exclusion lists too

---

## Scaling Considerations

LeadGen is designed for solo operators and small agencies — think 10-500 leads 
per day, one operator reviewing and approving. If you need to scale beyond that:

- Switch from SQLite to Supabase/PostgreSQL
- Add a job queue (Celery + Redis) instead of direct async calls
- Move the MCP server to a persistent process rather than on-demand
- Add rate limit backoff tuning per API
- Consider a React dashboard for client self-service (Phase 4 roadmap)
