"""Add a funding opportunity by pasting its URL (or the full text of a call),
and verify opportunities that web search reported.

The page is fetched through :func:`grantforge.net.safe_get` (public addresses
only), reduced to text, and the model extracts the structured record from
the page text itself, so deadlines and eligibility come from the funder's
page rather than from a search summary.
"""
from __future__ import annotations

import hashlib
import io
import re
from html import unescape

import httpx

from .. import llm
from ..net import safe_get
from . import RawOpportunity

SYSTEM = """Extract the funding opportunity described in the text as JSON:
{
 "title": str, "funder": str, "close_date": "YYYY-MM-DD" | "rolling" | "unknown",
 "open_date": "YYYY-MM-DD" | "unknown",
 "award_floor": int|null, "award_ceiling": int|null,
 "applicant_types": [str], "eligibility_text": str,
 "description": str (a faithful 1-3 paragraph digest of purpose, priorities, required components and review criteria),
 "contact_email": str, "submission_channel": "portal"|"email"|"grants_gov"|"unknown",
 "submission_target": str (application URL or email address),
 "is_open": bool (true only if the text shows the call is currently accepting or will accept applications)
}
Use only what the text says. Unknown fields: "unknown", null or ""."""


def html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def response_to_text(url: str, r: httpx.Response) -> str:
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf") or r.content[:4] == b"%PDF":
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages)
    return html_to_text(r.text)


def fetch_page_text(url: str) -> str:
    return response_to_text(url, safe_get(url))


def _d(v) -> str:
    v = v.strip() if isinstance(v, str) else v
    return "" if v in (None, "unknown", "null") else str(v)


def _i(v):
    try:
        return int(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def parse_page(url: str, text: str) -> dict:
    return llm.complete_json("parse_opportunity_page", SYSTEM, f"Source URL: {url}\n\n{text[:60000]}", max_tokens=3000)


def opportunity_from_url(url: str, pasted_text: str = "") -> RawOpportunity:
    text = pasted_text.strip() or fetch_page_text(url)
    data = parse_page(url, text)
    sid = hashlib.sha1(url.encode()).hexdigest()[:16]
    return RawOpportunity(
        source="manual",
        source_id=sid,
        title=_d(data.get("title")) or url,
        funder=_d(data.get("funder")),
        url=url,
        status="posted" if data.get("is_open", True) else "closed",
        open_date=_d(data.get("open_date")),
        close_date=_d(data.get("close_date")),
        award_floor=_i(data.get("award_floor")),
        award_ceiling=_i(data.get("award_ceiling")),
        applicant_types=[str(a) for a in data.get("applicant_types") or []],
        eligibility_text=_d(data.get("eligibility_text")),
        description=_d(data.get("description")) or text[:5000],
        contact_email=_d(data.get("contact_email")),
        submission_channel=_d(data.get("submission_channel")) or "unknown",
        submission_target=_d(data.get("submission_target")),
        raw={"page_text_chars": len(text)},
        verification="verified" if not pasted_text.strip() else "unverified",
        verification_note="Details read from the funder's page." if not pasted_text.strip()
        else "Details read from pasted text; check them against the funder's page.",
    )


def verify_web_opportunity(item: RawOpportunity, log_fn=print) -> RawOpportunity:
    """Re-read a web-search result from its own page and overwrite the model-reported facts."""
    try:
        text = fetch_page_text(item.url)
    except Exception as exc:
        item.verification = "failed"
        item.verification_note = f"Could not open {item.url} to confirm the details: {exc}"
        log_fn(f"  verify failed: {item.url}: {exc}")
        return item
    if len(text.strip()) < 200:
        item.verification = "unverified"
        item.verification_note = "The funder's page had too little readable text to confirm the details."
        return item
    data = parse_page(item.url, text)
    item.title = _d(data.get("title")) or item.title
    item.funder = _d(data.get("funder")) or item.funder
    item.close_date = _d(data.get("close_date"))
    item.open_date = _d(data.get("open_date"))
    item.award_floor = _i(data.get("award_floor"))
    item.award_ceiling = _i(data.get("award_ceiling"))
    item.applicant_types = [str(a) for a in data.get("applicant_types") or []]
    item.eligibility_text = _d(data.get("eligibility_text"))
    item.description = _d(data.get("description")) or item.description
    item.contact_email = _d(data.get("contact_email"))
    item.submission_channel = _d(data.get("submission_channel")) or item.submission_channel
    item.submission_target = _d(data.get("submission_target")) or item.submission_target
    if data.get("is_open") is False:
        item.status = "closed"
    item.verification = "verified"
    item.verification_note = "Deadline, award and eligibility were re-read from the funder's page."
    return item
