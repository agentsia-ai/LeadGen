"""
LeadGen Configuration Loader
Loads and validates client config from YAML + environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()


# ── Pydantic models for config validation ────────────────────────────────────

class ICPConfig(BaseModel):
    industries: list[str] = []
    company_size: dict[str, int] = {"min_employees": 1, "max_employees": 10000}
    annual_revenue: dict[str, int] = {}
    geography: dict[str, Any] = {"countries": ["US"], "states": [], "cities": []}
    pain_points: list[str] = []
    positive_signals: list[str] = []
    negative_signals: list[str] = []


class ValuePropConfig(BaseModel):
    headline: str = ""
    one_liner: str = ""
    proof_points: list[str] = []


class OutreachConfig(BaseModel):
    tone: str = "friendly-professional"
    daily_email_limit: int = 30
    follow_up_days: list[int] = [3, 7, 14]
    max_follow_ups: int = 3
    require_approval: bool = True
    signature: str = ""


class SourcesConfig(BaseModel):
    apollo: dict[str, Any] = {"enabled": False}
    hunter: dict[str, Any] = {"enabled": False}
    pdl: dict[str, Any] = {"enabled": False}
    web_crawl: dict[str, Any] = {"enabled": False}
    csv_import: dict[str, Any] = {"enabled": True, "watch_folder": "./imports"}


class ScoringConfig(BaseModel):
    industry_match: float = 0.30
    company_size_match: float = 0.20
    geography_match: float = 0.15
    pain_point_signals: float = 0.25
    contact_quality: float = 0.10
    threshold: float = Field(default=0.60, alias="scoring_threshold")

    @field_validator("industry_match", "company_size_match", "geography_match",
                     "pain_point_signals", "contact_quality")
    @classmethod
    def weight_in_range(cls, v: float) -> float:
        assert 0.0 <= v <= 1.0, "Scoring weight must be between 0 and 1"
        return v


class DatabaseConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_path: str = "./data/leadgen.db"
    supabase_url: str = ""


class AIConfig(BaseModel):
    """Optional AI customization. Lets a deployment swap the default Claude
    model and/or override the engine's built-in system prompts by pointing at
    external text files — without subclassing.

    Subclassing the `LeadScorer` / `OutreachDrafter` classes is the other
    supported customization path; see CLAUDE.md → Customization Patterns.
    """
    model: str = "claude-sonnet-4-20250514"
    scorer_prompt_path: str | None = None
    drafter_prompt_path: str | None = None
    followup_prompt_path: str | None = None


class LeadGenConfig(BaseModel):
    client_name: str
    operator_name: str
    operator_title: str
    operator_email: str
    icp: ICPConfig = ICPConfig()
    value_prop: ValuePropConfig = ValuePropConfig()
    outreach: OutreachConfig = OutreachConfig()
    sources: SourcesConfig = SourcesConfig()
    scoring: ScoringConfig = ScoringConfig()
    database: DatabaseConfig = DatabaseConfig()
    ai: AIConfig = AIConfig()


# ── API Keys (from environment only — never in config files) ─────────────────

class APIKeys(BaseModel):
    anthropic: str = Field(default="", alias="ANTHROPIC_API_KEY")
    apollo: str = Field(default="", alias="APOLLO_API_KEY")
    hunter: str = Field(default="", alias="HUNTER_API_KEY")
    pdl: str = Field(default="", alias="PDL_API_KEY")
    clearbit: str = Field(default="", alias="CLEARBIT_API_KEY")
    sendgrid: str = Field(default="", alias="SENDGRID_API_KEY")
    smtp_host: str = Field(default="smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="", alias="SMTP_FROM_NAME")

    @classmethod
    def from_env(cls) -> "APIKeys":
        return cls(**{
            field.alias: os.getenv(field.alias, field.default)
            for field in cls.model_fields.values()
            if field.alias
        })


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config(config_path: str | Path | None = None) -> LeadGenConfig:
    """Load and validate client config from YAML file."""
    path = Path(config_path or os.getenv("CONFIG_PATH", "config.yaml"))

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.yaml to {path} and fill in your details."
        )

    with open(path) as f:
        raw = yaml.safe_load(f)

    # Flatten scoring_threshold into scoring dict for Pydantic
    if "scoring_threshold" in raw:
        raw.setdefault("scoring", {})["scoring_threshold"] = raw.pop("scoring_threshold")

    return LeadGenConfig(**raw)


def load_api_keys() -> APIKeys:
    """Load API keys from environment variables."""
    return APIKeys.from_env()
