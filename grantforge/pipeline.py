"""The end-to-end run: profile → discover → score → draft → notify.

Submission is deliberately NOT part of the pipeline. It only happens from the
review page, after the applicant presses Authorize.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import Match, Opportunity, Project, audit, utcnow
from .drafting import draft_match
from .ingest.profile import build_profile
from .matching import score_project
from .notify import notify_drafts_ready
from .sources import all_sources, upsert_opportunities

log = logging.getLogger(__name__)


def run_profile(session: Session, project: Project, log_fn=print) -> dict:
    return build_profile(session, project, log_fn)


def run_discover(session: Session, project: Project, log_fn=print) -> list[Opportunity]:
    if not project.profile:
        run_profile(session, project, log_fn)
    found: list[Opportunity] = []
    for source in all_sources(settings):
        log_fn(f"Searching source: {source.name}")
        try:
            raws = source.search(project.profile, log_fn)
        except Exception as exc:
            log_fn(f"Source {source.name} failed: {exc}")
            continue
        rows = upsert_opportunities(session, raws)
        found.extend(rows)
        log_fn(f"{source.name}: {len(rows)} opportunities stored")
    # Also re-score any manually added opportunities not yet matched to this project.
    manual = session.scalars(select(Opportunity).where(Opportunity.source == "manual")).all()
    have = {o.id for o in found}
    found.extend(o for o in manual if o.id not in have)
    project.status = "searched"
    session.commit()
    return found


def run_score(session: Session, project: Project, opportunities: list[Opportunity], log_fn=print) -> list[Match]:
    log_fn(f"Scoring {len(opportunities)} opportunities")
    return score_project(session, project, opportunities, log_fn)


def run_draft(session: Session, project: Project, log_fn=print, limit: int | None = None) -> list[Match]:
    limit = limit or settings.max_drafts_per_run
    shortlisted = session.scalars(
        select(Match).where(Match.project_id == project.id, Match.state == "shortlisted").order_by(Match.score.desc())
    ).all()
    drafted: list[Match] = []
    ready = []
    for m in shortlisted:
        if m.opportunity.status == "forecasted":
            # A forecast has no application package or final notice yet; draft it once it is posted.
            m.state = "held_forecast"
            audit(session, "held_forecast", m.id)
            log_fn(f"Holding forecasted opportunity until it opens: '{m.opportunity.title[:60]}'")
        else:
            ready.append(m)
    session.commit()
    shortlisted = ready
    for m in shortlisted[:limit]:
        try:
            drafted.append(draft_match(session, m, log_fn))
        except Exception as exc:
            log_fn(f"Drafting failed for match {m.id}: {exc}")
            log.exception("draft failed")
    if shortlisted[limit:]:
        log_fn(f"{len(shortlisted) - limit} more shortlisted matches remain undrafted (GF_MAX_DRAFTS_PER_RUN={limit}); run Draft again to continue")
    project.status = "drafted" if drafted else project.status
    session.commit()
    return drafted


def run_notify(session: Session, project: Project, matches: list[Match] | None = None, log_fn=print) -> dict | None:
    """Email the review links. A failure to send (no email service, SMTP blocked by the host) is logged and
    leaves the drafts in the 'drafted' state, visible on the dashboard and notified on a later run; it does not
    fail the job that wrote them."""
    from .notify import email_unavailable_reason

    if matches is None:
        matches = session.scalars(select(Match).where(Match.project_id == project.id, Match.state == "drafted")).all()
    matches = [m for m in matches if m.state == "drafted"]
    if not matches:
        log_fn("Nothing new to notify about")
        return None
    reason = email_unavailable_reason()
    if reason:
        log_fn(f"Email is not available ({reason}); the {len(matches)} draft(s) are on the dashboard and will be "
               "emailed on a later run once email works")
        return None
    try:
        result = notify_drafts_ready(matches, project.profile.get("project_name") if project.profile else project.name)
    except Exception as exc:
        log.exception("notification email failed")
        log_fn(f"The notification email could not be sent ({exc}); the {len(matches)} draft(s) are on the dashboard")
        return None
    for m in matches:
        m.state = "notified"
        m.notified_at = utcnow()
        audit(session, "notified", m.id)
    session.commit()
    log_fn(f"Notification sent to {settings.notify_to}")
    return result


def run_pipeline(session: Session, project: Project, log_fn=print, rebuild_profile: bool = False,
                 checkpoint: dict | None = None, save_checkpoint=None) -> dict:
    """Profile → discover → score → draft → notify.

    ``checkpoint`` is the job's saved progress ({"done": [stage, ...], "opportunity_ids": [...]}). Each finished
    stage is recorded through ``save_checkpoint(checkpoint)`` (which commits it to the database), so a run that is
    interrupted (a deploy, a restart, a free instance going to sleep) is resumed by the worker from the first
    unfinished stage. Scoring and drafting are themselves resumable: scored matches are not scored again, drafted
    matches leave the shortlist, and a half-written draft continues from its last finished section.
    """
    cp = checkpoint if checkpoint is not None else {}
    done: list[str] = list(cp.get("done") or [])

    def mark(stage: str, **extra) -> None:
        done.append(stage)
        cp["done"] = list(done)
        cp.update(extra)
        if save_checkpoint:
            save_checkpoint(cp)

    if done:
        log_fn(f"Resuming an interrupted run; already finished: {', '.join(done)}")
    if "profile" not in done:
        if rebuild_profile or not project.profile:
            run_profile(session, project, log_fn)
        mark("profile")
    if "discover" not in done:
        opportunities = run_discover(session, project, log_fn)
        mark("discover", opportunity_ids=[o.id for o in opportunities])
    else:
        ids = cp.get("opportunity_ids") or []
        opportunities = session.scalars(select(Opportunity).where(Opportunity.id.in_(ids))).all() if ids else []
    matches = run_score(session, project, opportunities, log_fn)
    if "score" not in done:
        mark("score")
    drafted = run_draft(session, project, log_fn)
    mark("draft")
    # Notify every draft of this project not yet emailed, including drafts finished before an interruption.
    run_notify(session, project, None, log_fn)
    mark("notify")
    return {
        "opportunities": len(opportunities),
        "shortlisted": sum(1 for m in matches if m.state in {"shortlisted", "drafted", "notified"}),
        "drafted": len(drafted),
    }
