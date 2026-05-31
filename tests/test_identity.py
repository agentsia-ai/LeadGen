"""Identity helpers — operator mail vs agent metadata."""

from __future__ import annotations

from leadgen.config.loader import (
    APIKeys,
    LeadGenConfig,
    display_agent_name,
    operator_from_email,
    operator_from_name,
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


def test_operator_from_email_prefers_config_when_env_blank() -> None:
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
        agent_email="bot@example.com",
    )
    keys = APIKeys()
    assert operator_from_email(cfg, keys) == "pat@example.com"


def test_operator_from_email_env_override() -> None:
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
    )
    keys = APIKeys()
    keys.smtp_from_email = "mailbox@example.com"
    assert operator_from_email(cfg, keys) == "mailbox@example.com"


def test_operator_from_name_uses_operator_not_agent() -> None:
    cfg = LeadGenConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Founder",
        operator_email="pat@example.com",
        agent_name="Lead Gen Assistant",
    )
    keys = APIKeys()
    assert operator_from_name(cfg, keys) == "Pat Operator"
