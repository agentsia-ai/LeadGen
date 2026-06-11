"""
PDL industry taxonomy + ICP-label → PDL-value mapping layer.

People Data Labs' ``industry`` / ``job_company_industry`` fields are an
``Enum (String)`` backed by a FIXED, LinkedIn-derived controlled vocabulary of
147 values (PDL canonical-data version 34.0, sourced from the public
``industry.txt`` enum file:
https://docs.peopledatalabs.com/docs/industries).

A query term that is NOT a literal member of that vocabulary matches zero
records → PDL returns 404. Historically the connector caught that 404 and
silently retried *without* the industry filter, so any human-readable ICP
label that wasn't already a verbatim PDL value (e.g. "insurance agencies",
"marketing agencies", "IT and managed services") effectively turned industry
targeting OFF and returned off-vertical leads.

This module is the mapping layer the connector now goes through:

* ``PDL_INDUSTRIES``          – the canonical 147-value set (the source of truth).
* ``ICP_INDUSTRY_ALIASES``    – human ICP labels → one or more valid PDL values,
                                so config.yaml can keep readable labels.
* ``resolve_industries``      – translate a list of ICP labels into PDL values.
* ``validate_and_resolve``    – same, but FAIL LOUDLY on any unmappable label
                                (naming the bad term + nearest valid value).

Caveat for broad mappings: several ICP labels are narrower than any PDL value
(e.g. "insurance agencies" → "insurance", which also includes carriers and
brokers). The industry filter alone cannot isolate those sub-segments; pair it
with company-size and keyword/title filters. Such mappings are annotated in
``BROADER_THAN_LABEL`` and surfaced in config comments.
"""

from __future__ import annotations

import difflib

# ── Canonical PDL industry vocabulary (v34.0, 147 values) ────────────────────
# Source of truth — do NOT edit casually. Refresh from PDL's published
# industry.txt enum when they bump canonical-data versions (quarterly).
PDL_INDUSTRIES: frozenset[str] = frozenset(
    {
        "accounting",
        "airlines/aviation",
        "alternative dispute resolution",
        "alternative medicine",
        "animation",
        "apparel & fashion",
        "architecture & planning",
        "arts and crafts",
        "automotive",
        "aviation & aerospace",
        "banking",
        "biotechnology",
        "broadcast media",
        "building materials",
        "business supplies and equipment",
        "capital markets",
        "chemicals",
        "civic & social organization",
        "civil engineering",
        "commercial real estate",
        "computer & network security",
        "computer games",
        "computer hardware",
        "computer networking",
        "computer software",
        "construction",
        "consumer electronics",
        "consumer goods",
        "consumer services",
        "cosmetics",
        "dairy",
        "defense & space",
        "design",
        "e-learning",
        "education management",
        "electrical/electronic manufacturing",
        "entertainment",
        "environmental services",
        "events services",
        "executive office",
        "facilities services",
        "farming",
        "financial services",
        "fine art",
        "fishery",
        "food & beverages",
        "food production",
        "fund-raising",
        "furniture",
        "gambling & casinos",
        "glass, ceramics & concrete",
        "government administration",
        "government relations",
        "graphic design",
        "health, wellness and fitness",
        "higher education",
        "hospital & health care",
        "hospitality",
        "human resources",
        "import and export",
        "individual & family services",
        "industrial automation",
        "information services",
        "information technology and services",
        "insurance",
        "international affairs",
        "international trade and development",
        "internet",
        "investment banking",
        "investment management",
        "judiciary",
        "law enforcement",
        "law practice",
        "legal services",
        "legislative office",
        "leisure, travel & tourism",
        "libraries",
        "logistics and supply chain",
        "luxury goods & jewelry",
        "machinery",
        "management consulting",
        "maritime",
        "market research",
        "marketing and advertising",
        "mechanical or industrial engineering",
        "media production",
        "medical devices",
        "medical practice",
        "mental health care",
        "military",
        "mining & metals",
        "motion pictures and film",
        "museums and institutions",
        "music",
        "nanotechnology",
        "newspapers",
        "non-profit organization management",
        "oil & energy",
        "online media",
        "outsourcing/offshoring",
        "package/freight delivery",
        "packaging and containers",
        "paper & forest products",
        "performing arts",
        "pharmaceuticals",
        "philanthropy",
        "photography",
        "plastics",
        "political organization",
        "primary/secondary education",
        "printing",
        "professional training & coaching",
        "program development",
        "public policy",
        "public relations and communications",
        "public safety",
        "publishing",
        "railroad manufacture",
        "ranching",
        "real estate",
        "recreational facilities and services",
        "religious institutions",
        "renewables & environment",
        "research",
        "restaurants",
        "retail",
        "security and investigations",
        "semiconductors",
        "shipbuilding",
        "sporting goods",
        "sports",
        "staffing and recruiting",
        "supermarkets",
        "telecommunications",
        "textiles",
        "think tanks",
        "tobacco",
        "translation and localization",
        "transportation/trucking/railroad",
        "utilities",
        "venture capital & private equity",
        "veterinary",
        "warehousing",
        "wholesale",
        "wine and spirits",
        "wireless",
        "writing and editing",
    }
)

# ── ICP label → valid PDL value(s) ───────────────────────────────────────────
# Keys are lowercased human-readable labels that may appear in an ICP config.
# Values are lists of VALID PDL taxonomy values (always members of
# PDL_INDUSTRIES). A label that already IS a PDL value does not need an entry
# here — resolution checks PDL_INDUSTRIES first.
ICP_INDUSTRY_ALIASES: dict[str, list[str]] = {
    # --- Tier 1: professional services ---
    "insurance agencies": ["insurance"],
    "insurance agency": ["insurance"],
    "accounting and bookkeeping": ["accounting"],
    "bookkeeping": ["accounting"],
    "property management": ["real estate"],
    "financial advisory": ["financial services"],
    "financial advisors": ["financial services"],
    "wealth management": ["financial services"],
    "marketing agencies": ["marketing and advertising"],
    "marketing agency": ["marketing and advertising"],
    "advertising agencies": ["marketing and advertising"],
    "it and managed services": ["information technology and services"],
    "managed services": ["information technology and services"],
    "msp": ["information technology and services"],
    "it services": ["information technology and services"],
    "law firm": ["law practice"],
    "law firms": ["law practice"],
    # --- Tier 2: home services (no native PDL home-services bucket) ---
    "cleaning services": ["facilities services"],
    "janitorial services": ["facilities services"],
    "home improvement and remodeling": ["construction"],
    "home improvement": ["construction"],
    "remodeling": ["construction"],
    "landscaping and lawn care": ["consumer services"],
    "landscaping": ["consumer services"],
    "lawn care": ["consumer services"],
    "home services": ["consumer services"],
    # --- Tier 3: trades ---
    "hvac and plumbing": ["construction"],
    "hvac": ["construction"],
    "plumbing": ["construction"],
    "roofing and gutters": ["construction"],
    "roofing": ["construction"],
    "pest control": ["consumer services"],
    # --- Other verticals to consider ---
    "healthcare clinics": ["medical practice"],
    "healthcare": ["hospital & health care"],
    "clinics": ["medical practice"],
    "restaurants and food service": ["restaurants"],
    "food service": ["restaurants"],
    "staffing and recruiting agencies": ["staffing and recruiting"],
    "recruiting": ["staffing and recruiting"],
    # --- Common shorthands / test fixtures ---
    "saas": ["computer software"],
    "software": ["computer software"],
    "fintech": ["financial services"],
    "tech": ["information technology and services"],
}

# Labels that resolve to a PDL value strictly BROADER than the intended
# segment. The industry filter alone cannot isolate the sub-segment; narrow
# with company-size and keyword/title filters. Used for warnings + config docs.
BROADER_THAN_LABEL: dict[str, str] = {
    "insurance agencies": "insurance (also includes carriers, brokers, reinsurers)",
    "property management": "real estate (also includes brokerages, developers, agents)",
    "financial advisory": "financial services (also includes banks, lenders, payments)",
    "cleaning services": "facilities services (broad building/facility ops)",
    "home improvement and remodeling": "construction (also includes commercial/heavy build)",
    "landscaping and lawn care": "consumer services (very broad consumer catch-all)",
    "home services": "consumer services (very broad consumer catch-all)",
    "hvac and plumbing": "construction (also includes commercial/heavy build)",
    "roofing and gutters": "construction (also includes commercial/heavy build)",
    "pest control": "consumer services (very broad consumer catch-all)",
}


def _norm(label: str) -> str:
    return label.strip().lower()


def suggest_pdl_value(label: str, n: int = 1) -> list[str]:
    """Return the nearest valid PDL value(s) for an unmappable label.

    Searches both the canonical vocabulary and the known alias keys so the
    suggestion is genuinely actionable ("did you mean 'insurance'?").
    """
    key = _norm(label)
    pool = list(PDL_INDUSTRIES) + list(ICP_INDUSTRY_ALIASES.keys())
    matches = difflib.get_close_matches(key, pool, n=n, cutoff=0.4)
    # Map any alias hit back to its PDL value(s) so we never suggest a
    # non-canonical term.
    out: list[str] = []
    for m in matches:
        if m in PDL_INDUSTRIES:
            out.append(m)
        else:
            out.extend(ICP_INDUSTRY_ALIASES.get(m, []))
    # Dedupe, preserve order.
    seen: set[str] = set()
    return [v for v in out if not (v in seen or seen.add(v))]


def resolve_industries(labels: list[str]) -> tuple[list[str], list[str]]:
    """Translate ICP labels into valid PDL taxonomy values.

    Returns ``(resolved, unresolved)`` where ``resolved`` is the deduped list
    of valid PDL values (order preserved) and ``unresolved`` is the list of
    original labels that are neither a canonical value nor a known alias.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()

    for label in labels:
        key = _norm(label)
        if key in PDL_INDUSTRIES:
            values = [key]
        elif key in ICP_INDUSTRY_ALIASES:
            values = ICP_INDUSTRY_ALIASES[key]
        else:
            unresolved.append(label)
            continue
        for v in values:
            if v not in seen:
                seen.add(v)
                resolved.append(v)

    return resolved, unresolved


class InvalidIndustryError(ValueError):
    """Raised when a configured ICP industry can't be mapped to PDL's taxonomy."""


def validate_and_resolve(labels: list[str]) -> list[str]:
    """Resolve ICP labels to PDL values, FAILING LOUDLY on any bad term.

    Raises:
        InvalidIndustryError: if any label is neither a canonical PDL value
            nor a known alias. The message names every offending term and its
            nearest valid PDL value so the config can be fixed quickly — rather
            than letting the term 404 and silently disable industry targeting.
    """
    resolved, unresolved = resolve_industries(labels)
    if unresolved:
        lines = []
        for bad in unresolved:
            suggestions = suggest_pdl_value(bad)
            hint = f" -> nearest valid PDL value: {', '.join(suggestions)!r}" if suggestions else (
                " -> no close PDL value found; pick one from PDL_INDUSTRIES"
            )
            lines.append(f"  - {bad!r}{hint}")
        raise InvalidIndustryError(
            "ICP industry term(s) are not valid PDL taxonomy values and would "
            "404 into a broadened (off-vertical) search. Fix config.yaml "
            "icp.industries or add a mapping in ICP_INDUSTRY_ALIASES:\n"
            + "\n".join(lines)
            + "\n(Valid values: PDL canonical 'industry' vocabulary, 147 entries - "
            "see leadgen/sources/pdl_industries.py / "
            "https://docs.peopledatalabs.com/docs/industries)"
        )
    return resolved
