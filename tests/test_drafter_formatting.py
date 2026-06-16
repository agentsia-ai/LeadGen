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


def test_normalize_subject_sentence_case_preserves_multi_word_company(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)
    sample_lead.company.name = "Matrix Realty"
    assert d._normalize_subject("Save matrix realty hours weekly", sample_lead) == (
        "Save Matrix Realty hours weekly"
    )


def test_deterministic_greeting_prepended(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = "{operator_email}"
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Saw Acme is hiring SDRs.", sample_lead)
    assert body.startswith("Hi Jane,\n\nSaw Acme is hiring SDRs.")


def test_greeting_title_cases_lowercased_first_name(test_config, test_keys, sample_lead) -> None:
    """Lead records store normalized lowercase names — greeting must title-case."""
    sample_lead.contact.first_name = "anthony"
    sample_lead.contact.last_name = "baumer"
    sample_lead.contact.full_name = "anthony baumer"
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Property managers often lose hours to admin.", sample_lead)
    assert body.startswith("Hi Anthony,")


def test_title_case_name_handles_hyphen_and_apostrophe(test_config, test_keys) -> None:
    d = OutreachDrafter(test_config, test_keys)
    assert d._title_case_name("jean-pierre") == "Jean-Pierre"
    assert d._title_case_name("o'connor") == "O'Connor"
    assert d._title_case_name("mary jane") == "Mary Jane"


def test_body_restores_title_cased_prospect_name(test_config, test_keys, sample_lead) -> None:
    sample_lead.contact.first_name = "anthony"
    test_config.outreach.greeting_format = ""
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Quick note for anthony about admin time.", sample_lead)
    assert "for Anthony about" in body


def test_model_greeting_stripped_before_deterministic_greeting(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Hi Jane,\n\nSaw Acme is hiring SDRs.", sample_lead)
    assert body.startswith("Hi Jane,\n\nSaw Acme is hiring SDRs.")
    assert body.count("Hi Jane,") == 1


def test_runon_model_greeting_stripped(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Hi Jane, Saw Acme is hiring SDRs.", sample_lead)
    assert body == "Hi Jane,\n\nSaw Acme is hiring SDRs."


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
    assert body.startswith("Jane,\n\nMain point here.")
    assert body.endswith("Tester")


def test_footer_links_include_booking_url(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.signature = "{operator_email}"
    test_config.outreach.booking_url = "https://cal.com/agentsia/discovery-call"
    test_config.outreach.footer_links = []
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Hello", sample_lead)
    assert "https://cal.com/agentsia/discovery-call" in body
    assert body.index("tester@example.com") < body.index("https://cal.com")
