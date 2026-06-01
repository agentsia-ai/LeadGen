"""
LeadGen Core Data Models
Central Lead model used across sources, enrichment, CRM, and outreach.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from leadgen._time import now_utc


class LeadStatus(str, Enum):
    NEW = "new"
    ENRICHED = "enriched"
    SCORED = "scored"
    QUEUED = "queued"           # approved for outreach
    CONTACTED = "contacted"     # initial message sent
    FOLLOWING_UP = "following_up"
    RESPONDED = "responded"
    MEETING_BOOKED = "meeting_booked"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"
    UNSUBSCRIBED = "unsubscribed"
    BOUNCED = "bounced"


class LeadSource(str, Enum):
    APOLLO = "apollo"
    HUNTER = "hunter"
    PDL = "pdl"
    WEB_CRAWL = "web_crawl"
    CSV_IMPORT = "csv_import"
    MANUAL = "manual"
    REFERRAL = "referral"


class ContactInfo(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    email_verified: bool = False
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None


class CompanyInfo(BaseModel):
    name: str
    domain: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    annual_revenue: Optional[int] = None
    founded_year: Optional[int] = None
    description: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: str = "US"
    linkedin_url: Optional[str] = None
    technologies: list[str] = []


class ScoringBreakdown(BaseModel):
    industry_match: float = 0.0
    company_size_match: float = 0.0
    geography_match: float = 0.0
    pain_point_signals: float = 0.0
    contact_quality: float = 0.0
    total: float = 0.0
    reasoning: str = ""
    scored_at: Optional[datetime] = None


class OutreachRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = "email"          # email | linkedin | phone
    subject: Optional[str] = None
    body: str = ""
    drafted_at: datetime = Field(default_factory=now_utc)
    approved_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    replied_at: Optional[datetime] = None
    sequence_step: int = 0       # 0 = initial, 1+ = follow-ups

    # Belt-and-suspenders for the outreach_history JSON roundtrip: records
    # written pre-migration store naive ISO strings in the outreach_json
    # column, and Pydantic's default parser would faithfully reconstruct them
    # as naive. That would then explode at runtime on any aware-vs-naive
    # comparison (e.g. EmailSender.sync_sent_today vs today_start). This
    # validator coerces every datetime field to aware UTC on construction,
    # so legacy rows read through `OutreachRecord(**dict)` come out aware.
    @field_validator(
        "drafted_at", "approved_at", "sent_at", "opened_at", "replied_at",
        mode="after",
    )
    @classmethod
    def _coerce_aware_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class Lead(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: LeadSource
    status: LeadStatus = LeadStatus.NEW
    contact: ContactInfo
    company: CompanyInfo
    score: Optional[ScoringBreakdown] = None
    outreach_history: list[OutreachRecord] = []
    notes: str = ""
    tags: list[str] = []
    raw_data: dict = {}          # original API response, preserved for debugging
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @property
    def display_name(self) -> str:
        if self.contact.full_name:
            return self.contact.full_name
        parts = [self.contact.first_name, self.contact.last_name]
        return " ".join(p for p in parts if p) or "Unknown"

    @property
    def is_contactable(self) -> bool:
        return bool(self.contact.email and self.contact.email_verified)

    @property
    def next_follow_up_step(self) -> int:
        return len([r for r in self.outreach_history if r.sent_at is not None])

    def touch(self) -> None:
        self.updated_at = now_utc()
