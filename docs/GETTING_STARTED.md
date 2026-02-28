# Getting Started with LeadGen

This guide takes you from a fresh clone to your first scored leads 
and drafted outreach emails. Should take about 30 minutes.

---

## Prerequisites

- Python 3.11+
- `uv` package manager (recommended) — install at `https://docs.astral.sh/uv/`
- An Anthropic API key (free credits available at `https://console.anthropic.com`)
- An Apollo.io account (free tier is fine to start)

---

## Step 1 — Clone the Repo

```bash
git clone https://github.com/agentsia-ai/LeadGen.git
cd LeadGen
```

---

## Step 2 — Install Dependencies

LeadGen uses `uv` for fast, reliable dependency management.

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install all dependencies
uv sync

# Install LeadGen itself in editable mode
uv pip install -e .
```

After this, the `leadgen` command is available inside your virtual environment.

To activate the environment for the session:
```bash
source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows
```

Or prefix all commands with `uv run`:
```bash
uv run leadgen pipeline
```

---

## Step 3 — Get Your API Keys

**Anthropic (required):**
1. Go to `https://console.anthropic.com` → sign up
2. API Keys → Create Key
3. You get $5 in free credits — enough to score thousands of leads

**Apollo.io (required for lead sourcing):**
1. Go to `https://app.apollo.io` → sign up (no credit card for free tier)
2. Settings → Integrations → API → Create API Key

**Hunter.io (recommended):**
1. Go to `https://hunter.io` → sign up (free tier: 25/month)
2. Dashboard → API → copy your key

See `docs/API_KEYS.md` for full cost breakdowns and setup details.

---

## Step 4 — Configure Your Environment

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:
```
ANTHROPIC_API_KEY=sk-ant-...
APOLLO_API_KEY=your-apollo-key
```

For sending emails, also add your SMTP credentials (see `docs/API_KEYS.md`).

---

## Step 5 — Configure Your ICP

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and customize for your business. The most important sections:

```yaml
client_name: "Artificial Intelligentsia, LLC"
operator_name: "Your Name"
operator_title: "AI Solutions Consultant"
operator_email: "you@agentsia.ai"

icp:
  industries:
    - "restaurants and food service"
    - "retail"
    - "professional services"
  company_size:
    min_employees: 5
    max_employees: 200

value_prop:
  one_liner: "We help small businesses save 10+ hours/week by automating 
              repetitive tasks with AI — set up in days, not months."
```

Take time on `value_prop.one_liner` — it feeds into every AI-drafted email.
The more specific and benefit-focused, the better the drafts.

---

## Step 6 — Initialize the Database

```bash
leadgen pipeline
```

This creates your SQLite database and shows a (currently empty) pipeline summary.

---

## Step 7 — Fetch Your First Leads

```bash
leadgen search --limit 20
```

This pulls up to 20 leads from Apollo matching your ICP config and saves them 
to the database. You should see output like:

```
✓ Fetched 20 leads, 20 new added to database.
```

---

## Step 8 — Score the Leads

```bash
leadgen score
```

This sends each lead to Claude for ICP scoring. You'll see:

```
✓ Scored 20 leads. 12 passed threshold 0.60.
```

Leads scoring above your threshold (default 0.60) are marked SCORED and 
queued for outreach drafting.

---

## Step 9 — Check Your Pipeline

```bash
leadgen pipeline
```

You'll see a table like:
```
┌──────────────────────────────────┐
│        LeadGen Pipeline          │
├────────────────┬─────────────────┤
│ Status         │ Count           │
│ new            │ 0               │
│ scored         │ 12              │
│ queued         │ 0               │
├────────────────┼─────────────────┤
│ TOTAL          │ 12              │
└────────────────┴─────────────────┘
```

---

## Step 10 — Draft Outreach Emails

The review command walks you through scored leads and drafts emails interactively:

```bash
leadgen review
```

Or use the MCP server (see Step 11) to do this conversationally.

---

## Step 11 — Connect to Claude Desktop (Optional but Recommended)

If you use Claude Desktop, you can control your entire pipeline conversationally:

```bash
leadgen mcp
```

See `docs/MCP_SETUP.md` for the full Claude Desktop configuration.

---

## Common Issues

**`leadgen: command not found`**
→ Your virtual environment isn't activated. Run `source .venv/bin/activate` 
  or prefix commands with `uv run leadgen ...`

**`Config file not found: config.yaml`**
→ Run `cp config.example.yaml config.yaml` and fill in your details

**`ANTHROPIC_API_KEY is not set`**
→ Check your `.env` file exists and has the key set correctly

**`Apollo returned 0 leads`**
→ Your ICP geography or industry filters may be too narrow. 
  Try removing the `states` and `cities` filters and just keeping `countries: ["US"]`

**Scores are all very low (under 0.3)**
→ Review your `icp.industries` list — be specific and use plain English 
  terms that match how businesses describe themselves

---

## Next Steps

- Read `docs/ARCHITECTURE.md` for a deep dive on how everything works
- Read `docs/API_KEYS.md` for cost details and when to upgrade tiers
- Read `docs/CLIENT_GUIDE.md` when you're ready to deploy for a client
- Read `docs/MCP_SETUP.md` to connect to Claude Desktop
