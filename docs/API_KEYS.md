# API Keys — What You Need and How to Get Them

This guide covers every external API LeadGen uses, with honest notes on 
free tiers, costs, and what's truly required vs. optional.

---

## Required to Run Anything

### Anthropic (Claude API)
**Used for:** Lead scoring, outreach drafting — the core intelligence of LeadGen

**Cost:** Pay-per-use, no subscription required
- Claude Sonnet: ~$3 per million input tokens, ~$15 per million output tokens
- Scoring a lead: ~500 tokens → roughly $0.002 per lead scored
- Drafting an email: ~800 tokens → roughly $0.004 per draft
- **Real-world estimate:** 100 leads scored + 50 emails drafted ≈ **$0.40**
- Don't stress the cost — it's genuinely cheap for what you get

**How to get it:**
1. Go to `https://console.anthropic.com`
2. Create an account
3. Go to API Keys → Create Key
4. Add to `.env` as `ANTHROPIC_API_KEY=sk-ant-...`

**Free tier:** $5 in free credits when you first sign up (enough to score 
thousands of leads and draft hundreds of emails)

---

## Required for Lead Sourcing

### Apollo.io
**Used for:** Primary lead source — searches for contacts matching your ICP

**Cost:**
- Free tier: 50 email credits/month, 10,000 export credits/month (plenty to start)
- Basic: $49/month — 1,000 email credits
- Professional: $99/month — 2,000 email credits
- **Start on free.** It's genuinely useful for getting the system running.

**How to get it:**
1. Go to `https://app.apollo.io`
2. Sign up (no credit card for free tier)
3. Go to Settings → Integrations → API
4. Create an API key
5. Add to `.env` as `APOLLO_API_KEY=`

**Notes:**
- Apollo's free tier rate-limits to ~50 API requests/hour
- Some contact emails are hidden behind credit paywalls on free tier
- The `search` endpoint we use doesn't require email credits — only exporting emails does

---

## Recommended (Significantly Improves Quality)

### Hunter.io
**Used for:** Finding and verifying email addresses for contacts Apollo found

**Cost:**
- Free tier: 25 searches/month, 50 verifications/month
- Starter: $49/month — 500 searches, 1,000 verifications
- **Start on free** for testing, upgrade when you're sending real volume

**How to get it:**
1. Go to `https://hunter.io`
2. Sign up
3. Go to API → Your API key
4. Add to `.env` as `HUNTER_API_KEY=`

**Why bother:**
Email bounce rate is the #1 killer of cold email deliverability. Even a 5% 
bounce rate can get your domain flagged as spam. Hunter verification brings 
bounce rates under 2%.

---

## Required for Sending Emails

You need ONE of these two options:

### Option A: SMTP (Recommended to start)

Use your existing Gmail, Outlook, or custom domain email.

**Gmail setup:**
1. Enable 2-factor authentication on your Google account
2. Go to `https://myaccount.google.com/apppasswords`
3. Create an app password for "Mail"
4. Add to `.env`:
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx   # the app password, not your real password
SMTP_FROM_EMAIL=you@gmail.com
SMTP_FROM_NAME=Your Name
```

**Custom domain (recommended for business):**
Use whatever SMTP your domain host provides (Google Workspace, Zoho, etc.)
Sending from `you@agentsia.ai` is significantly more professional and 
deliverable than `you@gmail.com` for B2B cold outreach.

**Gmail free tier limits:** 500 emails/day (more than enough for cold outreach)

### Option B: SendGrid
**Cost:** Free tier: 100 emails/day forever. Essentials: $20/month for 50k/month

1. Go to `https://sendgrid.com`
2. Sign up and verify your sender domain
3. Create an API key with "Mail Send" permissions
4. Add to `.env` as `SENDGRID_API_KEY=`

**When to use SendGrid over SMTP:**
- You want open/click tracking
- You're sending more than 200 emails/day
- You want a dashboard showing delivery stats

---

## Optional / Future

### Clearbit
**Used for:** Company enrichment — adds firmographic data (revenue, tech stack, etc.)

**Cost:** Was free for limited use; now owned by HubSpot and pricing has changed.
Check `https://clearbit.com` for current pricing. 

**Honest take:** Apollo already provides most of this data. Skip Clearbit 
until you have a specific enrichment gap Apollo isn't filling.

### Supabase
**Used for:** Cloud database backend instead of local SQLite

**Cost:** Free tier is generous (500MB database, unlimited API requests)

Only needed if you want:
- Multiple people accessing the same lead database
- Cloud backup without managing files
- A foundation for a future web dashboard

Start with SQLite. Switch to Supabase when you need it.

---

## Cost Reality Check

For a solo operator running LeadGen for your own business:

| Service | Usage | Monthly Cost |
|---------|-------|-------------|
| Anthropic | 500 leads scored, 200 emails drafted | ~$2–4 |
| Apollo | 200 new leads/month | Free tier |
| Hunter | 100 verifications/month | Free tier |
| SMTP | 30 emails/day | Free (Gmail) |
| **Total** | | **~$2–4/month** |

Even at paid Apollo + Hunter tiers for higher volume:

| Service | Paid Tier | Monthly Cost |
|---------|-----------|-------------|
| Anthropic | 5,000 leads scored | ~$15 |
| Apollo Basic | 1,000 email credits | $49 |
| Hunter Starter | 500 searches | $49 |
| SendGrid | 50k emails | $20 |
| **Total** | | **~$133/month** |

That's a very reasonable cost of goods for a lead gen system that can 
generate $5,000–50,000/month in new business if it works.

---

## Getting Started — Minimum Viable Setup

If you want to get LeadGen running right now with the least friction:

1. **Anthropic** — sign up, get $5 free credits, you're good for weeks of testing
2. **Apollo free tier** — sign up, no credit card, enough to test the whole flow
3. **Gmail SMTP** — you already have this, just create an app password

That's it. Three steps, zero dollars, and you can run the full pipeline 
end-to-end. Add Hunter and paid tiers when you're ready to scale.
