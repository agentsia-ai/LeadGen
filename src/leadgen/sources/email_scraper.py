"""
Contact-page email scraper — recovers publicly published emails Hunter misses.

Fetches a small set of likely contact-bearing pages (homepage + nav-discovered
subpages + common fallbacks), extracts emails, filters junk, and ranks candidates.
Does not auto-verify; surfaces results for operator review.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from leadgen.crm.database import LeadDatabase
    from leadgen.models import Lead

logger = logging.getLogger(__name__)

USER_AGENT = "LeadGen/0.1 (+contact enrichment; respects robots.txt)"
REQUEST_TIMEOUT = 15.0
MAX_PAGES_PER_LEAD = 6

CONTACT_SLUGS = frozenset({
    "contact",
    "contact-us",
    "contactus",
    "about",
    "about-us",
    "aboutus",
    "team",
    "our-team",
    "staff",
    "appointments",
    "attorneys",
    "agents",
    "people",
    "firm",
    "office",
    "locations",
    "location",
    "meet-the-team",
})

CONTACT_ANCHOR_KEYWORDS = (
    "contact",
    "about",
    "team",
    "staff",
    "appointment",
    "attorney",
    "agent",
    "reach us",
    "get in touch",
    "office",
    "location",
)

COMMON_FALLBACK_PATHS = (
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/team",
    "/staff",
    "/appointments",
    "/attorneys",
    "/agents",
)

ROLE_LOCAL_PARTS = frozenset({
    "info",
    "admin",
    "contact",
    "hello",
    "sales",
    "support",
    "office",
    "inquiries",
    "inquiry",
    "help",
    "mail",
    "reception",
    "customerservice",
    "service",
    "team",
    "general",
})

JUNK_EMAIL_DOMAINS = frozenset({
    "godaddy.com",
    "wix.com",
    "squarespace.com",
    "wordpress.com",
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "squarespace-cdn.com",
    "placeholder.com",
    "yoursite.com",
    "yourdomain.com",
})

JUNK_LOCAL_PREFIXES = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "example",
    "test",
    "placeholder",
    "yourname",
    "your.email",
    "name@",
    "email@",
    "username@",
    "sample",
    "fake",
    "null",
)

# Registered-looking gTLDs (3+ chars). Two-letter suffixes are treated as ccTLDs.
PLAUSIBLE_GTlds = frozenset({
    "aaa", "aarp", "abogado", "academy", "accountant", "accountants", "actor",
    "ads", "adult", "aero", "agency", "airforce", "apartments", "app", "archi",
    "army", "art", "associates", "attorney", "auction", "audio", "auto", "autos",
    "baby", "band", "bank", "bar", "bargains", "beer", "best", "bet", "bid",
    "bike", "bingo", "bio", "biz", "black", "blog", "blue", "boats", "bond",
    "boutique", "build", "builders", "business", "buzz", "cab", "cafe", "cam",
    "camp", "capital", "cards", "care", "careers", "cash", "casino", "catering",
    "center", "ceo", "chat", "cheap", "church", "city", "claims", "cleaning",
    "clinic", "clothing", "cloud", "club", "coach", "codes", "coffee", "college",
    "com", "community", "company", "computer", "condos", "construction", "consulting",
    "contact", "contractors", "cooking", "cool", "coop", "country", "coupons",
    "credit", "creditcard", "cruises", "dance", "dating", "day", "deals",
    "degree", "delivery", "dental", "dentist", "design", "dev", "diamonds",
    "digital", "direct", "directory", "discount", "doctor", "dog", "domains",
    "download", "earth", "edu", "education", "email", "energy", "engineer", "engineering",
    "enterprises", "equipment", "estate", "events", "exchange", "expert",
    "experts", "fail", "family", "fan", "farm", "finance", "financial", "fish",
    "fit", "fitness", "flights", "florist", "flowers", "food", "football",
    "forsale", "foundation", "fund", "furniture", "futbol", "fyi", "gallery",
    "game", "games", "garden", "gift", "gifts", "gives", "glass", "global",
    "gmbh", "gold", "golf", "gov", "graphics", "gratis", "green", "gripe", "group",
    "guide", "guru", "health", "healthcare", "help", "hiphop", "hockey", "holdings",
    "holiday", "homes", "horse", "hospital", "host", "hosting", "house", "how",
    "immo", "immobilien", "industries", "info", "ink", "institute", "insurance",
    "insure", "int", "international", "investments", "jewelry", "jobs", "juegos", "kaufen",
    "kim", "kitchen", "land", "law", "lawyer", "lawyers", "lease", "legal",
    "lgbt", "life", "lighting", "limited", "limo", "link", "live", "loan",
    "loans", "lol", "love", "ltd", "luxury", "maison", "management", "market",
    "marketing", "markets", "mba", "media", "memorial", "men", "menu", "mil",
    "mobi", "moda", "moe", "money", "mortgage", "movie", "museum", "name", "navy",
    "net", "network", "news", "ninja", "one", "online", "org", "organic", "partners", "parts",
    "party", "pet", "photo", "photography", "photos", "pics", "pictures", "pizza",
    "place", "plumbing", "plus", "poker", "press", "pro", "productions",
    "properties", "property", "pub", "qpon", "recipes", "red", "rehab", "reise",
    "reisen", "rent", "rentals", "repair", "report", "republican", "rest",
    "restaurant", "reviews", "rich", "rip", "rocks", "rodeo", "run", "sale",
    "salon", "sarl", "school", "schule", "science", "services", "sexy", "shiksha",
    "shoes", "shop", "shopping", "show", "singles", "site", "ski", "social",
    "software", "solar", "solutions", "space", "store", "studio", "style",
    "supplies", "supply", "support", "surgery", "systems", "tax", "taxi", "team",
    "tech", "technology", "tel", "tennis", "theater", "theatre", "tienda", "tips",
    "today", "tools", "top", "tours", "town", "toys", "trade", "training",
    "travel", "university", "uno", "vacations", "ventures", "vet", "viajes",
    "video", "villas", "vin", "vision", "voyage", "watch", "webcam", "website",
    "wedding", "wiki", "win", "wine", "work", "works", "world", "wtf", "xyz",
    "yoga", "zone",
})

COMPOUND_TLD_PREFIXES = frozenset({
    "ac", "co", "com", "edu", "gov", "ltd", "me", "mil", "net", "nhs", "org",
    "plc", "sch",
})

DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\'-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

OBFUSCATION_RE = re.compile(
    r"\b([a-zA-Z0-9._%+\'-]+)\s*(?:\[?\s*(?:@|at)\s*\]?)\s*"
    r"([a-zA-Z0-9.-]+)\s*(?:\[?\s*(?:\.|dot)\s*\]?)\s*([a-zA-Z]{2,})\b",
    re.IGNORECASE,
)


@dataclass
class EmailCandidate:
    email: str
    page_url: str
    domain_match: bool
    is_role_inbox: bool
    rank: int

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "page_url": self.page_url,
            "domain_match": self.domain_match,
            "is_role_inbox": self.is_role_inbox,
            "rank": self.rank,
        }


@dataclass
class ScrapeResult:
    lead_id: str | None = None
    domain: str | None = None
    status: str = "no_email"
    pages_fetched: list[str] = field(default_factory=list)
    candidates: list[EmailCandidate] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict:
        best = self.candidates[0].email if self.candidates else None
        return {
            "lead_id": self.lead_id,
            "domain": self.domain,
            "status": self.status,
            "pages_fetched": self.pages_fetched,
            "candidates": [c.to_dict() for c in self.candidates],
            "best_email": best,
            "reason": self.reason,
        }


def normalize_domain(domain: str | None) -> str | None:
    """Extract clean domain (e.g. company.com) from URL or raw domain."""
    if not domain or not isinstance(domain, str):
        return None
    domain = domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    return domain.split("/")[0] if domain else None


def _site_base_url(domain: str) -> str:
    return f"https://{domain}"


def _same_site(url: str, domain: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == domain or host.endswith(f".{domain}")


def _path_slug(path: str) -> str:
    slug = path.strip("/").split("/")[-1].lower()
    return slug.split("?")[0].split("#")[0]


def _is_contactish_path(path: str) -> bool:
    slug = _path_slug(path)
    if slug in CONTACT_SLUGS:
        return True
    return any(token in slug for token in CONTACT_SLUGS)


def _is_contactish_anchor(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    return any(keyword in lowered for keyword in CONTACT_ANCHOR_KEYWORDS)


def _effective_tld(domain: str) -> str | None:
    """Return the registrable suffix used for TLD plausibility checks."""
    labels = domain.lower().split(".")
    if len(labels) < 2:
        return None
    if (
        len(labels) >= 3
        and labels[-2] in COMPOUND_TLD_PREFIXES
        and len(labels[-1]) == 2
        and labels[-1].isalpha()
    ):
        return labels[-1]
    return labels[-1]


def _has_plausible_tld(domain: str) -> bool:
    tld = _effective_tld(domain)
    if not tld or not tld.isalpha():
        return False
    if len(tld) == 2:
        return True
    return tld in PLAUSIBLE_GTlds


def _is_structurally_valid_email(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if len(local) > 64 or len(domain) > 253:
        return False
    if local.startswith(".") or local.endswith("."):
        return False
    if ".." in local or ".." in domain:
        return False

    labels = domain.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not label or len(label) > 63:
            return False
        if len(label) == 1:
            if not label.isalnum():
                return False
            continue
        if not DOMAIN_LABEL_RE.match(label):
            return False

    return _has_plausible_tld(domain)


def _is_junk_email(email: str) -> bool:
    lowered = email.lower().strip()
    if not EMAIL_RE.fullmatch(lowered):
        return True

    local, _, domain_part = lowered.partition("@")
    if not _is_structurally_valid_email(lowered):
        return True
    if domain_part in JUNK_EMAIL_DOMAINS:
        return True
    if any(domain_part.endswith(f".{junk}") for junk in JUNK_EMAIL_DOMAINS):
        return True
    if any(local.startswith(prefix) for prefix in JUNK_LOCAL_PREFIXES):
        return True
    if local in {"you", "your", "name", "email", "username", "user"}:
        return True

    # Reject obvious asset filenames parsed as emails
    if domain_part.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
        return True

    return False


def _is_confident_candidate(candidate: EmailCandidate) -> bool:
    """True when apply=true may write this candidate without operator review."""
    if candidate.domain_match:
        return True
    path = urlparse(candidate.page_url).path or "/"
    return _is_contactish_path(path)


def _deobfuscate_text(text: str) -> str:
    """Normalize common 'name [at] domain [dot] com' patterns to plain emails."""
    normalized = text
    normalized = re.sub(r"\s*\[\s*at\s*\]\s*", "@", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*\(\s*at\s*\)\s*", "@", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+at\s+", "@", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*\[\s*dot\s*\]\s*", ".", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*\(\s*dot\s*\)\s*", ".", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+dot\s+", ".", normalized, flags=re.IGNORECASE)
    return normalized


def extract_emails_from_html(html: str) -> set[str]:
    """Extract candidate emails from HTML (mailto links, visible text, obfuscations)."""
    found: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if isinstance(href, str) and href.lower().startswith("mailto:"):
            mailto = href[7:].split("?")[0].strip()
            if mailto and not _is_junk_email(mailto):
                found.add(mailto.lower())

    text = soup.get_text(" ", strip=True)
    deobfuscated = _deobfuscate_text(text)
    for match in EMAIL_RE.findall(deobfuscated):
        if not _is_junk_email(match):
            found.add(match.lower())

    for match in OBFUSCATION_RE.findall(deobfuscated):
        candidate = f"{match[0]}@{match[1]}.{match[2]}".lower()
        if not _is_junk_email(candidate):
            found.add(candidate)

    return found


def discover_candidate_urls(homepage_html: str, base_url: str, domain: str) -> list[str]:
    """Discover contact-ish subpages from homepage nav links + common fallbacks."""
    urls: list[str] = [base_url]
    seen: set[str] = {base_url.rstrip("/")}

    soup = BeautifulSoup(homepage_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not isinstance(href, str) or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if not _same_site(absolute, domain):
            continue

        path = parsed.path or "/"
        anchor_text = anchor.get_text(" ", strip=True)
        if _is_contactish_path(path) or _is_contactish_anchor(anchor_text):
            normalized = f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/') or '/'}"
            key = normalized.rstrip("/")
            if key not in seen:
                seen.add(key)
                urls.append(normalized)

    for path in COMMON_FALLBACK_PATHS:
        candidate = urljoin(base_url, path)
        key = candidate.rstrip("/")
        if key not in seen:
            seen.add(key)
            urls.append(candidate)

    return urls[:MAX_PAGES_PER_LEAD]


def _is_role_inbox(local_part: str) -> bool:
    base = local_part.split("+")[0].split(".")[0]
    return base in ROLE_LOCAL_PARTS


def _candidate_rank(
    email: str,
    lead_domain: str,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
) -> tuple[int, bool, bool]:
    """Lower rank is better. Returns (rank, domain_match, is_role_inbox)."""
    local = email.split("@")[0]
    email_domain = email.split("@", 1)[1]
    domain_match = email_domain == lead_domain or email_domain.endswith(f".{lead_domain}")
    role = _is_role_inbox(local)

    if first_name or last_name:
        name_tokens = [
            t.lower()
            for t in (first_name, last_name)
            if t
        ]
        if any(token in local for token in name_tokens):
            return (0 if domain_match else 2, domain_match, role)

    if domain_match and not role:
        return (1, domain_match, role)
    if domain_match and role:
        return (3, domain_match, role)
    if not domain_match and not role:
        return (4, domain_match, role)
    return (5, domain_match, role)


def rank_candidates(
    raw: dict[str, str],
    lead_domain: str,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
) -> list[EmailCandidate]:
    """Rank extracted emails; dedupe by address keeping best rank."""
    best_by_email: dict[str, EmailCandidate] = {}
    for email, page_url in raw.items():
        rank, domain_match, role = _candidate_rank(
            email,
            lead_domain,
            first_name=first_name,
            last_name=last_name,
        )
        candidate = EmailCandidate(
            email=email,
            page_url=page_url,
            domain_match=domain_match,
            is_role_inbox=role,
            rank=rank,
        )
        existing = best_by_email.get(email)
        if existing is None or candidate.rank < existing.rank:
            best_by_email[email] = candidate

    return sorted(best_by_email.values(), key=lambda c: (c.rank, c.email))


class ContactEmailScraper:
    """Polite, sequential fetcher for published contact emails on a firm site."""

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = REQUEST_TIMEOUT,
        max_pages: int = MAX_PAGES_PER_LEAD,
        client: httpx.AsyncClient | None = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_pages = max_pages
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> ContactEmailScraper:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *args) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _fetch_text(self, url: str) -> str | None:
        assert self._client is not None
        try:
            response = await self._client.get(url)
            if response.status_code >= 400:
                logger.debug("Skip %s — HTTP %s", url, response.status_code)
                return None
            content_type = response.headers.get("content-type", "")
            snippet = (response.text or "")[:500].lower()
            looks_like_html = "<html" in snippet or "<!doctype" in snippet
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                if not looks_like_html:
                    return None
            return response.text
        except httpx.HTTPError as exc:
            logger.debug("Fetch failed for %s: %s", url, exc)
            return None

    async def _robots_allows(self, domain: str, url: str) -> bool:
        assert self._client is not None
        robots_url = f"https://{domain}/robots.txt"
        try:
            response = await self._client.get(robots_url)
            if response.status_code >= 400:
                return True
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            return parser.can_fetch(self.user_agent, url)
        except httpx.HTTPError:
            return True

    async def scrape_domain(
        self,
        domain: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> ScrapeResult:
        domain = normalize_domain(domain) or domain
        if not domain:
            return ScrapeResult(status="no_domain", reason="no domain provided")

        base_url = _site_base_url(domain)
        homepage_html = await self._fetch_text(base_url)
        if not homepage_html:
            return ScrapeResult(
                domain=domain,
                status="fetch_failed",
                reason=f"could not fetch homepage for {domain}",
            )

        if not await self._robots_allows(domain, base_url):
            return ScrapeResult(
                domain=domain,
                status="robots_disallowed",
                reason=f"robots.txt disallows fetching {base_url}",
            )

        candidate_urls = discover_candidate_urls(homepage_html, base_url, domain)
        pages_fetched: list[str] = []
        found: dict[str, str] = {}

        for url in candidate_urls[: self.max_pages]:
            if not await self._robots_allows(domain, url):
                logger.debug("robots.txt disallows %s", url)
                continue

            html = homepage_html if url.rstrip("/") == base_url.rstrip("/") else await self._fetch_text(url)
            if html is None:
                continue

            pages_fetched.append(url)
            for email in extract_emails_from_html(html):
                found.setdefault(email, url)

        candidates = rank_candidates(
            found,
            domain,
            first_name=first_name,
            last_name=last_name,
        )
        status = "found" if candidates else "no_email"
        return ScrapeResult(
            domain=domain,
            status=status,
            pages_fetched=pages_fetched,
            candidates=candidates,
            reason=None if candidates else "no published email found on candidate pages",
        )


async def scrape_lead_email(
    lead: Lead,
    *,
    domain: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ScrapeResult:
    """Scrape published contact emails for a single lead record."""
    resolved = normalize_domain(domain or lead.company.domain or lead.company.website)
    if not resolved:
        return ScrapeResult(
            lead_id=lead.id,
            status="no_domain",
            reason="no domain on lead and none provided",
        )

    async with ContactEmailScraper(client=client) as scraper:
        result = await scraper.scrape_domain(
            resolved,
            first_name=lead.contact.first_name,
            last_name=lead.contact.last_name,
        )
    result.lead_id = lead.id
    return result


async def scrape_lead_email_by_id(
    db: LeadDatabase,
    lead_id: str,
    *,
    domain: str | None = None,
    apply: bool = False,
) -> dict:
    """Scrape emails for one lead by id; optional apply writes top candidate unverified."""
    from leadgen.crm.suppression import check_lead_suppressed
    from leadgen.crm.update_lead import update_lead

    lead = await db.get(lead_id)
    if not lead:
        return {"error": "Lead not found", "lead_id": lead_id}

    is_blocked, suppress_reason = await check_lead_suppressed(db, lead)
    if is_blocked:
        return {
            "lead_id": lead_id,
            "name": lead.display_name,
            "status": "suppressed",
            "reason": f"previously {suppress_reason}",
        }

    result = await scrape_lead_email(lead, domain=domain)
    payload = result.to_dict()
    payload["name"] = lead.display_name
    payload["company"] = lead.company.name

    if apply and result.candidates:
        top = result.candidates[0]
        if not _is_confident_candidate(top):
            payload["applied"] = False
            payload["apply_skipped"] = True
            payload["apply_result"] = {
                "updated": False,
                "reason": (
                    "no confident candidate — top result is not a domain match "
                    "and was not found on a contact-ish page"
                ),
            }
            payload["reason"] = payload["apply_result"]["reason"]
        else:
            note = f"Scraped candidate from {top.page_url} (not verified — review before send)"
            update_result = await update_lead(
                db,
                lead_id,
                email=top.email,
                note=note,
            )
            payload["applied"] = update_result.get("updated", False)
            payload["apply_result"] = update_result
            if update_result.get("status") == "collision":
                payload["status"] = "collision"
                payload["reason"] = update_result.get("reason")

    return payload
