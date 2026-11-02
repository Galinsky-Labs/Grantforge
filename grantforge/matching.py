"""Score opportunities against a project.

Two stages:
1. Cheap hard filters (deadline passed, applicant type plainly excluded) so we
   never spend model tokens on obvious misses.
2. Model scoring (0-100) with a written rationale and explicit blockers.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .config import settings
from .db import Match, Opportunity, Project, audit
from .ingest.profile import profile_as_text

log = logging.getLogger(__name__)

# Which Grants.gov applicant-type phrases admit which of our entity types.
ENTITY_PHRASES = {
    "individual": ["individual"],
    "for_profit": ["for profit", "for-profit", "small business"],
    "small_business": ["small business", "for profit", "for-profit"],
    "nonprofit_501c3": ["nonprofit", "non-profit"],
    "nonprofit_other": ["nonprofit", "non-profit"],
    "university": ["higher education", "university", "college"],
    "government": ["government", "state", "county", "city"],
    "tribal": ["tribal", "native american"],
}
OPEN_PHRASES = ["unrestricted", "others", "other", "see text", "additional information on eligibility", "anyone"]

SCORE_SYSTEM = """You are a grants strategist who has reviewed thousands of applications. Judge how well
this funding opportunity fits the project. Be strict about eligibility and honest about fit:
a funder's stated priorities matter more than keyword overlap.

Return JSON:
{
 "score": int 0-100,          # 85+: apply, strong fit. 65-84: apply, decent fit. 40-64: weak. <40: no.
 "eligible": bool,            # false if the applicant type, geography, or a stated requirement rules it out
 "rationale": str,            # 3-6 sentences a busy applicant can act on
 "blockers": [str],           # concrete things that would need to be true/obtained before applying (e.g. "requires 501(c)(3) status", "requires SAM.gov registration", "requires a university partner")
 "submission_channel": "grants_gov" | "email" | "portal" | "unknown",
 "submission_target": str     # email address or application URL if the text states one, else ""
}"""


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def hard_filter(opp: Opportunity, applicant_type: str) -> str | None:
    """Return a reason string if the opportunity is plainly out, else None."""
    close = _parse_date(opp.close_date)
    if close is not None and close < date.today() + timedelta(days=settings.min_days_to_deadline):
        return f"deadline {opp.close_date} is past or within {settings.min_days_to_deadline} days"
    types = [t.lower() for t in (opp.applicant_types or [])]
    if types:
        if any(any(p in t for p in OPEN_PHRASES) for t in types):
            return None
        phrases = ENTITY_PHRASES.get(applicant_type, [])
        if phrases and not any(any(p in t for p in phrases) for t in types):
            return f"applicant type '{applicant_type}' not among eligible types: {', '.join(opp.applicant_types)[:200]}"
    return None


def score_project(session: Session, project: Project, opportunities: list[Opportunity], log_fn=print) -> list[Match]:
    profile = project.profile or {}
    applicant_type = profile.get("applicant_type", "individual")
    profile_text = profile_as_text(profile)
    results: list[Match] = []
    skipped = 0
    for i, opp in enumerate(opportunities, 1):
        match = session.scalar(select(Match).where(Match.project_id == project.id, Match.opportunity_id == opp.id))
        if match is not None and match.state not in {"candidate"}:
            results.append(match)  # already shortlisted/drafted/rejected: leave it alone
            continue
        if match is None:
            match = Match(project_id=project.id, opportunity_id=opp.id)
            session.add(match)
        reason = hard_filter(opp, applicant_type)
        if reason:
            match.score, match.eligible, match.rationale, match.state = 0, False, f"Filtered: {reason}", "rejected"
            match.blockers = [reason]
            skipped += 1
            continue
        try:
            data = llm.complete_json(
                "score_opportunity",
                SCORE_SYSTEM,
                f"## Project profile\n{profile_text}\n\n## Funding opportunity\n{opp.as_prompt_text()}",
                max_tokens=1200,
            )
        except Exception as exc:
            log_fn(f"  scoring failed for '{opp.title[:60]}': {exc}")
            continue
        match.score = int(data.get("score", 0) or 0)
        match.eligible = bool(data.get("eligible", True))
        match.rationale = str(data.get("rationale", ""))
        match.blockers = [str(b) for b in data.get("blockers") or []]
        ch = data.get("submission_channel") or "unknown"
        if opp.submission_channel in {"unknown", ""} and ch != "unknown":
            opp.submission_channel = ch
        if not opp.submission_target and data.get("submission_target"):
            opp.submission_target = str(data["submission_target"])
        if match.eligible and match.score >= settings.match_threshold:
            match.state = "shortlisted"
        else:
            match.state = "rejected"
        results.append(match)
        if i % 10 == 0:
            session.commit()
            log_fn(f"  scored {i}/{len(opportunities)}")
    session.commit()
    shortlisted = [m for m in results if m.state == "shortlisted"]
    log_fn(f"Scoring done: {len(shortlisted)} shortlisted, {skipped} filtered out without a model call")
    for m in shortlisted:
        audit(session, "shortlisted", m.id, score=m.score)
    session.commit()
    return results
