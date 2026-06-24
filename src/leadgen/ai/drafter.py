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
import re
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
- Never use em-dashes (—) or en-dashes (–); use commas, periods, or hyphens (-)
- Never start with "I hope this email finds you well" or similar clichés

HARD CONSTRAINT — NO FABRICATED SPECIFICS:
Only state concrete facts from the prospect info in the user message or the
sender's configured value prop/proof points. Never invent clients, case studies,
testimonials, statistics about other customers, or unverified "I noticed…"
claims. Stay generic when you lack a verifiable specific.

Do not include a greeting line — the engine prepends one from config. End the
body with the last message sentence — no sign-off or name. The engine appends
the signature block.

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

=== VERIFICATION RULES ===
Only use concrete facts from PROSPECT INFO or SENDER INFO above. Never invent
clients, case studies, testimonials, customer statistics, or "I saw/noticed"
claims. Do not include a greeting — the engine prepends one. End the body
without a sign-off — the engine appends the signature.

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
    def _title_case_name(name: str, *, preserve_stored_casing: bool = False) -> str:
        """Title-case a name from normalized (often lowercased) lead data.

        When ``preserve_stored_casing`` is True (company names), keep the stored
        string if it already has meaningful caps; otherwise title-case it.
        Person names always pass ``preserve_stored_casing=False``.
        """
        name = (name or "").strip()
        if not name:
            return name
        if preserve_stored_casing and name != name.lower():
            return name

        def capitalize_part(part: str) -> str:
            if not part:
                return part
            if "'" in part:
                return "'".join(
                    capitalize_part(p) if p else p for p in part.split("'")
                )
            return part[0].upper() + part[1:].lower()

        pieces: list[str] = []
        for segment in re.split(r"(\s+|-)", name):
            if not segment or segment.isspace() or segment == "-":
                pieces.append(segment)
            else:
                pieces.append(capitalize_part(segment))
        return "".join(pieces)

    def _display_company_name(self, lead: Lead) -> str:
        """Company name for interpolation — stored casing or title-case when all lower."""
        return self._title_case_name(
            lead.company.name or "", preserve_stored_casing=True
        )

    @staticmethod
    def _strip_em_dashes(text: str) -> str:
        """Replace em/en dashes with comma or hyphen — deterministic LLM slip guard."""
        text = re.sub(r"\s*—\s*", ", ", text)
        text = re.sub(r"\s*–\s*", "-", text)
        return text

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
            subject=self._normalize_subject(data["subject"], lead),
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
            subject=self._normalize_subject(data["subject"], lead),
            body=self._format_body(data["body"], lead),
            sequence_step=step,
        )

        logger.info(f"Drafted follow-up #{step} for {lead.display_name}")
        return record

    def _person_name_forms(self, lead: Lead) -> dict[str, str]:
        """First/last/display name tokens — always title-cased from the lead record."""
        forms: dict[str, str] = {}

        def add_token(token: str) -> None:
            token = token.strip()
            if len(token) < 2:
                return
            key = token.lower()
            if key not in forms:
                forms[key] = self._title_case_name(token)

        for name in (
            lead.contact.first_name,
            lead.contact.last_name,
            lead.contact.full_name,
            lead.display_name,
        ):
            if name:
                for word in re.findall(r"[\w']+", name):
                    add_token(word)

        return forms

    def _restore_company_name_phrase(self, text: str, lead: Lead) -> str:
        """Restore multi-word company names using display casing from the lead record."""
        name = self._display_company_name(lead)
        if not name:
            return text
        words = re.findall(r"[\w']+", name)
        if len(words) < 2:
            return text
        pattern = (
            r"(?<!\w)"
            + r"\s+".join(re.escape(w) for w in words)
            + r"(?!\w)"
        )
        return re.sub(pattern, lambda _m: name, text, flags=re.IGNORECASE)

    def _company_name_forms(self, lead: Lead) -> dict[str, str]:
        """Company name tokens — display casing from the lead record."""
        forms: dict[str, str] = {}
        display = self._display_company_name(lead)
        if display:
            for word in re.findall(r"[\w']+", display):
                word = word.strip()
                if len(word) >= 2:
                    key = word.lower()
                    if key not in forms:
                        forms[key] = word
        return forms

    def _acronym_forms_from_subject(self, subject: str, lead: Lead) -> dict[str, str]:
        """All-caps tokens from the model (ACME, B2B) — not person names like JANE."""
        person_keys = set(self._person_name_forms(lead).keys())
        forms: dict[str, str] = {}
        for word in re.findall(r"[\w']+", subject):
            letters = [c for c in word if c.isalpha()]
            if len(letters) < 2 or not all(c.isupper() for c in letters):
                continue
            key = word.lower()
            if key in person_keys or key in forms:
                continue
            forms[key] = word
        return forms

    def _apply_token_forms(self, text: str, forms: dict[str, str]) -> str:
        for token_lower, token_proper in forms.items():
            text = re.sub(
                rf"\b{re.escape(token_lower)}\b",
                token_proper,
                text,
                flags=re.IGNORECASE,
            )
        return text

    def _normalize_subject(self, subject: str, lead: Lead) -> str:
        """Apply config-driven subject casing without changing wording.

        Sentence-case lowercases Title Case noise but restores prospect names
        from the lead record and all-caps acronyms from the model output
        (e.g. "ACME", "B2B") so intentional casing survives normalization.
        """
        subject = (subject or "").strip()
        if not subject:
            return subject
        subject = self._strip_em_dashes(subject)
        casing = (self.config.outreach.subject_casing or "sentence").strip().lower()
        if casing == "lowercase":
            return subject.lower()
        # sentence-case: lowercase all, capitalize first character
        lowered = subject.lower()
        result = lowered[0].upper() + lowered[1:] if len(lowered) > 1 else lowered.upper()
        result = self._restore_company_name_phrase(result, lead)
        result = self._apply_token_forms(result, self._company_name_forms(lead))
        # Model's all-caps acronyms win over company title case (ACME vs Acme).
        result = self._apply_token_forms(
            result, self._acronym_forms_from_subject(subject, lead)
        )
        # Person names always use lead-record casing (Jane, not JANE).
        result = self._apply_token_forms(result, self._person_name_forms(lead))
        return result

    def _build_greeting(self, lead: Lead) -> str:
        """Deterministic opening line from config (e.g. 'Hi {first_name},')."""
        fmt = (self.config.outreach.greeting_format or "").strip()
        if not fmt:
            return ""
        first = (lead.contact.first_name or "").strip()
        if not first and lead.display_name:
            first = lead.display_name.split()[0]
        if not first:
            return ""
        return fmt.format(first_name=self._title_case_name(first))

    def _strip_model_greeting(self, body: str, lead: Lead) -> str:
        """Remove model-generated opening greetings so the engine owns the salutation."""
        body = body.strip()
        if not body:
            return body
        first = (lead.contact.first_name or "").strip()
        if not first and lead.display_name:
            first = lead.display_name.split()[0]
        if not first:
            return body

        lines = body.splitlines()
        while lines:
            line = lines[0].strip()
            if not line:
                lines.pop(0)
                continue
            low = line.lower().rstrip(",")
            if low in (first.lower(), f"hi {first.lower()}", f"hey {first.lower()}"):
                lines.pop(0)
                while lines and not lines[0].strip():
                    lines.pop(0)
                continue
            # Run-on: "Hi Jane, Saw your post..." on one line
            for prefix in (
                rf"^hi\s+{re.escape(first)}\s*,?\s*",
                rf"^hey\s+{re.escape(first)}\s*,?\s*",
                rf"^{re.escape(first)}\s*,?\s*",
            ):
                stripped = re.sub(prefix, "", line, count=1, flags=re.IGNORECASE).strip()
                if stripped != line:
                    if stripped:
                        lines[0] = stripped
                    else:
                        lines.pop(0)
                    break
            break
        return "\n".join(lines).strip()

    def _strip_model_signoff(self, body: str) -> str:
        """Remove model-generated sign-offs so only the deterministic footer signs."""
        lines = body.splitlines()
        signoff_words = {
            "best", "regards", "thanks", "thank you", "cheers", "sincerely",
            "warm regards", "kind regards", "best regards",
        }
        op_name = self.config.operator_name.strip().lower()
        op_first = op_name.split()[0] if op_name else ""

        while lines:
            last = lines[-1].strip()
            if not last:
                lines.pop()
                continue
            low = last.lower().rstrip(",")
            if low in signoff_words or low == op_name or (op_first and low == op_first):
                lines.pop()
                continue
            break
        return "\n".join(lines).rstrip()

    def _footer_link_lines(self) -> list[str]:
        """Plain visible URLs for the deterministic footer (display URL = actual URL)."""
        outreach = self.config.outreach
        lines: list[str] = []
        booking = (outreach.booking_url or "").strip()
        if booking:
            lines.append(booking)
        for url in outreach.footer_links:
            u = (url or "").strip()
            if u and u not in lines:
                lines.append(u)
        return lines

    def _format_body(self, body: str, lead: Lead) -> str:
        """Append signature and config-driven footer links to email body.

        Signatures use operator identity only — cold outreach is sent from the
        human operator, not the agent persona (agent_name/agent_email).
        """
        body = self._strip_model_signoff(body.strip())
        body = self._strip_model_greeting(body, lead)
        body = self._strip_em_dashes(body)
        body = self._apply_token_forms(body, self._person_name_forms(lead))
        body = self._restore_company_name_phrase(body, lead)
        greeting = self._build_greeting(lead)
        sig = self.config.outreach.signature.format(
            operator_name=self.config.operator_name,
            operator_title=self.config.operator_title,
            operator_email=self.config.operator_email,
            client_name=self.config.client_name,
        ).strip()
        link_lines = self._footer_link_lines()
        parts: list[str] = []
        if greeting:
            parts.append(greeting)
        if body:
            parts.append(body)
        if sig:
            parts.append(sig)
        if link_lines:
            parts.append("\n".join(link_lines))
        return "\n\n".join(parts)
