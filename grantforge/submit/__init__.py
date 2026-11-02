"""Submission.

A match may only reach :func:`submit_match` after a human has pressed
Authorize (``match.authorized_at`` is set). The submitter is chosen from the
opportunity's ``submission_channel``:

* ``email``      – the application is emailed to the funder's stated address,
                   with the DOCX and Markdown attached.  Fully automatic.
* ``grants_gov``  – Grants.gov only accepts applications through Workspace (web)
                   or the System-to-System (S2S) SOAP interface, which requires an
                   organisation-level registration, an Authorized Organization
                   Representative, and an mTLS certificate registered with the
                   Grants.gov PMO.  Without S2S credentials the submitter produces
                   the complete upload-ready package and step-by-step instructions
                   and marks the match ``ready_for_manual_upload``.  With
                   credentials configured it hands off to :mod:`.grants_gov_s2s`.
* ``portal``      – foundation portals (Submittable, Fluxx, SmartSimple, …) have
                   no public API.  The submitter prepares the package and the
                   field-by-field answers, and marks ``ready_for_manual_upload``.
* ``unknown``     – same as ``portal``.

Every outcome is recorded on the match and in the audit table.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..db import Match, audit, utcnow
from .email_submitter import submit_by_email
from .manual_submitter import prepare_manual

log = logging.getLogger(__name__)


class NotAuthorized(RuntimeError):
    pass


def submit_match(session: Session, match: Match, log_fn=print) -> dict:
    if match.authorized_at is None:
        raise NotAuthorized("This application has not been authorized by the applicant.")
    if match.state == "submitted":
        return match.submission_result or {"status": "already_submitted"}
    channel = match.opportunity.submission_channel or "unknown"
    log_fn(f"Submitting match {match.id} via channel '{channel}'")
    try:
        if channel == "email":
            result = submit_by_email(match, log_fn)
        elif channel == "grants_gov":
            from .grants_gov_s2s import s2s_configured, submit_s2s

            if s2s_configured():
                result = submit_s2s(match, log_fn)
            else:
                result = prepare_manual(match, log_fn)
        else:
            result = prepare_manual(match, log_fn)
    except Exception as exc:
        log.exception("submission failed")
        match.state = "failed"
        match.submission_result = {"status": "failed", "error": str(exc)}
        audit(session, "submission_failed", match.id, error=str(exc))
        session.commit()
        raise
    match.submission_result = result
    if result.get("status") == "submitted":
        match.state = "submitted"
        match.submitted_at = utcnow()
    else:
        match.state = result.get("status", "ready_for_manual_upload")
    audit(session, "submission_" + result.get("status", "unknown"), match.id, **{k: v for k, v in result.items() if k != "status"})
    session.commit()
    return result
