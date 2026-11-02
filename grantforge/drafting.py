"""Write the application for a shortlisted match.

Steps, in order:

1. **Requirements.** For a Grants.gov opportunity the funding notice (NOFO) and
   related documents are downloaded and read. The model extracts, once per
   opportunity, the required sections with their length limits, the review
   criteria, required forms and attachments, and formatting rules.
2. **Plan.** One call returns the outline, the requested amount, the budget by
   SF-424A object class, the checklist, open questions and the cover email.
3. **Sections.** Each section is written by its own call, so no single response
   has to hold the whole application. The shared material (applicant, profile,
   sources, requirements) is sent as a cached block so it is paid for at the
   cache-read price after the first call.
4. **Length limits.** Each section is measured against its limit. A section
   that is too long is written again from the beginning to fit; it is never
   cut or trimmed.
5. **Fact check.** Each section's factual claims are checked against the source
   material, and unsupported or contradicted claims are listed on the review page.
6. **Budget arithmetic** is done in code, never by the model.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from . import llm
from .config import settings
from .db import ApplicantProfile, Match, Opportunity, audit
from .export import write_package
from .ingest.profile import profile_as_text

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\[APPLICANT:[^\]]*\]")
BUDGET_CATEGORIES = [
    ("personnel", "a. Personnel"),
    ("fringe", "b. Fringe Benefits"),
    ("travel", "c. Travel"),
    ("equipment", "d. Equipment"),
    ("supplies", "e. Supplies"),
    ("contractual", "f. Contractual"),
    ("construction", "g. Construction"),
    ("other", "h. Other"),
    ("indirect", "j. Indirect Charges"),
]

REQUIREMENTS_SYSTEM = """You read funding notices (NOFOs, RFPs, calls for proposals) and extract exactly what an
application must contain. Use only what the text states.

Return JSON:
{
 "sections": [{"heading": str, "limit": {"unit": "words"|"characters"|"pages", "value": int} | null, "guidance": str}],
     # the narrative sections the funder requires, in the funder's order and with the funder's exact names.
     # If the text names no sections, return [].
 "review_criteria": [str],          # how applications are scored, with weights if stated
 "required_forms": [str],           # e.g. "SF-424", "SF-424A", "Project/Performance Site Location(s)"
 "required_attachments": [str],     # e.g. "Biographical sketch", "Letters of support", "Data management plan"
 "formatting": str,                 # font, margins, spacing, page size, file type rules
 "cost_sharing": str,               # cost share or match requirement, or "None stated"
 "submission_notes": str            # anything unusual about how or where to submit
}"""

PLAN_SYSTEM = """You are an experienced grant writer planning an application. The material above contains the
funding opportunity, its extracted requirements, the applicant, the project profile and the applicant's source
documents. Use only facts from that material.

If the requirements list sections, use exactly those sections, in that order, with those names and limits.
Otherwise use a standard structure (Project Summary; Statement of Need; Goals and Objectives; Project Description
and Approach; Timeline; Evaluation; Qualifications and Team; Budget Justification; Sustainability), dropping any
that the funder's description makes irrelevant.

The budget must be realistic for the award range and the project's stated needs. Put every cost in one SF-424A
object class. Amounts are whole US dollars. Do not compute totals; they are computed for you.

Return JSON:
{
 "title": str,
 "requested_amount_usd": int,
 "sections": [{"heading": str, "brief": str, "limit": {"unit": "words"|"characters"|"pages", "value": int} | null}],
     # brief = 2-5 sentences: what this section must accomplish for this funder and which facts to use
 "budget": {"personnel": [{"item": str, "amount": int}], "fringe": [...], "travel": [...], "equipment": [...],
            "supplies": [...], "contractual": [...], "construction": [...], "other": [...], "indirect": [...]},
 "checklist": [{"item": str, "status": "drafted" | "needs_applicant_data" | "needs_external_document", "note": str}],
 "gaps": [str],          # facts the funder asks for that the material does not contain
 "cover_email": {"subject": str, "body": str}
}"""

SECTION_SYSTEM = """You are an experienced grant writer writing one section of an application. The material above
contains the funding opportunity, its requirements, the applicant, the project profile and the applicant's source
documents.

Rules:
- Use only facts present in the material. Never invent numbers, names, results, partners or dates.
- When the funder asks for something the material does not contain, write [APPLICANT: what is needed] in place
  of the missing fact, so the applicant can see and fill it.
- Write plain, specific, declarative sentences. State every logical connection explicitly. No filler, no hype.
- Address the funder's review criteria that apply to this section, using the funder's own terms.
- Stay within the length limit given.
- Output only the body of the section in Markdown: no heading line for the section itself, no preamble."""

REWRITE_SYSTEM = SECTION_SYSTEM + """
- You are writing this section again from the beginning because the previous version exceeded the funder's limit.
  Compose a new, complete section that fits the limit with a clear margin. Do not cut or abbreviate the previous
  version; decide what matters most and write it fresh at the required length."""

FACT_CHECK_SYSTEM = """You check a grant application section against the applicant's source material above.
List every factual claim in the section (numbers, dates, names, results, capabilities, partnerships, status,
qualifications). For each, decide:
- "supported": the source material states it (quote the evidence, at most 200 characters)
- "unsupported": the source material does not state it
- "contradicted": the source material states something different (quote it)
- "placeholder": the claim is an [APPLICANT: ...] placeholder
Claims about the funder's own program that come from the funding notice count as supported.

Return a JSON array: [{"claim": str, "status": str, "evidence": str}]"""


# --------------------------------------------------------------------------
# Requirements
# --------------------------------------------------------------------------

def ensure_requirements(session: Session, opp: Opportunity, log_fn=print) -> dict:
    if opp.source == "grants_gov" and not opp.nofo_text:
        from .sources.grants_gov import fetch_nofo_text

        log_fn("Downloading the funding notice and related documents")
        try:
            opp.nofo_text = fetch_nofo_text(opp.raw or {}, log_fn)
        except Exception as exc:
            log_fn(f"  funding notice download failed: {exc}")
        if not opp.nofo_text:
            log_fn("  no readable funding notice; requirements will come from the synopsis only")
        session.commit()
    if opp.requirements:
        return opp.requirements
    text = (
        f"{opp.as_prompt_text(limit=20000)}\n\n"
        + (f"## Full funding notice and related documents\n{opp.nofo_text[:240000]}" if opp.nofo_text else
           "## Full funding notice\n(not available; use the description above)")
    )
    log_fn("Extracting the funder's requirements")
    req = llm.complete_json("extract_requirements", REQUIREMENTS_SYSTEM, text, max_tokens=4000)
    if not isinstance(req, dict):
        req = {}
    req.setdefault("sections", [])
    opp.requirements = req
    session.commit()
    return req


# --------------------------------------------------------------------------
# Context shared (and cached) across every call for one application
# --------------------------------------------------------------------------

def build_context(session: Session, match: Match) -> str:
    project, opp = match.project, match.opportunity
    applicant = session.get(ApplicantProfile, 1)
    parts = [
        "# FUNDING OPPORTUNITY\n" + opp.as_prompt_text(limit=15000),
        "# FUNDER REQUIREMENTS (extracted from the funding notice)\n" + json.dumps(opp.requirements or {}, indent=1),
        "# APPLICANT\n" + (applicant.as_prompt_text() if applicant else "{}"),
        "# PROJECT PROFILE\n" + profile_as_text(project.profile or {}),
    ]
    if project.notes.strip():
        parts.append("# APPLICANT NOTES\n" + project.notes.strip())
    used = sum(len(p) for p in parts)
    for doc in project.documents:
        if not doc.text.strip():
            continue
        block = f"# SOURCE DOCUMENT: {doc.name}\nSummary: {doc.summary}\n\nText:\n{doc.text[:30000]}"
        if used + len(block) > 260_000:
            block = f"# SOURCE DOCUMENT: {doc.name}\nSummary: {doc.summary}"
        parts.append(block)
        used += len(block)
    if opp.nofo_text:
        excerpt = opp.nofo_text[: max(0, 320_000 - used)]
        if excerpt:
            parts.append("# FUNDING NOTICE TEXT\n" + excerpt)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Length limits
# --------------------------------------------------------------------------

def measure(text: str, unit: str) -> float:
    words = len(re.findall(r"\S+", text))
    if unit == "words":
        return words
    if unit == "characters":
        return len(text)
    if unit == "pages":
        return round(words / max(settings.words_per_page, 1), 2)
    return 0


def _limit_text(limit: dict | None) -> str:
    if not limit or not limit.get("value"):
        return "No stated limit; write 250-600 words."
    unit = limit.get("unit", "words")
    if unit == "pages":
        return (f"At most {limit['value']} page(s), counted here as {limit['value'] * settings.words_per_page} words "
                f"at {settings.words_per_page} words per page.")
    return f"At most {limit['value']} {unit}."


def _over(text: str, limit: dict | None) -> tuple[bool, float]:
    if not limit or not limit.get("value"):
        return False, 0
    actual = measure(text, limit.get("unit", "words"))
    return actual > float(limit["value"]), actual


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------

def draft_match(session: Session, match: Match, log_fn=print) -> Match:
    project, opp = match.project, match.opportunity
    profile = project.profile or {}
    ensure_requirements(session, opp, log_fn)
    ctx = build_context(session, match)

    log_fn(f"Planning application for '{opp.title[:70]}'")
    plan = llm.complete_json("plan_application", PLAN_SYSTEM, "Plan the application now.",
                             max_tokens=6000, cached_context=ctx)
    if not isinstance(plan, dict) or not plan.get("sections"):
        raise RuntimeError("The planning step returned no sections")

    sections: list[dict] = []
    length_report: list[dict] = []
    for i, sec in enumerate(plan["sections"], 1):
        heading = str(sec.get("heading") or f"Section {i}")
        limit = sec.get("limit") if isinstance(sec.get("limit"), dict) else None
        log_fn(f"  writing section {i}/{len(plan['sections'])}: {heading}")
        body = _write_section(heading, sec.get("brief", ""), limit, sections, ctx)
        over, actual = _over(body, limit)
        rewrites = 0
        while over and rewrites < 2:
            rewrites += 1
            log_fn(f"    {heading} is {actual} {limit['unit']} against a limit of {limit['value']}; writing it again to fit")
            body = llm.complete_text(
                "rewrite_section", REWRITE_SYSTEM,
                f"SECTION TO WRITE: {heading}\nBrief: {sec.get('brief', '')}\nLength limit: {_limit_text(limit)}\n"
                f"Target: about {int(float(limit['value']) * 0.85)} {limit['unit'] if limit['unit'] != 'pages' else 'pages'}.\n\n"
                f"The previous version, for reference only (it was {actual} {limit['unit']}):\n{body}",
                max_tokens=settings.section_max_tokens, cached_context=ctx,
            ).strip()
            over, actual = _over(body, limit)
        if limit and limit.get("value"):
            length_report.append({"section": heading, "unit": limit.get("unit"), "limit": limit.get("value"),
                                  "actual": actual, "ok": not over, "rewrites": rewrites,
                                  "estimated": limit.get("unit") == "pages"})
        sections.append({"heading": heading, "body": body})

    budget = compute_budget(plan.get("budget") or {})
    gaps = [str(g) for g in plan.get("gaps") or []]
    requested = plan.get("requested_amount_usd")
    if budget["total"] and requested and int(requested) != budget["total"]:
        gaps.append(f"The plan stated ${int(requested):,} requested, but the budget lines add up to "
                    f"${budget['total']:,}; the budget total is used. Adjust the budget if this is wrong.")
    requested = budget["total"] or requested

    fact_check: list[dict] = []
    if settings.fact_check:
        for sec in sections:
            log_fn(f"  fact-checking: {sec['heading']}")
            try:
                items = llm.complete_json("fact_check_section", FACT_CHECK_SYSTEM,
                                          f"SECTION: {sec['heading']}\n\n{sec['body']}",
                                          max_tokens=4000, cached_context=ctx)
            except llm.LLMError as exc:
                items = [{"claim": f"(fact check could not run: {exc})", "status": "unsupported", "evidence": ""}]
            for it in items if isinstance(items, list) else []:
                fact_check.append({"section": sec["heading"], "claim": str(it.get("claim", "")),
                                   "status": str(it.get("status", "unsupported")), "evidence": str(it.get("evidence", ""))})

    checklist = list(plan.get("checklist") or [])
    req = opp.requirements or {}
    def listed(name: str) -> bool:
        # "SF-424" and "SF-424 Application for Federal Assistance" are the same item; "SF-424A" is not.
        key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        for c in checklist:
            other = re.sub(r"[^a-z0-9]+", " ", str(c.get("item", "")).lower()).strip()
            if key == other or re.search(rf"\b{re.escape(key)}\b", other) or re.search(rf"\b{re.escape(other)}\b", key):
                return True
        return False

    for form in req.get("required_forms") or []:
        if not listed(form):
            checklist.append({"item": form, "status": "needs_applicant_data", "note": "required form"})
    for att in req.get("required_attachments") or []:
        if not listed(att):
            checklist.append({"item": att, "status": "needs_external_document", "note": "required attachment"})
    for doc in project.documents:
        if doc.is_attachment:
            checklist.append({"item": f"Attachment: {doc.name}", "status": "drafted", "note": "included in package"})

    match.draft_sections = {
        "title": plan.get("title") or f"Application: {profile.get('project_name', project.name)}",
        "requested_amount_usd": requested,
        "sections": sections,
        "cover_email": plan.get("cover_email") or {},
        "fact_check_version": match.version + 1,
        "change": "redraft" if match.version else "draft",
    }
    match.budget = budget
    match.length_report = length_report
    match.fact_check = fact_check
    match.draft_checklist = checklist
    match.draft_markdown = render_markdown(match)
    match.draft_gaps = _merge_gaps(gaps, match.draft_markdown)
    match.version = (match.version or 0) + 1
    match.state = "drafted"
    session.commit()

    pkg_dir = settings.data_dir / "packages" / f"match_{match.id}"
    match.package_files = write_package(match, pkg_dir, match.draft_sections["change"])
    match.package_dir = str(pkg_dir)
    unsupported = sum(1 for f in fact_check if f["status"] in {"unsupported", "contradicted"})
    audit(session, "drafted", match.id, version=match.version, sections=len(sections), gaps=len(match.draft_gaps),
          unsupported_claims=unsupported, over_limit=sum(1 for r in length_report if not r["ok"]))
    session.commit()
    log_fn(f"  draft v{match.version} complete: {len(sections)} sections, {len(match.draft_gaps)} open questions, "
           f"{unsupported} claims not found in your material")
    return match


def _write_section(heading: str, brief: str, limit: dict | None, written: list[dict], ctx: str) -> str:
    previous = "\n\n".join(f"## {s['heading']}\n{s['body']}" for s in written)[-20000:]
    user = (f"SECTION TO WRITE: {heading}\nBrief: {brief}\nLength limit: {_limit_text(limit)}\n\n"
            + (f"Sections already written (for continuity; do not repeat them):\n{previous}" if previous else ""))
    try:
        return llm.complete_text("draft_section", SECTION_SYSTEM, user, max_tokens=settings.section_max_tokens,
                                 cached_context=ctx).strip()
    except llm.LLMTruncated:
        # The model ran past the token cap: ask for a complete, shorter section from scratch.
        return llm.complete_text(
            "draft_section:retry", SECTION_SYSTEM,
            user + "\n\nYour previous attempt was too long to finish. Write a complete version of at most 900 words.",
            max_tokens=settings.section_max_tokens, cached_context=ctx,
        ).strip()


def compute_budget(raw: dict) -> dict:
    """Sum the model's line items in code. Returns lines, per-category totals, direct, indirect and total."""
    lines: dict[str, list[dict]] = {}
    totals: dict[str, int] = {}
    for key, _label in BUDGET_CATEGORIES:
        items = []
        for it in raw.get(key) or []:
            try:
                amount = int(round(float(str(it.get("amount", 0)).replace(",", "").replace("$", ""))))
            except (TypeError, ValueError):
                continue
            items.append({"item": str(it.get("item", "")), "amount": amount})
        lines[key] = items
        totals[key] = sum(i["amount"] for i in items)
    direct = sum(v for k, v in totals.items() if k != "indirect")
    return {"lines": lines, "totals": totals, "direct": direct, "indirect": totals["indirect"],
            "total": direct + totals["indirect"]}


def _merge_gaps(gaps: list[str], markdown: str) -> list[str]:
    out = list(dict.fromkeys(gaps))
    for ph in dict.fromkeys(PLACEHOLDER_RE.findall(markdown)):
        out.append(f"Fill in {ph}")
    return out


def render_markdown(match: Match) -> str:
    d = match.draft_sections or {}
    opp = match.opportunity
    lines = [f"# {d.get('title', 'Application')}", ""]
    lines.append(f"**Funding opportunity:** {opp.title}  ")
    if (opp.raw or {}).get("number"):
        lines.append(f"**Opportunity number:** {opp.raw['number']}  ")
    lines.append(f"**Funder:** {opp.funder}  ")
    if opp.close_date:
        lines.append(f"**Deadline:** {opp.close_date}  ")
    if d.get("requested_amount_usd"):
        lines.append(f"**Amount requested:** ${int(d['requested_amount_usd']):,}  ")
    lines.append("")
    for sec in d.get("sections", []):
        lines += [f"## {sec.get('heading', '')}", "", str(sec.get("body", "")).strip(), ""]
    b = match.budget
    if b and b.get("total") and not any(s.get("heading") == "Budget Summary" for s in d.get("sections", [])):
        lines += ["## Budget Summary", "", "| Object class | Item | Amount |", "|---|---|---|"]
        for key, label in BUDGET_CATEGORIES:
            for it in b["lines"].get(key, []):
                lines.append(f"| {label} | {it['item']} | ${it['amount']:,} |")
        lines += [f"| | **Total direct** | **${b['direct']:,}** |", f"| | **Indirect** | **${b['indirect']:,}** |",
                  f"| | **Total** | **${b['total']:,}** |", ""]
    return "\n".join(lines)


def update_sections_from_markdown(match: Match, markdown: str) -> None:
    """Keep sections in sync when the user edits the Markdown, re-measure limits and placeholders,
    and write a new package version marked 'edited'."""
    d = dict(match.draft_sections or {"sections": []})
    sections: list[dict] = []
    current = None
    title = d.get("title", "Application")
    for line in markdown.splitlines():
        if line.startswith("# ") and not sections and current is None:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = {"heading": line[3:].strip(), "body": ""}
        elif current is not None:
            current["body"] += line + "\n"
    if current:
        sections.append(current)
    for s in sections:
        s["body"] = s["body"].strip()
    d["title"] = title
    d["sections"] = [s for s in sections if s["heading"] != "Budget Summary"]
    d["change"] = "edited"
    match.draft_sections = d
    match.draft_markdown = markdown
    limits = {r["section"]: r for r in match.length_report or []}
    new_report = []
    for s in sections:
        r = limits.get(s["heading"])
        if r:
            actual = measure(s["body"], r["unit"])
            new_report.append({**r, "actual": actual, "ok": actual <= float(r["limit"])})
    match.length_report = new_report
    plan_gaps = [g for g in (match.draft_gaps or []) if not g.startswith("Fill in [APPLICANT:")]
    match.draft_gaps = _merge_gaps(plan_gaps, markdown)
    match.version = (match.version or 0) + 1
    pkg_dir = Path(match.package_dir) if match.package_dir else settings.data_dir / "packages" / f"match_{match.id}"
    match.package_files = write_package(match, pkg_dir, "edited")
    match.package_dir = str(pkg_dir)
