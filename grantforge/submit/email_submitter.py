"""Email the application to the funder."""
from __future__ import annotations

import re
from pathlib import Path

from ..config import settings
from ..db import ApplicantProfile, Match
from ..notify import send_email

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _target_address(match: Match) -> str:
    opp = match.opportunity
    for candidate in (opp.submission_target, opp.contact_email):
        m = EMAIL_RE.search(candidate or "")
        if m:
            return m.group(0)
    raise RuntimeError("No funder email address is known for this opportunity; set one on the review page.")


def submit_by_email(match: Match, log_fn=print) -> dict:
    if not settings.allow_email_submission:
        return {"status": "ready_for_manual_upload", "reason": "GF_ALLOW_EMAIL_SUBMISSION is false"}
    to = _target_address(match)
    cover = (match.draft_sections or {}).get("cover_email") or {}
    subject = cover.get("subject") or f"Application: {(match.draft_sections or {}).get('title', 'Grant application')}"
    body = cover.get("body") or "Please find the attached application."
    pkg = Path(match.package_dir)
    attachments = [p for p in (pkg / "application.docx", pkg / "application.md") if p.exists()]

    from ..db import SessionLocal

    with SessionLocal() as s:
        applicant = s.get(ApplicantProfile, 1)
    reply_to = applicant.contact_email if applicant and applicant.contact_email else None

    if settings.submission_test_mode:
        test_to = settings.submission_test_recipient or settings.notify_to
        log_fn(f"TEST MODE: would send to {to}; sending to {test_to} instead")
        result = send_email(test_to, f"[TEST – would go to {to}] {subject}", body, attachments, reply_to=reply_to)
        return {"status": "submitted", "channel": "email", "test_mode": True, "intended_recipient": to, **result}

    log_fn(f"Emailing application to {to}")
    result = send_email(to, subject, body, attachments, reply_to=reply_to)
    return {"status": "submitted", "channel": "email", "recipient": to, **result}
