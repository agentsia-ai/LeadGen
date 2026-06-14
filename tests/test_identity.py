"""Identity helpers — operator mail vs agent metadata."""

from __future__ import annotations

from leadgen.config.loader import (
    APIKeys,
    LeadGenConfig,
    OutreachConfig,
    display_agent_name,
    outbound_from_email,
    outbound_from_name,
    operator_from_email,
)


def test_display_agent_name_falls_back_to_engine_name() -> None:
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
    )
    assert display_agent_name(cfg) == "leadgen"
    cfg.agent_name = "Lead Gen Assistant"
    assert display_agent_name(cfg) == "Lead Gen Assistant"


def test_outbound_from_email_uses_operator_by_default() -> None:
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
        agent_email="bot@example.com",
    )
    keys = APIKeys()
    assert outbound_from_email(cfg, keys) == "pat@example.com"
    assert operator_from_email(cfg, keys) == "pat@example.com"


def test_outbound_from_email_ignores_smtp_from_env_override() -> None:
    """SMTP_FROM_EMAIL is for auth routing — config operator_email is the From line."""
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
        agent_email="bot@example.com",
    )
    keys = APIKeys()
    keys.smtp_from_email = "bot@example.com"
    keys.smtp_from_name = "Bot Persona"
    assert outbound_from_email(cfg, keys) == "pat@example.com"
    assert outbound_from_name(cfg, keys) == "Pat Operator"


def test_outbound_from_email_agent_identity_when_configured() -> None:
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
        agent_name="Rex",
        agent_email="bot@example.com",
        outreach=OutreachConfig(from_identity="agent"),
    )
    keys = APIKeys()
    assert outbound_from_email(cfg, keys) == "bot@example.com"
    assert outbound_from_name(cfg, keys) == "Rex | Example Co"
