"""Base class customization tests.

Verify the two customization paths work for both AI classes:
  (1) Subclassing + overriding the class-level SYSTEM_PROMPT constant
  (2) Config-based prompt path override (ai.scorer_prompt_path,
      ai.drafter_prompt_path, ai.followup_prompt_path)

No network — we only verify prompt-resolution mechanics (which string the
instance ends up reading), not Claude call behavior.
"""

from __future__ import annotations

from pathlib import Path

from leadgen.ai.drafter import OutreachDrafter
from leadgen.ai.scorer import LeadScorer


# ── Subclass overrides ────────────────────────────────────────────────────────


def test_scorer_subclass_overrides_prompt(test_config, test_keys) -> None:
    """A downstream agent (e.g. Rex) should be able to subclass LeadScorer
    and set SYSTEM_PROMPT to get a custom persona without engine changes."""

    class MyScorer(LeadScorer):
        SYSTEM_PROMPT = "Custom scoring persona for MyCo."

    s = MyScorer(test_config, test_keys)
    assert s._system_prompt == "Custom scoring persona for MyCo."


def test_drafter_subclass_overrides_initial_prompt(test_config, test_keys) -> None:
    """Subclassing must work for the initial-outreach prompt."""

    class MyDrafter(OutreachDrafter):
        INITIAL_SYSTEM_PROMPT = "Custom initial-outreach voice."

    d = MyDrafter(test_config, test_keys)
    assert d._initial_prompt == "Custom initial-outreach voice."


def test_drafter_subclass_overrides_followup_prompt(test_config, test_keys) -> None:
    """Subclassing must also work for the follow-up prompt — personas often
    want a distinct tone for follow-ups vs. first touches."""

    class MyDrafter(OutreachDrafter):
        FOLLOWUP_SYSTEM_PROMPT = "Custom follow-up voice."

    d = MyDrafter(test_config, test_keys)
    assert d._followup_prompt == "Custom follow-up voice."


# ── Config-path overrides ─────────────────────────────────────────────────────


def test_scorer_config_path_override(test_config, test_keys, tmp_path: Path) -> None:
    """An operator can point ai.scorer_prompt_path at an external file and
    get a tuned prompt without any subclassing."""
    prompt_file = tmp_path / "custom_scorer.txt"
    prompt_file.write_text("Config-path scorer override.", encoding="utf-8")
    test_config.ai.scorer_prompt_path = str(prompt_file)

    s = LeadScorer(test_config, test_keys)
    assert s._system_prompt == "Config-path scorer override."


def test_drafter_config_path_override_initial(
    test_config, test_keys, tmp_path: Path
) -> None:
    """Config-path override for the initial drafter prompt."""
    prompt_file = tmp_path / "custom_initial.txt"
    prompt_file.write_text("Config-path initial override.", encoding="utf-8")
    test_config.ai.drafter_prompt_path = str(prompt_file)

    d = OutreachDrafter(test_config, test_keys)
    assert d._initial_prompt == "Config-path initial override."


def test_drafter_config_path_override_followup(
    test_config, test_keys, tmp_path: Path
) -> None:
    """Config-path override for the follow-up drafter prompt."""
    prompt_file = tmp_path / "custom_followup.txt"
    prompt_file.write_text("Config-path followup override.", encoding="utf-8")
    test_config.ai.followup_prompt_path = str(prompt_file)

    d = OutreachDrafter(test_config, test_keys)
    assert d._followup_prompt == "Config-path followup override."


# ── Missing-file fallback ─────────────────────────────────────────────────────


def test_scorer_config_path_missing_falls_back_to_class_default(
    test_config, test_keys, tmp_path: Path
) -> None:
    """If the configured prompt_path points at a non-existent file, the
    instance must fall back to SYSTEM_PROMPT rather than crash. (A hard
    crash here would brick every startup on a typo.)"""
    test_config.ai.scorer_prompt_path = str(tmp_path / "does_not_exist.txt")

    s = LeadScorer(test_config, test_keys)
    # Default prompt mentions the word "qualification"
    assert "qualification" in s._system_prompt.lower()


def test_drafter_config_path_missing_falls_back_to_class_default(
    test_config, test_keys, tmp_path: Path
) -> None:
    """Same missing-file fallback for both drafter prompt paths."""
    test_config.ai.drafter_prompt_path = str(tmp_path / "missing_initial.txt")
    test_config.ai.followup_prompt_path = str(tmp_path / "missing_followup.txt")

    d = OutreachDrafter(test_config, test_keys)
    # Default initial prompt mentions "cold" outreach; default follow-up
    # mentions "follow-up".
    assert "cold" in d._initial_prompt.lower()
    assert "follow-up" in d._followup_prompt.lower()


# ── Regression: class attributes must exist as strings ────────────────────────


def test_base_classes_expose_system_prompt_constants() -> None:
    """The pluggable seam relies on these being class-level string
    attributes (not instance-only or descriptors). If someone refactors
    these to @property or moves them onto __init__, every downstream
    subclass that sets them as plain strings breaks silently."""
    assert isinstance(LeadScorer.SYSTEM_PROMPT, str)
    assert isinstance(OutreachDrafter.INITIAL_SYSTEM_PROMPT, str)
    assert isinstance(OutreachDrafter.FOLLOWUP_SYSTEM_PROMPT, str)
