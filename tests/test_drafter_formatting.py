"""Drafter post-processing — subject casing, sign-off strip, footer links."""

from __future__ import annotations

from leadgen.ai.drafter import OutreachDrafter
from leadgen.config.loader import OutreachConfig


def test_normalize_subject_sentence_case_fixes_title_case(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("Quick Question About ACME", sample_lead) == (
        "Quick question about ACME"
    )


def test_normalize_subject_sentence_case_preserves_acronyms(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("AI tools for B2B teams", sample_lead) == (
        "AI tools for B2B teams"
    )
    assert d._normalize_subject("Quick Question About ACME for Jane", sample_lead) == (
        "Quick question about ACME for Jane"
    )


def test_normalize_subject_sentence_case_preserves_prospect_name(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("Quick question, Jane", sample_lead) == "Quick question, Jane"
    assert d._normalize_subject("Quick Question, JANE", sample_lead) == "Quick question, Jane"


def test_normalize_subject_lowercase(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.subject_casing = "lowercase"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("Quick Question About ACME", sample_lead) == (
        "quick question about acme"
    )


def test_strip_model_signoff_removes_name_and_best(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.signature = "Best,\n{operator_name}"
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Main point here.\n\nBest,\nTester", sample_lead)
    assert body.count("Best,") == 1
    assert body.startswith("Main point here.")
    assert body.endswith("Tester")


def test_footer_links_include_booking_url(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.signature = "{operator_email}"
    test_config.outreach.booking_url = "https://cal.com/agentsia/discovery-call"
    test_config.outreach.footer_links = []
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Hello", sample_lead)
    assert "https://cal.com/agentsia/discovery-call" in body
    assert body.index("tester@example.com") < body.index("https://cal.com")
