"""Section-by-section drafting, length enforcement, fact checking, budget arithmetic and versioning."""
import json
from datetime import date, timedelta
from pathlib import Path

from grantforge.db import ApplicantProfile, Document, Match, Project
from grantforge.drafting import compute_budget, draft_match, measure, update_sections_from_markdown
from grantforge.llm import MockProvider
from grantforge.sources import RawOpportunity, upsert_opportunities


def _setup(session, with_attachment=False, tmp_path=None):
    a = session.get(ApplicantProfile, 1)
    a.legal_name, a.entity_type = "Jane Builder", "individual"
    p = Project(name="Demo", profile={"project_name": "Demo", "applicant_type": "individual"})
    session.add(p)
    session.flush()
    session.add(Document(project_id=p.id, kind="text", name="pitch.md", text="We built a thing.", char_count=17))
    if with_attachment:
        f = tmp_path / "letter_of_support.pdf"
        f.write_bytes(b"%PDF-1.4 letter")
        session.add(Document(project_id=p.id, kind="file", name=f.name, path=str(f), text="letter", char_count=6,
                             is_attachment=True))
    opp = upsert_opportunities(session, [RawOpportunity(
        source="stub", source_id="o1", title="Prototype Fund", funder="Agency", status="posted",
        close_date=(date.today() + timedelta(days=60)).isoformat(), applicant_types=["Individuals"],
        description="Funds prototypes.", raw={"number": "PF-2027-01"})])[0]
    m = Match(project_id=p.id, opportunity_id=opp.id, score=80, state="shortlisted")
    session.add(m)
    session.commit()
    return m


def test_sections_are_written_one_call_each_and_follow_the_plan(session):
    calls = []
    MockProvider.overrides["draft_section"] = lambda user: calls.append(user) or "Body text."
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    assert len(calls) == 3  # one per planned section
    assert "SECTION TO WRITE: Project Summary" in calls[0]
    assert "Sections already written" in calls[1] and "## Project Summary" in calls[1]
    assert [s["heading"] for s in m.draft_sections["sections"]] == ["Project Summary", "Project Narrative", "Budget Justification"]
    assert m.opportunity.requirements["review_criteria"] == ["Technical merit", "Feasibility"]


def test_over_limit_section_is_rewritten_from_scratch_not_trimmed(session):
    long_text = " ".join(["word"] * 400)  # Project Summary limit is 250 words
    MockProvider.overrides["draft_section"] = lambda user: long_text if "Project Summary" in user.split("\n")[0] else "Short body."
    rewrites = []
    MockProvider.overrides["rewrite_section"] = lambda user: rewrites.append(user) or " ".join(["fresh"] * 200)
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    summary = m.draft_sections["sections"][0]["body"]
    assert summary == " ".join(["fresh"] * 200)  # the new composition, not a cut of the old text
    assert "previous version, for reference only (it was 400 words)" in rewrites[0]
    report = {r["section"]: r for r in m.length_report}
    assert report["Project Summary"] == {"section": "Project Summary", "unit": "words", "limit": 250, "actual": 200,
                                         "ok": True, "rewrites": 1, "estimated": False}
    assert report["Project Narrative"]["estimated"] is True  # page limits are estimated from words


def test_section_still_over_after_two_rewrites_is_reported(session):
    MockProvider.overrides["draft_section"] = lambda user: " ".join(["word"] * 400)
    MockProvider.overrides["rewrite_section"] = lambda user: " ".join(["again"] * 300)
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    r = m.length_report[0]
    assert r["ok"] is False and r["rewrites"] == 2 and r["actual"] == 300
    checklist = Path(m.package_dir, m.package_files["checklist"]).read_text()
    assert "Sections over the funder's limit" in checklist and "Project Summary: 300 words against a limit of 250" in checklist


def test_fact_check_flags_unsupported_claims(session):
    MockProvider.overrides["fact_check_section"] = lambda user: json.dumps([
        {"claim": "We have 10,000 users.", "status": "unsupported", "evidence": ""},
        {"claim": "We built a thing.", "status": "supported", "evidence": "We built a thing."},
    ])
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    bad = [f for f in m.fact_check if f["status"] == "unsupported"]
    assert len(bad) == 3 and bad[0]["claim"] == "We have 10,000 users."  # one per section
    assert "Claims not found in your material" in Path(m.package_dir, m.package_files["checklist"]).read_text()


def test_budget_is_summed_in_code_and_plan_mismatch_is_reported(session):
    def plan(user):
        from grantforge.llm import MockProvider as M

        data = json.loads(M()._plan_application(user))
        data["requested_amount_usd"] = 99999  # the model's own total is wrong
        return json.dumps(data)

    MockProvider.overrides["plan_application"] = plan
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    assert m.budget["total"] == 65000 and m.budget["direct"] == 65000
    assert m.draft_sections["requested_amount_usd"] == 65000
    assert any("$99,999" in g and "$65,000" in g for g in m.draft_gaps)


def test_compute_budget_handles_strings_and_indirect():
    b = compute_budget({"personnel": [{"item": "PI", "amount": "$1,000"}], "indirect": [{"item": "10%", "amount": 100}],
                        "travel": [{"item": "bad", "amount": "n/a"}]})
    assert b["totals"]["personnel"] == 1000 and b["direct"] == 1000 and b["indirect"] == 100 and b["total"] == 1100
    assert b["lines"]["travel"] == []


def test_placeholders_become_open_questions(session):
    MockProvider.overrides["draft_section"] = lambda user: "Our team has [APPLICANT: number of staff] people."
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    assert "Fill in [APPLICANT: number of staff]" in m.draft_gaps


def test_edit_creates_new_version_files_and_keeps_old_ones(session, tmp_path):
    m = _setup(session, with_attachment=True, tmp_path=tmp_path)
    draft_match(session, m, lambda *_: None)
    v1 = dict(m.package_files)
    assert v1["attachments"] == ["attachments/letter_of_support.pdf"]
    edited = m.draft_markdown.replace("mock project summary", "edited summary")
    update_sections_from_markdown(m, edited)
    session.commit()
    assert m.version == 2
    assert m.package_files["docx"].startswith("PF-2027-01_v2-edited_")
    assert Path(m.package_dir, v1["docx"]).exists() and Path(m.package_dir, m.package_files["docx"]).exists()
    assert v1["docx"] != m.package_files["docx"]


def test_measure_units():
    assert measure("a b c", "words") == 3
    assert measure("abc", "characters") == 3
    assert measure(" ".join(["w"] * 1000), "pages") == 2.0


def test_checklist_does_not_repeat_forms_under_different_names(session):
    m = _setup(session)
    draft_match(session, m, lambda *_: None)
    items = [c["item"] for c in m.draft_checklist]
    assert items.count("SF-424") == 0 and "SF-424 Application for Federal Assistance" in items
    assert "SF-424A" in items  # a different form, so it is added
    assert "Resume of the principal investigator" in items
