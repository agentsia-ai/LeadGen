# Deploying LeadGen for a New Client

This guide walks through setting up a new LeadGen instance for a client.
Each client gets their own isolated config, database, and API keys.

## Estimated Setup Time: 30–60 minutes

---

## Step 1: Gather Client Info

Before you touch any code, have this ready:

- [ ] Their target industries (be specific)
- [ ] Their ideal company size range
- [ ] Their geographic focus (national, regional, specific cities?)
- [ ] The pain points their product/service solves
- [ ] Their sender email and name for outreach
- [ ] Their value proposition in 1–2 sentences
- [ ] 2–3 proof points or differentiators

---

## Step 2: Create Client Config

```bash
# From the LeadGen root directory
cp config.example.yaml clients/client-name.yaml
```

Edit `clients/client-name.yaml` with the info from Step 1.

Key things to customize:
- `icp.industries` — be specific, Apollo uses these as keyword filters
- `icp.company_size` — narrow this to where they actually win deals
- `value_prop.one_liner` — this feeds into every AI-drafted email
- `outreach.tone` — match their brand voice
- `outreach.require_approval` — always `true` for new clients until you trust the output

---

## Step 3: Set Up API Keys

Each client needs their own set of API keys (or you manage shared keys per your agreement):

```bash
cp .env.example clients/client-name.env
```

At minimum you need:
- `ANTHROPIC_API_KEY` — for scoring and drafting
- `APOLLO_API_KEY` — for lead sourcing
- `SMTP_*` or `SENDGRID_API_KEY` — for sending emails (use client's email for best deliverability)

---

## Step 4: Initialize the Database

```bash
CONFIG_PATH=clients/client-name.yaml leadgen pipeline
```

This creates their database at the path specified in their config.

---

## Step 5: Run a Test Search

```bash
CONFIG_PATH=clients/client-name.yaml leadgen search --limit 10
CONFIG_PATH=clients/client-name.yaml leadgen score
CONFIG_PATH=clients/client-name.yaml leadgen pipeline
```

Review the scored leads. If scores are all low, the ICP config may be too narrow or Apollo's data
for their target industry is sparse.

---

## Step 6: Review First Outreach Drafts

```bash
CONFIG_PATH=clients/client-name.yaml leadgen review
```

Read through 5–10 drafts carefully. Common issues to fix in config:
- Emails too long → shorten `value_prop.one_liner`
- Tone feels off → adjust `outreach.tone` or add to value prop copy
- Missing personalization → enrich company descriptions via Clearbit

---

## Step 7: Handoff / Ongoing Operations

**If you're running it for them (retainer model):**
- Schedule `leadgen search` + `leadgen score` to run daily (cron or Task Scheduler)
- Review and approve drafts weekly
- Send them a weekly pipeline report

**If you're handing it off to them:**
- Walk them through the Claude Desktop MCP setup
- Show them how to use `get_pipeline` and `draft_outreach` conversationally
- Leave them a short Loom video walkthrough

---

## Pricing Model Suggestions

| Tier | What's included | Suggested Price |
|------|----------------|-----------------|
| Setup only | Config + first 50 leads | $500–1,500 one-time |
| Managed monthly | Setup + 200 leads/mo + outreach drafts | $500–1,000/mo |
| Full service | Everything + sending + reporting | $1,000–2,500/mo |
| License | They run it, you configured it | $200–500/mo SaaS |
