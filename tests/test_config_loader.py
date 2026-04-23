"""Config loader tests — YAML parsing, env-var key loading, scoring validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from leadgen.config.loader import APIKeys, ScoringConfig, load_api_keys, load_config


def _minimal_config_dict() -> dict:
    """Smallest valid config — only the required LeadGenConfig fields."""
    return {
        "client_name": "Example Co",
        "operator_name": "Alex",
        "operator_title": "Founder",
        "operator_email": "alex@example.com",
    }


def test_load_config_reads_yaml(tmp_path: Path) -> None:
    """load_config parses a minimal YAML and hydrates defaults for sub-models."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(_minimal_config_dict()), encoding="utf-8")

    cfg = load_config(cfg_path)
    assert cfg.client_name == "Example Co"
    assert cfg.operator_email == "alex@example.com"
    # Safety defaults — mirrors CustComm's invariant that approval is on unless
    # explicitly disabled in the deployed config.
    assert cfg.outreach.require_approval is True
    assert cfg.scoring.threshold == 0.60


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    """Missing config.yaml must raise FileNotFoundError, not a Pydantic error
    (so operators see an actionable message, not a validation traceback)."""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.yaml")


def test_load_config_flattens_top_level_scoring_threshold(tmp_path: Path) -> None:
    """Historical config shape allowed `scoring_threshold:` at the top level;
    load_config must route it into `scoring.threshold` so old configs keep
    working."""
    cfg_dict = _minimal_config_dict()
    cfg_dict["scoring_threshold"] = 0.42
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")

    cfg = load_config(cfg_path)
    assert cfg.scoring.threshold == 0.42


def test_api_keys_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """APIKeys.from_env reads the expected aliases from the process env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("HUNTER_API_KEY", "hunter-xyz")
    monkeypatch.setenv("APOLLO_API_KEY", "apollo-xyz")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)

    keys = load_api_keys()
    assert keys.anthropic == "sk-ant-test"
    assert keys.hunter == "hunter-xyz"
    assert keys.apollo == "apollo-xyz"
    assert keys.smtp_username == ""  # default when env missing


def test_api_keys_defaults_when_env_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env vars fall back to the defaults declared on APIKeys. The SMTP
    host/port defaults matter: they're used whenever a user enables SMTP but
    doesn't override host/port, and a regression here would silently send
    mail to the wrong server."""
    for k in [
        "ANTHROPIC_API_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY", "PDL_API_KEY",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
    ]:
        monkeypatch.delenv(k, raising=False)

    keys = APIKeys.from_env()
    assert keys.anthropic == ""
    assert keys.smtp_host == "smtp.gmail.com"
    assert keys.smtp_port == 587


def test_scoring_weight_out_of_range_rejected() -> None:
    """ScoringConfig rejects weights outside [0.0, 1.0] — protects the
    weighted-average contract the scorer prompt relies on."""
    with pytest.raises((ValidationError, AssertionError, ValueError)):
        ScoringConfig(industry_match=1.5)
    with pytest.raises((ValidationError, AssertionError, ValueError)):
        ScoringConfig(contact_quality=-0.1)


def test_default_scoring_weights_sum_to_one() -> None:
    """Invariant: the five default scoring weights sum to exactly 1.0 so the
    scorer's prompt instruction ('weighted average using these weights')
    produces a total in [0, 1].

    The engine does NOT currently enforce this at the model level (see
    NOTES.md → 'LeadGen scoring weights: no sum-to-1.0 validator'). If that
    validator is ever added, keep this test as the defaults-are-balanced
    regression check."""
    s = ScoringConfig()
    total = (
        s.industry_match
        + s.company_size_match
        + s.geography_match
        + s.pain_point_signals
        + s.contact_quality
    )
    # Tight epsilon — floats from Pydantic defaults are exact.
    assert abs(total - 1.0) < 1e-9, (
        f"Default scoring weights must sum to 1.0, got {total}. "
        f"Check leadgen.config.loader.ScoringConfig defaults."
    )
