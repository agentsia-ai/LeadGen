"""Outbound signature rendering — operator identity only."""

from __future__ import annotations

from leadgen.ai.drafter import OutreachDrafter
from leadgen.config.loader import LeadGenConfig, OutreachConfig


def test_signature_uses_operator_fields_not_agent(
    test_config, test_keys, sample_lead
) -> None:
    test_config.agent_name = "Lead Gen Assistant"
    test_config.agent_email = "assistant@example.com"
    test_config.outreach.signature = (
        "{operator_name}\n{operator_title}\n{operator_email}"
    )

    d = OutreachDrafter(test_config, test_keys)
    formatted = d._format_body("Hey Jane,", sample_lead)

    assert "Tester" in formatted
    assert "Founder" in formatted
    assert "tester@example.com" in formatted
    assert "Lead Gen Assistant" not in formatted
    assert "assistant@example.com" not in formatted
