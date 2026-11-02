"""Live checks with the real Claude model and real Grants.gov. Skipped without ANTHROPIC_API_KEY.

The end-to-end test runs the whole pipeline on a fictional sample project
(tests/live/fixtures/sample_project.md), drafts ONE application against a real
opportunity, and saves the package, the profile and the scores under
live-artifacts/ so the draft quality can be read.
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from grantforge import llm
from grantforge.config import settings
from grantforge.db import ApplicantProfile, Document, LLMUsage, Match, Project

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]
ART = Path(os.environ.get("GF_LIVE_ARTIFACTS", "live-artifacts"))
ART.mkdir(parents=True, exist_ok=True)


def test_basic_call_and_usage_recording(session):
    assert settings.llm_provider == "anthropic"
    out = llm.complete_text("smoke", "Answer with one word.", "Say the word: ready")
    assert "ready" in out.lower()
    row = session.query(LLMUsage).filter_by(task="smoke").one()
    assert row.input_tokens > 0 and row.output_tokens > 0 and row.cost_usd > 0


def test_web_search_tool_version_is_accepted(session, monkeypatch):
    monkeypatch.setattr(settings, "web_search_max_uses", 2)
    out = llm.complete_text("smoke_search", "Use web search once, then answer in one sentence.",
                            "What is the official website address of Grants.gov?", max_tokens=2000, web_search=True)
    assert "grants.gov" in out.lower()
    row = session.query(LLMUsage).filter_by(task="smoke_search").one()
    (ART / "web_search_smoke.json").write_text(json.dumps({"text": out, "searches": row.web_searches,
                                                           "tool": settings.web_search_tool_version}, indent=1))
    assert row.web_searches >= 1


def test_end_to_end_one_real_draft(session, monkeypatch):
    from grantforge import pipeline

    monkeypatch.setattr(settings, "max_drafts_per_run", 1)
    monkeypatch.setattr(settings, "max_candidates_per_query", 8)
    monkeypatch.setattr(settings, "match_threshold", 40)  # the fixture is fictional; we want one draft to read
    a = session.get(ApplicantProfile, 1)
    a.legal_name, a.entity_type, a.contact_name = "RiverWatch Test Applicant", "nonprofit_501c3", "Test Person"
    a.state, a.country = "CT", "USA"
    p = Project(name="RiverWatch (live test fixture)")
    session.add(p)
    session.flush()
    text = (Path(__file__).parent / "fixtures" / "sample_project.md").read_text()
    session.add(Document(project_id=p.id, kind="text", name="sample_project.md", text=text, char_count=len(text)))
    session.commit()

    logs: list[str] = []
    pipeline.run_profile(session, p, logs.append)
    p.profile = {**p.profile, "search_queries": p.profile.get("search_queries", [])[:3]}  # keep the run small
    session.commit()
    result = pipeline.run_pipeline(session, p, logs.append)
    (ART / "pipeline_log.txt").write_text("\n".join(logs))
    (ART / "profile.json").write_text(json.dumps(p.profile, indent=1))
    scores = [{"title": m.opportunity.title, "score": m.score, "state": m.state, "rationale": m.rationale,
               "blockers": m.blockers} for m in session.query(Match).filter_by(project_id=p.id).all()]
    (ART / "scores.json").write_text(json.dumps(scores, indent=1))
    spend = sum(r.cost_usd for r in session.query(LLMUsage).all())
    (ART / "spend.json").write_text(json.dumps({"usd": round(spend, 4), "result": result}, indent=1))

    assert p.profile.get("project_name")
    assert result["opportunities"] > 0, "Grants.gov returned nothing for the profile's queries"
    drafted = session.query(Match).filter(Match.project_id == p.id, Match.version > 0).all()
    assert drafted, f"nothing reached the drafting stage; scores: {scores[:5]}"
    m = drafted[0]
    assert len(m.draft_sections["sections"]) >= 2
    assert m.fact_check is not None
    shutil.copytree(m.package_dir, ART / "package", dirs_exist_ok=True)
    (ART / "requirements.json").write_text(json.dumps(m.opportunity.requirements, indent=1))
