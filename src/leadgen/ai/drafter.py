"""
LeadGen AI Outreach Drafter
Uses Claude to write personalized outreach emails for each lead.

This is the generic engine implementation. To customize for a productized
agent (e.g. a named persona with a distinctive voice), either:
  1. Subclass `OutreachDrafter` and override `INITIAL_SYSTEM_PROMPT` and/or
     `FOLLOWUP_SYSTEM_PROMPT` (and optionally the `_build_*_prompt` methods), or
  2. Point `config.ai.drafter_prompt_path` and `config.ai.followup_prompt_path`
     at external prompt files.

See CLAUDE.md → Customization Patterns for details.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic

from leadgen.config.loader import APIKeys, LeadGenConfig
from leadgen.models import Lead, OutreachRecord

logger = logging.getLogger(__name__)


DEFAULT_INITIAL_SYSTEM_PROMPT = """You are an expert B2B cold-outreach copywriter.

Write emails that:
- Feel personal and researched, not templated
- Lead with the prospect's situation, not a pitch
- Are SHORT (under 120 words for initial, under 80 for follow-ups)
- Have one clear, low-friction call to action
- Sound human — avoid corporate jargon and AI-speak
- Never start with "I hope this email finds you well" or similar clichés

Return ONLY the email content in this JSON format:
{
  "subject": "Subject line here",
  "body": "Email body here"
}

No preamble. No explanation. Just the JSON."""


DEFAULT_FOLLOWUP_SYSTEM_PROMPT = """You are writing follow-up emails in a B2B cold outreach sequence.

Follow-up rules:
- Reference the previous email briefly and naturally
- Add new value or a new angle — don't just say "following up"
- Be even shorter than the initial email
- Make it easy to say yes or ask a question

Return ONLY JSON: {"subject": "...", "body": "..."}"""


DRAFT_SYSTEM_PROMPT = DEFAULT_INITIAL_SYSTEM_PROMPT
FOLLOW_UP_SYSTEM_PROMPT = DEFAULT_FOLLOWUP_SYSTEM_PROMPT


class OutreachDrafter:
    """Drafts personalized outreach using Claude.

    Subclass this and override `INITIAL_SYSTEM_PROMPT` / `FOLLOWUP_SYSTEM_PROMPT`
    to define a tuned drafter with a custom voice. Per-deployment overrides can
    also be supplied via `config.ai.drafter_prompt_path` and
    `config.ai.followup_prompt_path`.
    """

    INITIAL_SYSTEM_PROMPT: str = DEFAULT_INITIAL_SYSTEM_PROMPT
    FOLLOWUP_SYSTEM_PROMPT: str = DEFAULT_FOLLOWUP_SYSTEM_PROMPT

    def __init__(self, config: LeadGenConfig, keys: APIKeys):
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.op = config  # shorthand
        self.model = config.ai.model
        self._initial_prompt = self._load_initial_prompt()
        self._followup_prompt = self._load_followup_prompt()

    def _load_prompt(self, override_path: str | None, fallback: str, label: str) -> str:
        if override_path:
            path = Path(override_path)
            if path.exists():
                logger.info(f"OutreachDrafter using {label} prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"{label}_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__} default"
            )
        return fallback

    def _load_initial_prompt(self) -> str:
        return self._load_prompt(
            self.config.ai.drafter_prompt_path, self.INITIAL_SYSTEM_PROMPT, "drafter"
        )

    def _load_followup_prompt(self) -> str:
        return self._load_prompt(
            self.config.ai.followup_prompt_path, self.FOLLOWUP_SYSTEM_PROMPT, "followup"
        )

    def _build_initial_prompt(self, lead: Lead) -> str:
        vp = self.config.value_prop
        op = self.config
        tone_map = {
            "formal": "professional and formal",
            "friendly-professional": "warm, friendly, and professional",
            "casual": "conversational and casual",
        }
        tone = tone_map.get(self.config.outreach.tone, "friendly-professional")

        return f"""Write a cold outreach email from {op.operator_name} ({op.operator_title}) 
to {lead.display_name} at {lead.company.name}.

=== SENDER INFO ===
Name: {op.operator_name}
Title: {op.operator_title}
Email: {op.operator_email}
Value Prop: {vp.one_liner}
Key Proof Points: {'; '.join(vp.proof_points)}

=== PROSPECT INFO ===
Name: {lead.display_name}
Title: {lead.contact.title or 'Unknown'}
Company: {lead.company.name}
Industry: {lead.company.industry or 'Unknown'}
Size: {lead.company.employee_count or 'Unknown'} employees
Location: {lead.company.city}, {lead.company.state}
Description: {lead.company.description or 'Not available'}
Technologies: {', '.join(lead.company.technologies[:5]) if lead.company.technologies else 'Unknown'}
ICP Score Reasoning: {lead.score.reasoning if lead.score else 'Not scored'}

=== TONE ===
{tone}

Write the initial cold outreach email now."""

    def _build_followup_prompt(self, lead: Lead, step: int) -> str:
        last = next(
            (r for r in reversed(lead.outreach_history) if r.sent_at is not None), None
        )
        previous_context = (
            f"Subject of previous email: {last.subject}\nBody: {last.body[:300]}..."
            if last else "No previous email context available."
        )

        follow_up_labels = {1: "first", 2: "second", 3: "third (final)"}
        label = follow_up_labels.get(step, f"follow-up #{step}")

        return f"""Write the {label} follow-up email to {lead.display_name} at {lead.company.name}.

=== PREVIOUS OUTREACH ===
{previous_context}

=== PROSPECT CONTEXT ===
Company: {lead.company.name}
Industry: {lead.company.industry or 'Unknown'}
Title: {lead.contact.title or 'Unknown'}

Keep it very short. Add a new angle or value point. Don't be pushy."""

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)

    async def draft_initial(self, lead: Lead) -> OutreachRecord:
        """Draft the initial outreach email for a lead."""
        prompt = self._build_initial_prompt(lead)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=self._initial_prompt,
            messages=[{"role": "user", "content": prompt}],
        )

        data = self._parse_json_response(response.content[0].text)
        record = OutreachRecord(
            type="email",
            subject=data["subject"],
            body=self._format_body(data["body"], lead),
            sequence_step=0,
        )

        logger.info(f"Drafted initial email for {lead.display_name} — '{record.subject}'")
        return record

    async def draft_followup(self, lead: Lead) -> OutreachRecord:
        """Draft the next follow-up email in the sequence."""
        step = lead.next_follow_up_step
        if step >= self.config.outreach.max_follow_ups:
            raise ValueError(
                f"Lead {lead.display_name} has reached max follow-ups ({self.config.outreach.max_follow_ups})"
            )

        prompt = self._build_followup_prompt(lead, step)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=400,
            system=self._followup_prompt,
            messages=[{"role": "user", "content": prompt}],
        )

        data = self._parse_json_response(response.content[0].text)
        record = OutreachRecord(
            type="email",
            subject=data["subject"],
            body=self._format_body(data["body"], lead),
            sequence_step=step,
        )

        logger.info(f"Drafted follow-up #{step} for {lead.display_name}")
        return record

    def _format_body(self, body: str, lead: Lead) -> str:
        """Append signature to email body."""
        sig = self.config.outreach.signature.format(
            operator_name=self.config.operator_name,
            operator_title=self.config.operator_title,
            operator_email=self.config.operator_email,
        )
        return f"{body.strip()}\n\n{sig.strip()}" if sig else body
