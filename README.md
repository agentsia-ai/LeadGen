# LeadGen (a.k.a. Legion) ⚡

> *LeadGen* — Lead Generation engine. Say it fast: **Legion**.  
> An AI-powered, MCP-native lead generation platform built for the modern solo operator and small business — and designed from day one to be white-labeled and deployed for clients.

---

## Vision

Most lead generation tools are either too expensive (ZoomInfo, Apollo enterprise), too manual (spreadsheets and cold DMs), or too generic (spray-and-pray email blasts).

**LeadGen** is different. It's:

- **AI-first** — Claude scores leads, drafts personalized outreach, and learns your ICP
- **MCP-native** — runs as an MCP server so you can query and control it conversationally via Claude Desktop or any MCP client
- **Fully configurable** — swap out the target industry, ICP, messaging, and tone per client deployment
- **Modular** — use the whole stack or just the pieces you need
- **Yours to white-label** — the architecture is designed so each client gets their own configured instance

The goal: you spend 20 minutes a day reviewing AI-qualified leads and approving outreach drafts. LeadGen does the rest.

---

## Core Concepts

### The Five Layers

```
┌─────────────────────────────────────────────────────┐
│                   MCP SERVER LAYER                  │
│         (talk to LeadGen via Claude Desktop)         │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────┐  ┌────────┴───────┐  ┌────────────────┐
│  SOURCES   │  │   ENRICHMENT   │  │    OUTREACH    │
│            │  │                │  │                │
│ Apollo API │  │ Clearbit/etc   │  │ Email (SMTP)   │
│ Hunter.io  │  │ Claude scoring │  │ LinkedIn msg   │
│ Web crawl  │  │ ICP matching   │  │ Draft approval │
│ Manual CSV │  │ Contact finder │  │ Sequences      │
└────────────┘  └────────────────┘  └────────────────┘
                         │
              ┌──────────┴──────────┐
              │        CRM          │
              │  Pipeline / Status  │
              │  Follow-up queue    │
              │  Response tracking  │
              └─────────────────────┘
```

### Client Config Philosophy

Every client deployment is just a **config file**. The engine is the same. You change:

```yaml
# client_config.yaml
client_name: "Acme Corp"
icp:
  industries: ["retail", "e-commerce"]
  company_size: "10-200 employees"
  geography: ["US", "Canada"]
  pain_points: ["inventory management", "customer retention"]
outreach:
  sender_name: "Jane Smith"
  sender_title: "AI Solutions Consultant"
  tone: "friendly-professional"
  value_prop: "We help retail businesses automate inventory forecasting using AI"
sources:
  apollo: true
  hunter: true
  web_crawl: false
```

That's it. One config, one client.

---

## Architecture

```
LeadGen/
├── src/
│   ├── sources/          # Lead acquisition connectors
│   │   ├── apollo.py     # Apollo.io API connector
│   │   ├── hunter.py     # Hunter.io email finder
│   │   ├── crawler.py    # Playwright-based web crawler
│   │   └── csv_import.py # Manual CSV import
│   │
│   ├── enrichment/       # Making leads smarter
│   │   ├── scorer.py     # Claude-powered ICP scoring
│   │   ├── enricher.py   # Company/contact enrichment
│   │   └── deduper.py    # Deduplication logic
│   │
│   ├── outreach/         # Taking action
│   │   ├── email.py      # SMTP / SendGrid email sender
│   │   ├── drafter.py    # Claude-powered message drafting
│   │   ├── sequences.py  # Multi-step follow-up sequences
│   │   └── linkedin.py   # LinkedIn outreach (API-based)
│   │
│   ├── crm/              # Tracking everything
│   │   ├── database.py   # SQLite/Supabase abstraction
│   │   ├── pipeline.py   # Lead status management
│   │   └── scheduler.py  # Follow-up scheduling
│   │
│   ├── mcp_server/       # MCP interface (the magic layer)
│   │   ├── server.py     # MCP server definition
│   │   ├── tools.py      # MCP tool definitions
│   │   └── prompts.py    # MCP prompt templates
│   │
│   ├── ai/               # Claude integration
│   │   ├── client.py     # Anthropic API client wrapper
│   │   ├── scorer.py     # Lead scoring prompts
│   │   ├── drafter.py    # Outreach drafting prompts
│   │   └── analyzer.py   # Campaign analysis
│   │
│   └── config/           # Configuration management
│       ├── loader.py     # Config file loader
│       ├── validator.py  # Config validation
│       └── schema.py     # Config schema definitions
│
├── tests/
│   ├── unit/             # Unit tests per module
│   └── integration/      # End-to-end flow tests
│
├── docs/
│   ├── ARCHITECTURE.md   # Deep dive on design decisions
│   ├── MCP_SETUP.md      # How to connect to Claude Desktop
│   ├── CLIENT_GUIDE.md   # How to deploy for a new client
│   └── API_KEYS.md       # Which APIs you need and how to get them
│
├── examples/
│   ├── configs/          # Example client config files
│   └── notebooks/        # Jupyter exploration notebooks
│
├── scripts/
│   ├── setup.sh          # One-command environment setup
│   └── new_client.sh     # Scaffold a new client config
│
├── config.example.yaml   # Template config (copy → config.yaml)
├── pyproject.toml        # Project metadata and dependencies
├── .env.example          # Required environment variables
├── .gitignore
└── LICENSE               # AGPL-3.0
```

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/yourusername/LeadGen.git
cd LeadGen

# 2. Install
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env          # add your API keys
cp config.example.yaml config.yaml  # customize your ICP

# 4. Run your first lead search
python -m leadgen search --limit 50

# 5. Review & approve outreach drafts
python -m leadgen review

# 6. Start the MCP server (connect to Claude Desktop)
python -m leadgen mcp
```

---

## MCP Integration

LeadGen exposes itself as an MCP server, meaning you can control it directly from Claude Desktop:

```
"Hey Claude, find me 20 leads in the restaurant industry 
 in Chicago with 10-50 employees and draft intro emails for the top 10."
```

Claude will use LeadGen's MCP tools to:
1. Query your lead database
2. Pull new leads from Apollo matching the criteria
3. Score them against your ICP
4. Draft personalized emails
5. Queue them for your approval

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `search_leads` | Find leads matching criteria |
| `score_lead` | AI-score a lead against your ICP |
| `draft_outreach` | Generate personalized outreach for a lead |
| `get_pipeline` | View current lead pipeline status |
| `approve_outreach` | Mark drafted messages as approved to send |
| `get_campaign_stats` | View outreach performance metrics |
| `add_lead_manually` | Add a lead you found yourself |
| `update_lead_status` | Move a lead through the pipeline |

---

## Productization / White-Label

LeadGen is designed to be sold and deployed as a service. The model:

1. **Setup fee** — you configure a client instance (ICP, messaging, integrations)
2. **Monthly retainer** — you run/maintain it, they get leads
3. **Or license the code** — they run it themselves (white-labeled)

Each client gets:
- Their own `config.yaml`
- Their own database (SQLite file or Supabase project)
- Their own API keys in `.env`
- Optionally: their own branded UI (future)

---

## Roadmap

### Phase 1 — Core Engine (MVP)
- [x] Project scaffold & architecture
- [ ] Apollo.io connector
- [x] Hunter.io connector
- [x] Claude lead scoring
- [x] SQLite CRM
- [x] Email outreach (SMTP)
- [x] CLI interface

### Phase 2 — MCP Layer
- [x] MCP server implementation
- [x] Claude Desktop integration
- [ ] Interactive `leadgen review` CLI command

### Phase 3 — Intelligence
- [ ] Multi-step outreach sequences
- [ ] Response tracking & classification
- [ ] Campaign performance analytics
- [ ] A/B testing for messaging

### Phase 4 — Productization
- [ ] Web UI (React dashboard)
- [ ] Client onboarding flow
- [ ] White-label packaging
- [ ] Supabase cloud backend option

---

## Required API Keys

| Service | Purpose | Free Tier? |
|---------|---------|-----------|
| Anthropic | Lead scoring, message drafting | Pay-per-use |
| Apollo.io | Lead sourcing | Yes (limited) |
| Hunter.io | Email finding/verification | Yes (25/mo) |
| SendGrid (or SMTP) | Email sending | Yes (100/day) |
| Clearbit | Company enrichment | Limited free |

---

## Philosophy

> Build it for yourself first. If it works for your business, it'll work for theirs.

LeadGen started as a tool to help one AI consulting LLC find and reach small business clients. Every feature was dogfooded before being generalized. That keeps the product honest and practical.

---

## License

AGPL-3.0 — free to use, modify, and distribute. If you run a modified version as a network service, you must open-source your modifications under the same license. See [LICENSE](LICENSE) for full terms.

---

*Built with Claude. Powered by LeadGen.*
