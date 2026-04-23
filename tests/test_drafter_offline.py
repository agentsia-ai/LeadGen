"""OutreachDrafter behavior tests with a mocked Anthropic client."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from leadgen.ai.drafter import OutreachDrafter
from leadgen.models import OutreachRecord


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_draft_initial_parses_json_and_applies_signature(
    test_config, test_keys, sample_lead
) -> None:
    """A draft_initial response must:
      - parse the {subject, body} JSON cleanly
      - emit an OutreachRecord with sequence_step=0 and type='email'
      - append the configured signature (resolved with operator_name /
        operator_title / operator_email) to the body
    """
    d = OutreachDrafter(test_config, test_keys)
    d.client.messages.create = AsyncMock(
        return_value=_mock_response(
            '{"subject":"Quick question, Jane","body":"Saw Acme is hiring SDRs — curious how you\'re handling list research today."}'
        )
    )

    record = await d.draft_initial(sample_lead)
    assert record.subject == "Quick question, Jane"
    assert "hiring SDRs" in record.body
    # Signature format in the test_config fixture:
    #   "— {operator_name}, {operator_title}"
    assert "Tester, Founder" in record.body
    assert record.sequence_step == 0
    assert record.type == "email"


@pytest.mark.asyncio
async def test_draft_initial_handles_fenced_json(
    test_config, test_keys, sample_lead
) -> None:
    """Fenced ```json ... ``` wrapping must be stripped, matching the
    scorer's fenced-JSON behavior."""
    d = OutreachDrafter(test_config, test_keys)
    d.client.messages.create = AsyncMock(
        return_value=_mock_response(
            '```json\n{"subject":"Hi Jane","body":"Hello there."}\n```'
        )
    )
    record = await d.draft_initial(sample_lead)
    assert record.subject == "Hi Jane"
    assert "Hello there." in record.body


@pytest.mark.asyncio
async def test_draft_followup_raises_when_max_reached(
    test_config, test_keys, sample_lead
) -> None:
    """Once a lead has sent == max_follow_ups outreach records, calling
    draft_followup must raise rather than silently over-contact someone."""
    # test_config sets max_follow_ups=2
    now = datetime.utcnow()
    sample_lead.outreach_history = [
        OutreachRecord(subject="#0", body="a", sent_at=now, sequence_step=0),
        OutreachRecord(subject="#1", body="b", sent_at=now, sequence_step=1),
    ]
    d = OutreachDrafter(test_config, test_keys)
    with pytest.raises(ValueError):
        await d.draft_followup(sample_lead)


@pytest.mark.asyncio
async def test_draft_followup_uses_followup_system_prompt(
    test_config, test_keys, sample_lead
) -> None:
    """draft_followup must send the FOLLOWUP system prompt to Anthropic,
    not the initial one. Verify by inspecting the `system` kwarg passed
    to messages.create."""
    sample_lead.outreach_history = [
        OutreachRecord(
            subject="Initial",
            body="earlier body",
            sent_at=datetime.utcnow(),
            sequence_step=0,
        ),
    ]
    d = OutreachDrafter(test_config, test_keys)
    d.client.messages.create = AsyncMock(
        return_value=_mock_response('{"subject":"Re: Initial","body":"Circling back."}')
    )
    record = await d.draft_followup(sample_lead)

    assert record.sequence_step == 1
    # Inspect what system prompt was passed
    call_kwargs = d.client.messages.create.call_args.kwargs
    assert call_kwargs["system"] == d._followup_prompt
    # And the user-message prompt references the previous email
    user_prompt = call_kwargs["messages"][0]["content"]
    assert "Initial" in user_prompt  # previous subject leaked into context


def test_format_body_applies_operator_fields_into_signature(
    test_config, test_keys, sample_lead
) -> None:
    """The signature template supports {operator_name}, {operator_title},
    and {operator_email} placeholders — a regression here would strip an
    operator's identity off every outbound email."""
    test_config.outreach.signature = (
        "{operator_name}\n{operator_title}\n{operator_email}"
    )
    d = OutreachDrafter(test_config, test_keys)
    formatted = d._format_body("Hey Jane,", sample_lead)
    assert "Tester" in formatted
    assert "Founder" in formatted
    assert "tester@example.com" in formatted
    assert formatted.startswith("Hey Jane,")
