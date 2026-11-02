"""Submission.

A match may only reach :func:`submit_match` after a human has pressed
Authorize (``match.authorized_at`` is set and the state is ``authorized``).
The submitter is chosen from the opportunity's ``submission_channel``:

* ``email``      – the application is emailed to the funder's stated address
                   with the DOCX, the budget CSV and the user's attachments.
* ``grants_gov`` – Grants.gov accepts applications only through Workspace (web)
                   or the System-to-System (S2S) interface, which needs an
                   organisation registration, an Authorized Organization
                   Representative and a registered client certificate. Without
                   S2S credentials the submitter produces the complete package,
                   an SF-424/SF-424A prefill file and step-by-step instructions,
                   and marks the match ``ready_for_manual_upload``.
* ``portal``/``unknown`` – no public API exists; same as above.

Exactly-once: the match is moved from ``authorized`` to ``submitting`` with a
single conditional UPDATE before anything is sent. A second worker, a double
click or a re-queued job finds the state already changed and sends nothing.
"""
from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..db import Match, audit, utcnow
from .email_submitter import submit_by_email
from .manual_submitter import prepare_manual

log = logging.getLogger(__name__)


class NotAuthorized(RuntimeError):
    pass


def claim_for_submission(session: Session, match: Match) -> bool:
    res = session.execute(
        update(Match).where(Match.id == match.id, Match.state == "authorized", Match.authorized_at.is_not(None))
        .values(state="submitting")
    )
    session.commit()
    session.refresh(match)
    return res.rowcount == 1


def submit_match(session: Session, match: Match, log_fn=print) -> dict:
    if match.authorized_at is None:
        raise NotAuthorized("This application has not been authorized by the applicant.")
    if match.state in {"submitted", "submitting"}:
        log_fn(f"Match {match.id} is already {match.state}; nothing sent")
        return match.submission_result or {"status": match.state}
    if not claim_for_submission(session, match):
        log_fn(f"Match {match.id} could not be claimed (state is {match.state}); nothing sent")
        return match.submission_result or {"status": match.state}
    channel = match.opportunity.submission_channel or "unknown"
    log_fn(f"Submitting match {match.id} (version {match.version}) via channel '{channel}'")
    try:
        if channel == "email":
            result = submit_by_email(match, log_fn)
        elif channel == "grants_gov":
            from .grants_gov_s2s import s2s_configured, submit_s2s

            result = submit_s2s(match, log_fn) if s2s_configured() else prepare_manual(match, log_fn)
        else:
            result = prepare_manual(match, log_fn)
    except Exception as exc:
        log.exception("submission failed")
        match.state = "failed"
        match.submission_result = {"status": "failed", "error": str(exc)}
        audit(session, "submission_failed", match.id, error=str(exc))
        session.commit()
        raise
    result["version"] = match.version
    match.submission_result = result
    if result.get("status") == "submitted":
        match.state = "submitted"
        match.submitted_at = utcnow()
    else:
        match.state = result.get("status", "ready_for_manual_upload")
    audit(session, "submission_" + result.get("status", "unknown"), match.id,
          **{k: v for k, v in result.items() if k not in {"status", "steps"}})
    session.commit()
    return result
