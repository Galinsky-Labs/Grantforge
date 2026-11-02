"""Grants.gov source.

Uses the two public, key-less REST endpoints documented in the Grants.gov API
guide (https://www.grants.gov/api/api-guide):

    POST https://api.grants.gov/v1/api/search2
    POST https://api.grants.gov/v1/api/fetchOpportunity

Required attribution (Grants.gov API terms): "This product uses the Grants.gov
API but is not endorsed or certified by the U.S. Department of Health and Human
Services."  The UI footer carries this line.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from html import unescape

import httpx

from ..config import settings
from . import RawOpportunity

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.grants.gov/v1/api/search2"
FETCH_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
DETAIL_PAGE = "https://www.grants.gov/search-results-detail/{id}"

# Grants.gov "eligibilities" codes (search2 filter). Used only for reference in
# the UI; the authoritative check is the applicant-type text on each record.
ELIGIBILITY_CODES = {
    "00": "State governments",
    "01": "County governments",
    "02": "City or township governments",
    "04": "Special district governments",
    "05": "Independent school districts",
    "06": "Public and State controlled institutions of higher education",
    "07": "Native American tribal governments (Federally recognized)",
    "08": "Public housing authorities/Indian housing authorities",
    "11": "Native American tribal organizations (other than Federally recognized tribal governments)",
    "12": "Nonprofits having a 501(c)(3) status with the IRS, other than institutions of higher education",
    "13": "Nonprofits that do not have a 501(c)(3) status with the IRS, other than institutions of higher education",
    "20": "Private institutions of higher education",
    "21": "Individuals",
    "22": "For profit organizations other than small businesses",
    "23": "Small businesses",
    "25": "Others",
    "99": "Unrestricted",
}


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _to_int(v) -> int | None:
    if v in (None, "", "none", "None"):
        return None
    try:
        return int(float(str(v).replace(",", "").replace("$", "")))
    except ValueError:
        return None


def _iso(d: str) -> str:
    """Grants.gov returns MM/DD/YYYY; normalise to YYYY-MM-DD."""
    if not d:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return d


class GrantsGovClient:
    def __init__(self, client: httpx.Client | None = None, pause: float = 0.3):
        self._client = client or httpx.Client(timeout=30, headers={"User-Agent": "GrantForge/0.1 (+self-hosted)"})
        self._pause = pause

    def search(self, keyword: str, statuses: str = "posted|forecasted", rows: int = 50, start: int = 0, **filters) -> dict:
        body = {
            "rows": rows,
            "startRecordNum": start,
            "keyword": keyword,
            "oppNum": "",
            "eligibilities": filters.get("eligibilities", ""),
            "agencies": filters.get("agencies", ""),
            "oppStatuses": statuses,
            "aln": "",
            "fundingCategories": filters.get("fundingCategories", ""),
            "fundingInstruments": "",
            "sortBy": "closeDate|asc",
        }
        r = self._client.post(SEARCH_URL, json=body)
        r.raise_for_status()
        data = r.json()
        if data.get("errorcode", 0) not in (0, None):
            raise RuntimeError(f"Grants.gov search2 error: {data.get('msg')}")
        return data.get("data", {})

    def fetch(self, opportunity_id: str | int) -> dict:
        r = self._client.post(FETCH_URL, json={"opportunityId": int(opportunity_id)})
        r.raise_for_status()
        data = r.json()
        if data.get("errorcode", 0) not in (0, None):
            raise RuntimeError(f"Grants.gov fetchOpportunity error: {data.get('msg')}")
        time.sleep(self._pause)
        return data.get("data", {})


def normalise_detail(hit: dict, detail: dict) -> RawOpportunity:
    """Merge a search2 hit with its fetchOpportunity detail into a RawOpportunity."""
    syn = detail.get("synopsis") or detail.get("forecast") or {}
    is_forecast = not detail.get("synopsis") and bool(detail.get("forecast"))
    opp_id = str(detail.get("id") or hit.get("id"))
    title = detail.get("opportunityTitle") or hit.get("title") or ""
    agency = (
        syn.get("agencyName")
        or detail.get("agencyDetails", {}).get("agencyName")
        or hit.get("agencyName")
        or hit.get("agencyCode")
        or ""
    )
    applicant_types: list[str] = []
    for at in syn.get("applicantTypes") or []:
        if isinstance(at, dict):
            applicant_types.append(at.get("description") or at.get("id") or "")
        else:
            applicant_types.append(str(at))
    close = syn.get("responseDate") or syn.get("estApplicationResponseDate") or hit.get("closeDate") or ""
    open_ = syn.get("postingDate") or hit.get("openDate") or ""
    desc = _strip_html(syn.get("synopsisDesc") or syn.get("forecastDesc") or "")
    # Posted opportunities on Grants.gov are applied for through Grants.gov itself
    # unless the synopsis says otherwise; the matcher may override from the text.
    return RawOpportunity(
        source="grants_gov",
        source_id=opp_id,
        title=title,
        funder=agency,
        url=DETAIL_PAGE.format(id=opp_id),
        status="forecasted" if is_forecast else (hit.get("oppStatus") or "posted"),
        open_date=_iso(open_),
        close_date=_iso(close),
        award_floor=_to_int(syn.get("awardFloor")),
        award_ceiling=_to_int(syn.get("awardCeiling")),
        applicant_types=[a for a in applicant_types if a],
        eligibility_text=_strip_html(syn.get("applicantEligibilityDesc") or ""),
        description=desc,
        contact_email=syn.get("agencyContactEmail") or "",
        submission_channel="grants_gov",
        submission_target=DETAIL_PAGE.format(id=opp_id),
        raw={"hit": hit, "number": detail.get("opportunityNumber") or hit.get("number"), "cfda": hit.get("cfdaList"),
             "costSharing": syn.get("costSharing"), "numberOfAwards": syn.get("numberOfAwards"),
             "estimatedFunding": syn.get("estimatedFunding"), "fundingDescLinkUrl": syn.get("fundingDescLinkUrl"),
             "agencyContactName": syn.get("agencyContactName"), "agencyContactPhone": syn.get("agencyContactPhone"),
             "attachments": [a.get("fileName") for folder in (detail.get("synopsisAttachmentFolders") or []) for a in (folder.get("synopsisAttachments") or [])][:30]},
    )


class GrantsGovSource:
    name = "grants_gov"

    def __init__(self, client: GrantsGovClient | None = None):
        self.client = client or GrantsGovClient()

    def search(self, profile: dict, log_fn=print) -> list[RawOpportunity]:
        queries = list(dict.fromkeys(q.strip() for q in profile.get("search_queries", []) if q.strip()))
        if not queries:
            queries = profile.get("keywords", [])[:10]
        statuses = "posted|forecasted" if settings.include_forecasted else "posted"
        hits: dict[str, dict] = {}
        for q in queries:
            try:
                data = self.client.search(q, statuses=statuses, rows=settings.max_candidates_per_query)
            except Exception as exc:
                log_fn(f"Grants.gov search failed for '{q}': {exc}")
                continue
            got = data.get("oppHits", []) or []
            log_fn(f"Grants.gov '{q}': {data.get('hitCount', len(got))} hits (kept {len(got)})")
            for h in got:
                hits.setdefault(str(h.get("id")), h)
        log_fn(f"Grants.gov: {len(hits)} unique opportunities; fetching details")
        out: list[RawOpportunity] = []
        for i, (oid, hit) in enumerate(hits.items(), 1):
            try:
                detail = self.client.fetch(oid)
                out.append(normalise_detail(hit, detail))
            except Exception as exc:
                log_fn(f"  fetchOpportunity {oid} failed: {exc}")
            if i % 25 == 0:
                log_fn(f"  fetched {i}/{len(hits)}")
        return out
