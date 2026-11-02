"""End-to-end dry run with the mock LLM and a stubbed opportunity source."""
import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from grantforge import pipeline
from grantforge.config import settings
from grantforge.db import ApplicantProfile, AuditEvent, Document, Match, Opportunity, Project, utcnow
from grantforge.ingest.extract import extract_text, extract_zip
from grantforge.matching import hard_filter, score_project
from grantforge.sources import RawOpportunity, upsert_opportunities
from grantforge.submit import NotAuthorized, submit_match


def future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class StubSource:
    name = "stub"

    def search(self, profile, log_fn=print):
        return [
            RawOpportunity(source="stub", source_id="good", title="Technology Prototype Fund", funder="Example Agency",
                           url="https://example.gov/opp/1", status="posted", close_date=future(60),
                           award_floor=25000, award_ceiling=200000, applicant_types=["Individuals", "Small businesses"],
                           description="Funds working technology prototypes.", contact_email="apply@example.gov",
                           submission_channel="email", submission_target="apply@example.gov"),
            RawOpportunity(source="stub", source_id="expired", title="Expired Fund", funder="Old Agency",
                           url="https://example.gov/opp/2", status="posted", close_date=future(-3),
                           applicant_types=["Individuals"], description="Too late."),
            RawOpportunity(source="stub", source_id="wrongtype", title="University-only Fund", funder="Uni Agency",
                           url="https://example.gov/opp/3", status="posted", close_date=future(90),
                           applicant_types=["Public and State controlled institutions of higher education"],
                           description="Universities only."),
        ]


@pytest.fixture(autouse=True)
def stub_sources(monkeypatch):
    monkeypatch.setattr(pipeline, "all_sources", lambda _settings: [StubSource()])


def make_project(session) -> Project:
    a = session.get(ApplicantProfile, 1)
    a.legal_name, a.entity_type, a.contact_name = "Jane Builder", "individual", "Jane Builder"
    p = Project(name="Demo", notes="A prototype exists and works.")
    session.add(p)
    session.flush()
    session.add(Document(project_id=p.id, kind="text", name="pitch.md", text="# Demo\nWe built a thing.", char_count=24))
    session.commit()
    return p


def test_extract_zip_prioritises_readme():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("src/main.py", "print('hi')\n")
        zf.writestr("README.md", "# Project\nIt does X.\n")
        zf.writestr("node_modules/x/index.js", "ignored")
        zf.writestr("image.png", b"\x89PNG\x00\x00binary")
    path = Path(settings.data_dir) / "t.zip"
    path.write_bytes(buf.getvalue())
    text = extract_zip(path)
    assert text.index("README.md") < text.index("src/main.py")
    assert "node_modules" not in text and "image.png" not in text
    assert extract_text(path) == text


def test_hard_filter_rules(session):
    opp = Opportunity(source="x", source_id="1", title="t", status="posted", close_date=future(-1), applicant_types=["Individuals"])
    assert "deadline" in hard_filter(opp, "individual")
    opp.close_date = future(30)
    assert hard_filter(opp, "individual") is None
    assert "not among eligible types" in hard_filter(opp, "nonprofit_501c3")
    opp.applicant_types = ["Unrestricted (i.e., open to any type of entity above)"]
    assert hard_filter(opp, "nonprofit_501c3") is None
    opp.applicant_types = []
    assert hard_filter(opp, "government") is None
    opp.status = "closed"
    assert "closed" in hard_filter(opp, "government")


def test_full_pipeline_then_authorize_then_submit(session, capsys):
    p = make_project(session)
    logs = []
    result = pipeline.run_pipeline(session, p, logs.append)

    assert p.profile["project_name"] == "Mock Project"
    assert result["opportunities"] == 3
    matches = {m.opportunity.source_id: m for m in session.query(Match).all()}
    assert matches["expired"].state == "rejected" and "deadline" in matches["expired"].rationale
    assert matches["wrongtype"].state == "rejected"
    good = matches["good"]
    assert good.score == 78
    assert good.state == "notified"
    assert good.version == 1
    assert good.draft_markdown.startswith("# Mock Application")
    assert "## Project Narrative" in good.draft_markdown and "## Budget Summary" in good.draft_markdown
    files = good.package_files
    assert files["docx"].startswith("Technology-Prototype-Fund_v1-draft_") and files["docx"].endswith("w_application.docx")
    assert files["budget_csv"] == "Technology-Prototype-Fund_v1-draft_budget-$65000.csv"
    for key in ("docx", "md", "json", "checklist", "budget_csv"):
        assert Path(good.package_dir, files[key]).is_file()

    out = capsys.readouterr().out
    assert "applicant@example.com" in out and f"/review/{good.id}?t=" in out

    with pytest.raises(NotAuthorized):
        submit_match(session, good, logs.append)

    good.authorized_at = utcnow()
    good.authorized_by = "Jane Builder"
    good.state = "authorized"
    session.commit()
    res = submit_match(session, good, logs.append)
    assert res["status"] == "submitted" and res["channel"] == "email" and res["recipient"] == "apply@example.gov"
    assert res["version"] == 1
    assert good.state == "submitted" and good.submitted_at is not None
    events = [e.event for e in session.query(AuditEvent).filter_by(match_id=good.id).all()]
    assert events[:3] == ["shortlisted", "drafted", "notified"] and events[-1] == "submission_submitted"
    outbox = sorted(Path(settings.data_dir, "outbox").glob("*.eml"))
    last = outbox[-1].read_bytes().decode("utf-8", "ignore")
    assert "To: apply@example.gov" in last and files["docx"] in last and "budget-$65000.csv" in last


def test_submission_is_sent_exactly_once(session):
    p = make_project(session)
    pipeline.run_pipeline(session, p, lambda *_: None)
    good = session.query(Match).filter(Match.state == "notified").one()
    good.authorized_at, good.state = utcnow(), "authorized"
    session.commit()
    before = len(list(Path(settings.data_dir, "outbox").glob("*.eml")))
    submit_match(session, good, lambda *_: None)
    second = submit_match(session, good, lambda *_: None)  # double click / duplicate job
    after = len(list(Path(settings.data_dir, "outbox").glob("*.eml")))
    assert after - before == 1
    assert second["status"] == "submitted"


def test_manual_channel_prepares_package(session):
    p = make_project(session)
    pipeline.run_profile(session, p, lambda *_: None)
    opp = upsert_opportunities(session, [RawOpportunity(
        source="stub", source_id="fed", title="Federal Thing", funder="Agency", status="posted", close_date=future(40),
        applicant_types=["Individuals"], description="x", submission_channel="grants_gov",
        raw={"number": "ABC-123", "cfda": ["10.001"]})])[0]
    m = score_project(session, p, [opp], lambda *_: None)[0]
    assert m.opportunity.submission_channel == "grants_gov"  # the source's channel beats the scorer's guess
    pipeline.run_draft(session, p, lambda *_: None)
    m.authorized_at, m.state = utcnow(), "authorized"
    session.commit()
    res = submit_match(session, m, lambda *_: None)
    assert res["status"] == "ready_for_manual_upload"
    prefill = json.loads(Path(m.package_dir, m.package_files["sf424"]).read_text())
    assert m.package_files["sf424"].startswith("ABC-123_v1-submit_")
    assert prefill["12_funding_opportunity_number"] == "ABC-123" and prefill["8a_legal_name"] == "Jane Builder"
    assert prefill["sf424a_total"] == 65000 and prefill["sf424a_section_b_object_classes"]["personnel"] == 60000
    assert Path(m.package_dir, m.package_files["instructions"]).exists()
    assert m.state == "ready_for_manual_upload"


def test_forecast_is_held_then_drafted_when_posted(session):
    p = make_project(session)
    pipeline.run_profile(session, p, lambda *_: None)
    raw = RawOpportunity(source="stub", source_id="fc", title="Forecast Fund", funder="A", status="forecasted",
                         close_date=future(120), applicant_types=["Individuals"], description="soon")
    opp = upsert_opportunities(session, [raw])[0]
    score_project(session, p, [opp], lambda *_: None)
    pipeline.run_draft(session, p, lambda *_: None)
    m = session.query(Match).filter_by(opportunity_id=opp.id).one()
    assert m.state == "held_forecast" and not m.draft_markdown
    raw.status = "posted"
    opp = upsert_opportunities(session, [raw])[0]
    score_project(session, p, [opp], lambda *_: None)
    assert m.state == "shortlisted"
    pipeline.run_draft(session, p, lambda *_: None)
    assert m.state == "drafted" and m.version == 1
