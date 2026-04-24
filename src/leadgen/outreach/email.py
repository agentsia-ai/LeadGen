"""
LeadGen Email Outreach Sender
Sends approved outreach emails via SMTP or SendGrid.

Features:
- Daily send limit enforcement (avoid spam flags)
- Bounce and unsubscribe handling
- Dry-run mode for testing without sending
- TODO: Per-domain cooling (don't hammer the same company)
"""

from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import aiosmtplib

from leadgen._time import now_utc
from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.crm.database import LeadDatabase
from leadgen.models import Lead, LeadStatus, OutreachRecord

logger = logging.getLogger(__name__)


class DailyLimitReached(Exception):
    """Raised when the configured daily send limit has been hit."""
    pass


class EmailSender:
    """
    Sends approved outreach emails and updates lead status.

    Supports two backends:
      - SMTP (default) — use Gmail, Outlook, or any SMTP server
      - SendGrid — for higher volume with open/click tracking

    Always call `check_daily_limit()` before sending batches.
    """

    def __init__(
        self,
        config: LeadGenConfig,
        keys: APIKeys,
        db: LeadDatabase,
        dry_run: bool = False,
    ):
        self.config = config
        self.keys = keys
        self.db = db
        self.dry_run = dry_run
        self._sent_today: int = 0  # in-memory counter, synced from DB on init

        # Determine backend
        self.use_sendgrid = bool(keys.sendgrid)
        self.use_smtp = bool(keys.smtp_username and keys.smtp_host)

        if not self.use_sendgrid and not self.use_smtp:
            logger.warning(
                "No email backend configured. Set SMTP_* or SENDGRID_API_KEY in .env"
            )

    # ── Public Interface ──────────────────────────────────────────────────────

    async def send_approved_outreach(self, lead: Lead) -> bool:
        """
        Send the next approved, unsent outreach message for a lead.

        Returns True if sent successfully, False otherwise.
        Raises DailyLimitReached if the daily limit has been hit.
        """
        # Find the next approved, unsent record
        record = self._next_pending_record(lead)
        if not record:
            logger.info(f"No pending approved outreach for {lead.display_name}")
            return False

        if not lead.contact.email:
            logger.warning(f"No email address for {lead.display_name} — skipping")
            return False

        await self._check_daily_limit()

        success = await self._send(
            to_email=lead.contact.email,
            to_name=lead.display_name,
            subject=record.subject or "(no subject)",
            body=record.body,
        )

        if success:
            record.sent_at = now_utc()
            lead.status = (
                LeadStatus.CONTACTED
                if record.sequence_step == 0
                else LeadStatus.FOLLOWING_UP
            )
            lead.touch()
            await self.db.upsert(lead)
            self._sent_today += 1
            logger.info(
                f"{'[DRY RUN] ' if self.dry_run else ''}Sent to {lead.display_name} "
                f"<{lead.contact.email}> — '{record.subject}'"
            )

        return success

    async def send_batch(self, leads: list[Lead]) -> dict:
        """
        Send approved outreach for a list of leads.
        Stops cleanly if the daily limit is hit mid-batch.

        Returns a summary dict with sent/skipped/failed counts.
        """
        await self.sync_sent_today()

        sent, skipped, failed = 0, 0, 0

        for lead in leads:
            try:
                result = await self.send_approved_outreach(lead)
                if result:
                    sent += 1
                else:
                    skipped += 1
            except DailyLimitReached:
                logger.info(f"Daily limit reached after {sent} sends. Stopping batch.")
                skipped += len(leads) - sent - skipped - failed
                break
            except Exception as e:
                logger.error(f"Failed to send to {lead.display_name}: {e}")
                failed += 1

        summary = {"sent": sent, "skipped": skipped, "failed": failed}
        logger.info(f"Batch send complete: {summary}")
        return summary

    async def handle_bounce(self, lead: Lead, email: str) -> None:
        """Mark a lead as bounced when we receive a bounce notification."""
        lead.status = LeadStatus.BOUNCED
        lead.notes = (lead.notes + f"\nBounced: {email} on {now_utc().date()}").strip()
        lead.touch()
        await self.db.upsert(lead)
        logger.info(f"Marked {lead.display_name} as BOUNCED")

    async def handle_unsubscribe(self, lead: Lead) -> None:
        """Mark a lead as unsubscribed."""
        lead.status = LeadStatus.UNSUBSCRIBED
        lead.touch()
        await self.db.upsert(lead)
        logger.info(f"Marked {lead.display_name} as UNSUBSCRIBED")

    # ── Backend Dispatch ──────────────────────────────────────────────────────

    async def _send(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        body: str,
    ) -> bool:
        """Route to the appropriate sending backend."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would send to {to_name} <{to_email}>: {subject}")
            return True

        if self.use_sendgrid:
            return await self._send_sendgrid(to_email, to_name, subject, body)
        elif self.use_smtp:
            return await self._send_smtp(to_email, to_name, subject, body)
        else:
            logger.error("No email backend configured — cannot send")
            return False

    async def _send_smtp(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        body: str,
    ) -> bool:
        """Send via SMTP (Gmail, Outlook, custom domain, etc.)"""
        keys = self.keys
        cfg = self.config

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{keys.smtp_from_name} <{keys.smtp_from_email}>" if keys.smtp_from_name else keys.smtp_from_email
        msg["To"] = f"{to_name} <{to_email}>"
        msg["Reply-To"] = keys.smtp_from_email

        # Plain text version
        msg.attach(MIMEText(body, "plain"))

        # HTML version (simple — wrap in basic template)
        html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
        html = f"<html><body><p>{html_body}</p></body></html>"
        msg.attach(MIMEText(html, "html"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=keys.smtp_host,
                port=keys.smtp_port,
                username=keys.smtp_username,
                password=keys.smtp_password,
                use_tls=False,
                start_tls=True,
            )
            return True
        except aiosmtplib.SMTPException as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected SMTP error for {to_email}: {e}")
            return False

    async def _send_sendgrid(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        body: str,
    ) -> bool:
        """Send via SendGrid API. Runs sync client in thread pool to avoid blocking."""
        import asyncio

        def _do_send() -> bool:
            import sendgrid
            from sendgrid.helpers.mail import Mail, Content, To

            sg = sendgrid.SendGridAPIClient(api_key=self.keys.sendgrid)
            message = Mail(
                from_email=(self.keys.smtp_from_email, self.keys.smtp_from_name or self.config.operator_name),
                to_emails=To(to_email, to_name),
                subject=subject,
            )
            message.add_content(Content("text/plain", body))

            html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
            message.add_content(Content("text/html", f"<html><body><p>{html_body}</p></body></html>"))

            response = sg.client.mail.send.post(request_body=message.get())

            if response.status_code in (200, 202):
                return True
            logger.error(f"SendGrid returned {response.status_code} for {to_email}")
            return False

        try:
            return await asyncio.to_thread(_do_send)
        except Exception as e:
            logger.error(f"SendGrid send failed for {to_email}: {e}")
            return False

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _next_pending_record(self, lead: Lead) -> Optional[OutreachRecord]:
        """Find the next approved but unsent outreach record."""
        for record in lead.outreach_history:
            if record.approved_at and not record.sent_at:
                return record
        return None

    async def _check_daily_limit(self) -> None:
        """Raise DailyLimitReached if we've hit today's send cap."""
        limit = self.config.outreach.daily_email_limit
        if self._sent_today >= limit:
            raise DailyLimitReached(
                f"Daily send limit of {limit} reached. "
                f"Increase outreach.daily_email_limit in config.yaml to send more."
            )

    async def sync_sent_today(self) -> int:
        """
        Sync the in-memory sent counter from the database.
        Call this on startup to account for sends in previous sessions today.
        """
        from leadgen.models import LeadStatus
        today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)

        # Count leads moved to CONTACTED or FOLLOWING_UP today
        contacted = await self.db.list(status=LeadStatus.CONTACTED, limit=1000)
        following = await self.db.list(status=LeadStatus.FOLLOWING_UP, limit=1000)

        sent_today = 0
        for lead in contacted + following:
            for record in lead.outreach_history:
                if record.sent_at and record.sent_at >= today_start:
                    sent_today += 1

        self._sent_today = sent_today
        logger.debug(f"Synced sent_today counter: {sent_today}")
        return sent_today
