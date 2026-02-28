# Gmail SMTP Setup for LeadGen

LeadGen sends outreach emails via SMTP. This guide walks you through setting up Gmail for sending — no Cloudflare, Lambda, or other infrastructure needed. LeadGen connects directly to Gmail's SMTP server.

---

## Why Gmail SMTP (Not Your Contact Form Setup)

Many website owners use Cloudflare, AWS Lambda, or similar services to *receive* contact form submissions and forward them to their inbox. That's an **inbound** flow.

LeadGen needs the opposite: **outbound** email — sending cold outreach from your address to leads. For that, you use Gmail's SMTP server directly. Same Gmail account, different purpose.

---

## Step 1 — Enable 2-Factor Authentication

Gmail requires 2FA before you can create an app password.

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Under "How you sign in to Google," click **2-Step Verification**
3. Follow the prompts to enable it (SMS or authenticator app)

---

## Step 2 — Create an App Password

1. Go to [App Passwords](https://myaccount.google.com/apppasswords)
   - If you don't see this page, 2FA may not be fully enabled yet
2. Under "Select app," choose **Mail**
3. Under "Select device," choose **Other** and type `LeadGen`
4. Click **Generate**
5. Copy the 16-character password (format: `xxxx xxxx xxxx xxxx`)
   - Store it somewhere safe — you won't see it again
   - Use it in `.env` *without* spaces: `xxxxxxxxxxxxxxxx`

---

## Step 3 — Add to Your `.env` File

Open `.env` and add or update:

```env
# Gmail SMTP
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your.email@gmail.com
SMTP_PASSWORD=your16charapppassword
SMTP_FROM_EMAIL=your.email@gmail.com
SMTP_FROM_NAME=Your Name
```

**Notes:**
- `SMTP_USERNAME` and `SMTP_FROM_EMAIL` are usually the same (your Gmail address)
- `SMTP_PASSWORD` is the app password, **not** your regular Gmail password
- Remove any spaces from the app password when pasting

---

## Step 4 — Test the Connection

Run the test command (does **not** send any email):

```bash
leadgen smtp-test
```

You should see:
```
✓ SMTP connection successful.
  Host: smtp.gmail.com:587
  From: Your Name <your.email@gmail.com>
```

If it fails, you'll see the error. Common issues:
- **"Authentication failed"** — Wrong app password, or 2FA not enabled
- **"Connection refused"** — Firewall/network blocking port 587; try port 465 with `use_tls=True` (not yet supported in LeadGen — open an issue)
- **"Username and Password not accepted"** — Use the app password, not your regular password

---

## Using a Custom Domain (e.g. you@yourcompany.com)

If you use Google Workspace with your own domain:

1. The SMTP settings are the same: `smtp.gmail.com`, port 587
2. Use your Workspace email as `SMTP_USERNAME` and `SMTP_FROM_EMAIL`
3. Create the app password the same way (from your Google Account)
4. `SMTP_FROM_NAME` can be your business name or your name

---

## Gmail Sending Limits

- **Personal Gmail:** 500 emails/day
- **Google Workspace:** 2,000 emails/day (varies by plan)

LeadGen's default `daily_email_limit` in config.yaml is 30 — well under Gmail's limits. Increase it when you're ready to scale.

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| "No email backend configured" | Add SMTP_* variables to `.env` |
| "Authentication failed" | Create a new app password; ensure 2FA is on |
| "Connection timed out" | Check firewall; some corporate networks block SMTP |
| "Less secure app" message | Use an app password, not your regular password |

---

## Next Steps

After `leadgen smtp-test` succeeds:

1. Run `leadgen send --dry-run` to simulate sending (no emails sent)
2. When ready, run `leadgen send` to send approved outreach

See `docs/GETTING_STARTED.md` for the full workflow.
