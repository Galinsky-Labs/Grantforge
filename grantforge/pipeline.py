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
    if matches is None:
        matches = session.scalars(select(Match).where(Match.project_id == project.id, Match.state == "drafted")).all()
    matches = [m for m in matches if m.state == "drafted"]
    if not matches:
        log_fn("Nothing new to notify about")
        return None
    if not settings.notify_to:
        log_fn("GF_NOTIFY_TO is not set; skipping email (drafts are visible in the dashboard)")
        return None
    result = notify_drafts_ready(matches, project.profile.get("project_name") if project.profile else project.name)
    for m in matches:
        m.state = "notified"
        m.notified_at = utcnow()
        audit(session, "notified", m.id)
    session.commit()
    log_fn(f"Notification sent to {settings.notify_to}")
    return result


def run_pipeline(session: Session, project: Project, log_fn=print, rebuild_profile: bool = False) -> dict:
    if rebuild_profile or not project.profile:
        run_profile(session, project, log_fn)
    opportunities = run_discover(session, project, log_fn)
    matches = run_score(session, project, opportunities, log_fn)
    drafted = run_draft(session, project, log_fn)
    run_notify(session, project, drafted, log_fn)
    return {
        "opportunities": len(opportunities),
        "shortlisted": sum(1 for m in matches if m.state in {"shortlisted", "drafted", "notified"}),
        "drafted": len(drafted),
    }
