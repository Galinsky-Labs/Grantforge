"""Add a funding opportunity by pasting its URL (or the full text of a call).

The page is fetched, reduced to text, and the model extracts the structured
record. Useful for foundation calls, RFPs sent by email, and any opportunity
the automated sources miss.
"""
from __future__ import annotations

import hashlib
import re
from html import unescape

import httpx

from .. import llm
from . import RawOpportunity

SYSTEM = """Extract the funding opportunity described in the text as JSON:
{
 "title": str, "funder": str, "close_date": "YYYY-MM-DD" | "rolling" | "unknown",
 "open_date": "YYYY-MM-DD" | "unknown",
 "award_floor": int|null, "award_ceiling": int|null,
 "applicant_types": [str], "eligibility_text": str,
 "description": str (a faithful 1-3 paragraph digest of purpose, priorities, required components and review criteria),
 "contact_email": str, "submission_channel": "portal"|"email"|"grants_gov"|"unknown",
 "submission_target": str (application URL or email address)
}
Use only what the text says. Unknown fields: "unknown", null or ""."""


def _html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def fetch_page_text(url: str) -> str:
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "GrantForge/0.1"}) as c:
        r = c.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            import io

            from pypdf import PdfReader

            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages)
        return _html_to_text(r.text)


def opportunity_from_url(url: str, pasted_text: str = "") -> RawOpportunity:
    text = pasted_text.strip() or fetch_page_text(url)
    data = llm.complete_json("parse_opportunity_page", SYSTEM, f"Source URL: {url}\n\n{text[:60000]}", max_tokens=3000)
    sid = hashlib.sha1(url.encode()).hexdigest()[:16]

    def _d(v):
        v = (v or "").strip() if isinstance(v, str) else v
        return "" if v in (None, "unknown", "null") else str(v)

    def _i(v):
        try:
            return int(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    return RawOpportunity(
        source="manual",
        source_id=sid,
        title=_d(data.get("title")) or url,
        funder=_d(data.get("funder")),
        url=url,
        status="posted",
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
    )
