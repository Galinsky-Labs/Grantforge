"""Build a structured Project Profile from all of a project's ingested material.

The profile is the single object every later stage reads: search queries come
from it, eligibility is judged against it, and the drafts are written from it.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from .. import llm
from ..db import ApplicantProfile, Document, Project

log = logging.getLogger(__name__)

PER_DOC_BUDGET = 40_000  # characters of each document shown to the summariser
PROFILE_CONTEXT_BUDGET = 150_000  # characters of combined material shown to the profile builder

SUMMARY_SYSTEM = (
    "You are a technical analyst preparing material for grant applications. "
    "Summarise the document for someone who must later explain this project to a funder. "
    "Keep every concrete fact: what it does, how it works, who it is for, stage of development, "
    "results, numbers, team, dates, intellectual property, and anything unusual or novel. "
    "Write dense prose, 300-600 words. Do not add facts that are not in the document."
)

PROFILE_SYSTEM = """You are a senior grant strategist. From the project material and the applicant
information, produce a structured project profile as JSON with exactly these keys:

{
  "project_name": str,
  "one_line": str,                       # one sentence, plain language
  "problem": str,                        # the problem the project addresses, 2-4 sentences
  "solution": str,                       # what the project is and how it works, 3-6 sentences
  "technology": [str],                   # specific technologies, methods, materials
  "stage": str,                          # idea | prototype | pilot | deployed | scaling
  "domains": [str],                      # funding domains, e.g. "renewable energy", "public health", "arts: documentary film"
  "beneficiaries": [str],
  "keywords": [str],                     # 10-25 single words or short phrases a funder's search index would use
  "geography": str,                      # where the work happens / who it serves
  "team": str,
  "funding_need_usd": {"min": int, "max": int},
  "funding_uses": [str],
  "evidence_of_traction": [str],         # results, users, awards, publications, letters of support
  "applicant_type": str,                 # copy from applicant info: individual | for_profit | small_business | nonprofit_501c3 | nonprofit_other | university | government | tribal
  "search_queries": [str]                # 8-15 short keyword queries (1-3 words each) to run against a grant database. Mix broad domain terms and specific technology terms. Never include the project's own name.
}

Rules: use only facts present in the material. If a fact is missing, write "unknown" rather than inventing it.
Think about every kind of funder that could plausibly care: federal agencies, state programs, foundations, prizes."""


def summarize_document(doc: Document) -> str:
    if not doc.text.strip():
        return ""
    user = f"Document name: {doc.name}\nKind: {doc.kind}\n\n{doc.text[:PER_DOC_BUDGET]}"
    return llm.complete_text("summarize_document", SUMMARY_SYSTEM, user, max_tokens=1500)


def build_profile(session: Session, project: Project, log_fn=print) -> dict:
    applicant = session.get(ApplicantProfile, 1)
    docs = [d for d in project.documents if d.text.strip()]
    if not docs and not project.notes.strip():
        raise RuntimeError("The project has no readable material yet. Upload documents, add a repository, or write notes.")

    # 1. Summarise each document (cached on the document row).
    for doc in docs:
        if not doc.summary:
            log_fn(f"Summarising {doc.name} ({doc.char_count:,} chars)")
            doc.summary = summarize_document(doc)
            session.commit()

    # 2. Assemble context: notes, then summaries, then raw excerpts while budget remains.
    parts = []
    if project.notes.strip():
        parts.append("## Notes written by the applicant\n" + project.notes.strip())
    for doc in docs:
        parts.append(f"## Summary of {doc.name}\n{doc.summary}")
    used = sum(len(p) for p in parts)
    for doc in docs:
        remaining = PROFILE_CONTEXT_BUDGET - used
        if remaining < 2000:
            break
        excerpt = doc.text[: min(remaining, 25_000)]
        parts.append(f"## Raw excerpt of {doc.name}\n{excerpt}")
        used += len(excerpt)

    user = (
        "## Applicant information\n" + (applicant.as_prompt_text() if applicant else "{}") + "\n\n" + "\n\n".join(parts)
    )
    log_fn("Building project profile")
    profile = llm.complete_json("build_profile", PROFILE_SYSTEM, user, max_tokens=4000)
    if not isinstance(profile, dict):
        raise RuntimeError("Profile builder returned a non-object")
    if applicant and applicant.entity_type:
        profile["applicant_type"] = applicant.entity_type
    profile.setdefault("search_queries", [])
    profile.setdefault("keywords", [])
    project.profile = profile
    project.status = "profiled"
    session.commit()
    log_fn(f"Profile built: {profile.get('project_name')} – {len(profile['search_queries'])} search queries")
    return profile


def profile_as_text(profile: dict) -> str:
    return json.dumps(profile, indent=1, ensure_ascii=False)
