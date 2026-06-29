"""Drafter post-processing — subject casing, sign-off strip, footer links."""

from __future__ import annotations

from leadgen.ai.drafter import OutreachDrafter
from leadgen.config.loader import OutreachConfig
from leadgen.outreach.email import plain_text_body_to_html


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


def test_normalize_subject_title_cases_lowercased_company_name(
    test_config, test_keys, sample_lead
) -> None:
    """Lead records store normalized lowercase company names — subject must title-case."""
    test_config.outreach.subject_casing = "sentence"
    sample_lead.company.name = "matrix realty group"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("matrix realty group admin hours back", sample_lead) == (
        "Matrix Realty Group admin hours back"
    )


def test_normalize_subject_downcases_model_shouted_common_company_words(
    test_config, test_keys, sample_lead
) -> None:
    """Model all-caps common words must not survive acronym preservation."""
    test_config.outreach.subject_casing = "sentence"
    sample_lead.company.name = "protect realty"
    sample_lead.company.display_name = None
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("PROTECT REALTY admin hours back", sample_lead) == (
        "Protect Realty admin hours back"
    )


def test_normalize_subject_casing_regression_guards(
    test_config, test_keys, sample_lead
) -> None:
    """Acronym, intercap, and single-letter-initial casing must stay intact together."""
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)

    sample_lead.company.name = "gtps insurance agency"
    sample_lead.company.display_name = "GTPS Insurance Agency"
    assert d._normalize_subject("GTPS insurance agency admin hours back", sample_lead) == (
        "GTPS Insurance Agency admin hours back"
    )

    sample_lead.company.name = "mchugh realty group"
    sample_lead.company.display_name = "McHugh Realty Group"
    assert d._normalize_subject("MCHUGH REALTY GROUP admin hours back", sample_lead) == (
        "McHugh Realty Group admin hours back"
    )

    sample_lead.company.name = "law offices of j. jeltes, ltd."
    sample_lead.company.display_name = None
    result = d._normalize_subject("LAW OFFICES OF J. JELTES admin hours back", sample_lead)
    assert "J. Jeltes" in result
    assert "J. jeltes" not in result
    assert "LAW OFFICES" not in result


def test_normalize_subject_capitalizes_single_letter_initials_in_company_name(
    test_config, test_keys, sample_lead
) -> None:
    """Single-letter initials with a period must title-case in deterministic fallback."""
    test_config.outreach.subject_casing = "sentence"
    sample_lead.company.name = "law offices of j. jeltes, ltd."
    sample_lead.company.display_name = None
    d = OutreachDrafter(test_config, test_keys)
    result = d._normalize_subject(
        "law offices of j. jeltes admin hours back", sample_lead
    )
    assert "J. Jeltes" in result
    assert "j. Jeltes" not in result


def test_normalize_subject_capitalizes_multiple_single_letter_initials(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.subject_casing = "sentence"
    sample_lead.company.name = "j. r. smith consulting"
    sample_lead.company.display_name = None
    d = OutreachDrafter(test_config, test_keys)
    result = d._normalize_subject("j. r. smith consulting admin time", sample_lead)
    assert "J. R. Smith" in result
    assert "j. r. Smith" not in result


def test_body_title_cases_lowercased_company_name(test_config, test_keys, sample_lead) -> None:
    """Full company phrase in body must use display casing from the lead record."""
    sample_lead.company.name = "matrix realty group"
    test_config.outreach.greeting_format = ""
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body(
        "Would a quick call be a fit for matrix realty group?",
        sample_lead,
    )
    assert "fit for Matrix Realty Group?" in body


def test_body_does_not_capitalize_generic_company_tokens(
    test_config, test_keys, sample_lead
) -> None:
    """Standalone tokens from a company name must not be altered in generic prose."""
    sample_lead.company.name = "gtps insurance agency"
    test_config.outreach.greeting_format = ""
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body(
        "Running an insurance agency means juggling renewals, claims, and client follow-ups.",
        sample_lead,
    )
    assert "insurance agency" in body
    assert "Insurance Agency" not in body


def test_body_restores_full_company_phrase_with_stored_casing(
    test_config, test_keys, sample_lead
) -> None:
    """Literal full company phrase in body is restored using display_name."""
    sample_lead.company.name = "gtps insurance agency"
    sample_lead.company.display_name = "GTPS Insurance Agency"
    test_config.outreach.greeting_format = ""
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body(
        "Would a quick call be worth it for gtps insurance agency?",
        sample_lead,
    )
    assert "for GTPS Insurance Agency?" in body


def test_display_company_name_uses_display_name_when_set(test_config, test_keys, sample_lead) -> None:
    sample_lead.company.name = "corfac international"
    sample_lead.company.display_name = "CORFAC International"
    d = OutreachDrafter(test_config, test_keys)
    assert d._display_company_name(sample_lead) == "CORFAC International"


def test_subject_renders_corfac_with_source_casing(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.subject_casing = "sentence"
    sample_lead.company.name = "corfac international"
    sample_lead.company.display_name = "CORFAC International"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject(
        "corfac international admin hours back", sample_lead
    ) == "CORFAC International admin hours back"


def test_subject_renders_luckytruck_with_source_casing(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.subject_casing = "sentence"
    sample_lead.company.name = "luckytruck"
    sample_lead.company.display_name = "LuckyTruck"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("luckytruck admin time back", sample_lead) == (
        "LuckyTruck admin time back"
    )


def test_display_company_name_preserves_stored_mixed_case(test_config, test_keys, sample_lead) -> None:
    sample_lead.company.name = "Matrix Realty Group"
    d = OutreachDrafter(test_config, test_keys)
    assert d._display_company_name(sample_lead) == "Matrix Realty Group"


def test_strip_em_dashes_from_subject(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)
    subject = d._normalize_subject(
        "TMG Real Estate Advisors — hours back on admin", sample_lead
    )
    assert "—" not in subject
    assert "–" not in subject
    assert subject == "TMG real estate advisors, hours back on admin"


def test_strip_en_dashes_from_subject(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.subject_casing = "sentence"
    d = OutreachDrafter(test_config, test_keys)
    subject = d._normalize_subject("Q1–Q2 admin savings", sample_lead)
    assert "–" not in subject
    assert subject == "Q1-q2 admin savings"


def test_strip_em_dashes_from_body(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.greeting_format = ""
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body(
        "Admin piles up fast — property managers feel it every week.",
        sample_lead,
    )
    assert "—" not in body
    assert "–" not in body
    assert "Admin piles up fast, property managers feel it every week." in body


def test_strip_en_dashes_from_body(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.greeting_format = ""
    test_config.outreach.signature = ""
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Week 1–3 is the hardest stretch.", sample_lead)
    assert "Week 1-3 is the hardest stretch." in body


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
    assert d._title_case_name("o'brien") == "O'Brien"
    assert d._title_case_name("mary jane") == "Mary Jane"


def test_title_case_name_handles_mc_and_mac_prefixes(test_config, test_keys) -> None:
    d = OutreachDrafter(test_config, test_keys)
    assert d._title_case_name("mchugh") == "McHugh"
    assert d._title_case_name("macdonald") == "MacDonald"
    assert d._title_case_name("smith") == "Smith"
    assert d._title_case_name("McHugh") == "McHugh"
    assert d._title_case_name("Mchugh") == "McHugh"


def test_subject_renders_mchugh_realty_with_intercap_last_name(
    test_config, test_keys, sample_lead
) -> None:
    test_config.outreach.subject_casing = "sentence"
    sample_lead.contact.first_name = "Rachel"
    sample_lead.contact.last_name = "McHugh"
    sample_lead.contact.full_name = "Rachel McHugh"
    sample_lead.company.name = "mchugh realty group"
    sample_lead.company.display_name = "McHugh Realty Group"
    d = OutreachDrafter(test_config, test_keys)
    assert d._normalize_subject("mchugh realty group admin hours back", sample_lead) == (
        "McHugh Realty Group admin hours back"
    )


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


def test_followup_body_renders_paragraph_breaks_like_initial(
    test_config, test_keys, sample_lead
) -> None:
    """Follow-up bodies with blank-line breaks use the same HTML render as initials."""
    test_config.outreach.greeting_format = "Hi {first_name},"
    test_config.outreach.signature = "{operator_email}"
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body(
        "Still thinking about admin time on your team.\n\n"
        "One thing teams like yours often miss: follow-ups slip when everyone is busy.\n\n"
        "Open to a quick 15-minute call?",
        sample_lead,
    )
    assert body.count("\n\n") >= 4
    html = plain_text_body_to_html(body)
    assert html.count("<p>") >= 5
    assert "</p><p>" in html


def test_footer_links_include_booking_url(test_config, test_keys, sample_lead) -> None:
    test_config.outreach.signature = "{operator_email}"
    test_config.outreach.booking_url = "https://cal.com/agentsia/discovery-call"
    test_config.outreach.footer_links = []
    d = OutreachDrafter(test_config, test_keys)
    body = d._format_body("Hello", sample_lead)
    assert "https://cal.com/agentsia/discovery-call" in body
    assert body.index("tester@example.com") < body.index("https://cal.com")
