"""EmailSender.send_test_draft — dry-run / mocked SMTP, no real sends."""

from __future__ import annotations

import pytest

from leadgen.outreach.email import EmailSender


@pytest.mark.asyncio
async def test_send_test_draft_prefixes_subject_and_skips_daily_limit(
    test_config, test_keys, initialized_db, monkeypatch
) -> None:
    captured: dict = {}

    async def fake_send(self, to_email, to_name, subject, body):
        captured.update(
            to_email=to_email, to_name=to_name, subject=subject, body=body
        )
        return True

    monkeypatch.setattr("leadgen.outreach.email.EmailSender._send", fake_send)

    sender = EmailSender(test_config, test_keys, initialized_db, dry_run=False)
    ok = await sender.send_test_draft(
        subject="Intro",
        body="Line one\n\nLine two",
        test_recipient="operator@test.com",
        to_name="Jane Doe",
    )

    assert ok is True
    assert captured["to_email"] == "operator@test.com"
    assert captured["subject"] == "[TEST] Intro"
    assert captured["body"] == "Line one\n\nLine two"
    assert sender._sent_today == 0
