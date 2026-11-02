"""Foundation, state, corporate and prize funding found through the model's
web-search tool. Covers what Grants.gov cannot: non-federal money.

Enabled with GF_ENABLE_WEB_SEARCH_SOURCE=true. Each run costs a handful of
searches; the model returns structured records with the page URL it read, and
:class:`ManualUrlSource` can re-parse any of those pages later for detail.
"""
from __future__ import annotations

import hashlib
import logging

from .. import llm
from ..config import settings
from . import RawOpportunity

log = logging.getLogger(__name__)

SYSTEM = """You are a grants researcher. Using web search, find CURRENTLY OPEN or soon-opening
funding opportunities (foundation grants, state programs, corporate programs, fellowships,
prizes, accelerators with non-dilutive awards) that fit the project described. Exclude
federal opportunities listed on Grants.gov; those are found separately. Exclude anything
whose deadline has passed and anything that is a loan or an equity investment.

Return a JSON array. Each element:
{
 "title": str, "funder": str, "url": str (the page you actually read),
 "close_date": "YYYY-MM-DD" or "rolling" or "unknown",
 "award_floor": int or null, "award_ceiling": int or null,
 "applicant_types": [str], "description": str (3-8 sentences, factual, from the page),
 "eligibility_text": str,
 "submission_channel": "portal" | "email" | "unknown",
 "submission_target": str (application URL or email address, or "")
}
Only include opportunities you actually found on a page. Never invent a URL or a deadline."""


class WebSearchSource:
    name = "web"

    def search(self, profile: dict, log_fn=print) -> list[RawOpportunity]:
        user = (
            "Project profile:\n" + _compact_profile(profile) +
            "\n\nFind up to 12 well-matched non-federal funding opportunities. Today is the current date."
        )
        log_fn("Web search: looking for foundation / state / prize funding")
        try:
            result = llm.complete_json("web_search_grants", SYSTEM, user, max_tokens=6000, web_search=True)
        except Exception as exc:
            log_fn(f"Web search source failed: {exc}")
            return []
        items = result if isinstance(result, list) else result.get("opportunities", []) if isinstance(result, dict) else []
        out = []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url or not it.get("title"):
                continue
            sid = hashlib.sha1(url.encode()).hexdigest()[:16]
            out.append(
                RawOpportunity(
                    source="web",
                    source_id=sid,
                    title=it.get("title", "")[:1000],
                    funder=it.get("funder", "")[:500],
                    url=url,
                    status="posted",
                    close_date=_norm_date(it.get("close_date")),
                    award_floor=_int(it.get("award_floor")),
                    award_ceiling=_int(it.get("award_ceiling")),
                    applicant_types=[str(a) for a in it.get("applicant_types") or []],
                    eligibility_text=it.get("eligibility_text", "") or "",
                    description=it.get("description", "") or "",
                    submission_channel=it.get("submission_channel") or "unknown",
                    submission_target=it.get("submission_target") or "",
                    raw=it,
                    verification="unverified",
                    verification_note="Reported by web search; not yet confirmed on the funder's page.",
                )
            )
        log_fn(f"Web search: {len(out)} opportunities; re-reading each funder page to confirm details")
        from .manual_url import verify_web_opportunity

        verified = [verify_web_opportunity(item, log_fn) for item in out]
        open_ = [v for v in verified if v.status != "closed"]
        log_fn(f"Web search: {sum(1 for v in open_ if v.verification == 'verified')} verified open, "
               f"{len(verified) - len(open_)} closed and dropped")
        return open_


def _compact_profile(profile: dict) -> str:
    keys = ["project_name", "one_line", "problem", "solution", "domains", "technology", "stage", "geography",
            "applicant_type", "funding_need_usd", "keywords"]
    return "\n".join(f"{k}: {profile.get(k)}" for k in keys if profile.get(k) is not None)


def _norm_date(v) -> str:
    if not v:
        return ""
    v = str(v).strip()
    return v if v not in {"unknown", "null"} else ""


def _int(v):
    try:
        return int(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None
