"""Write the application for a shortlisted match and build its package folder."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from . import llm
from .config import settings
from .db import ApplicantProfile, Match, audit
from .export import write_package
from .ingest.profile import profile_as_text

log = logging.getLogger(__name__)

DRAFT_SYSTEM = """You are an experienced grant writer. Write a complete, submission-quality application
for the funding opportunity, using ONLY facts from the project profile, the source excerpts and the
applicant information. Match the funder's language, priorities and review criteria closely. Where the
opportunity text names required sections, use those exact section names in that order; otherwise use a
standard structure (Project Summary; Statement of Need; Goals and Objectives; Project Description and
Approach; Timeline; Evaluation; Qualifications and Team; Budget and Budget Justification; Sustainability).

Writing rules:
- Plain, specific, declarative sentences. No filler, no hype words, no "cutting-edge".
- Every claim must be supported by the material. If something the funder asks for is not in the
  material, write the section anyway with a clearly marked placeholder like [APPLICANT: insert X] and
  list it in "gaps".
- The budget must be realistic for the award range and the project's stated funding needs, with
  line items and a total.
- Length: substantive. Narrative sections 200-600 words each unless the funder states limits.

Return JSON:
{
 "title": str,                                     # application title
 "requested_amount_usd": int,
 "sections": [{"heading": str, "body": str}],      # markdown allowed inside body
 "checklist": [{"item": str, "status": "drafted" | "needs_applicant_data" | "needs_external_document", "note": str}],
 "gaps": [str],                                    # questions the applicant must answer before submission
 "cover_email": {"subject": str, "body": str}      # used when the funder accepts applications by email
}"""


def draft_match(session: Session, match: Match, log_fn=print) -> Match:
    project = match.project
    opp = match.opportunity
    applicant = session.get(ApplicantProfile, 1)
    profile = project.profile or {}

    # Source excerpts: summaries plus the start of each document, within budget.
    excerpts = []
    used = 0
    for doc in project.documents:
        if not doc.text.strip():
            continue
        block = f"### {doc.name}\n{doc.summary}\n\nExcerpt:\n{doc.text[:12000]}"
        if used + len(block) > 90_000:
            break
        excerpts.append(block)
        used += len(block)
    if project.notes.strip():
        excerpts.insert(0, "### Applicant notes\n" + project.notes.strip())

    user = (
        f"## Funding opportunity\n{opp.as_prompt_text(limit=20000)}\n\n"
        f"## Applicant information\n{applicant.as_prompt_text() if applicant else '{}'}\n\n"
        f"## Project profile\n{profile_as_text(profile)}\n\n"
        f"## Source material\n" + "\n\n".join(excerpts)
    )
    log_fn(f"Drafting application for '{opp.title[:70]}'")
    data = llm.complete_json("draft_application", DRAFT_SYSTEM, user, max_tokens=settings.llm_max_tokens)

    sections = data.get("sections") or []
    match.draft_sections = {
        "title": data.get("title") or f"Application: {profile.get('project_name', project.name)}",
        "requested_amount_usd": data.get("requested_amount_usd"),
        "sections": sections,
        "cover_email": data.get("cover_email") or {},
    }
    match.draft_checklist = data.get("checklist") or []
    match.draft_gaps = [str(g) for g in data.get("gaps") or []]
    match.draft_markdown = render_markdown(match)
    match.state = "drafted"
    session.commit()

    pkg_dir = settings.data_dir / "packages" / f"match_{match.id}"
    write_package(match, pkg_dir)
    match.package_dir = str(pkg_dir)
    audit(session, "drafted", match.id, sections=len(sections), gaps=len(match.draft_gaps))
    session.commit()
    log_fn(f"  draft complete: {len(sections)} sections, {len(match.draft_gaps)} open questions")
    return match


def render_markdown(match: Match) -> str:
    d = match.draft_sections or {}
    opp = match.opportunity
    lines = [f"# {d.get('title', 'Application')}", ""]
    lines.append(f"**Funding opportunity:** {opp.title}  ")
    lines.append(f"**Funder:** {opp.funder}  ")
    if opp.close_date:
        lines.append(f"**Deadline:** {opp.close_date}  ")
    if d.get("requested_amount_usd"):
        lines.append(f"**Amount requested:** ${int(d['requested_amount_usd']):,}  ")
    lines.append("")
    for sec in d.get("sections", []):
        lines.append(f"## {sec.get('heading', '')}")
        lines.append("")
        lines.append(str(sec.get("body", "")).strip())
        lines.append("")
    return "\n".join(lines)


def update_sections_from_markdown(match: Match, markdown: str) -> None:
    """When the user edits the markdown in the review UI, keep sections in sync."""
    d = match.draft_sections or {"sections": []}
    sections = []
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
    d["sections"] = sections
    match.draft_sections = d
    match.draft_markdown = markdown
    pkg_dir = Path(match.package_dir) if match.package_dir else settings.data_dir / "packages" / f"match_{match.id}"
    write_package(match, pkg_dir)
    match.package_dir = str(pkg_dir)
