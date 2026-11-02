"""End-to-end dry run with the mock LLM and a stubbed Grants.gov source."""
import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from grantforge import pipeline
from grantforge.config import settings
from grantforge.db import ApplicantProfile, AuditEvent, Document, Match, Opportunity, Project
from grantforge.ingest.extract import extract_text, extract_zip
from grantforge.matching import hard_filter
from grantforge.sources import RawOpportunity, upsert_opportunities
from grantforge.submit import NotAuthorized, submit_match


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class StubSource:
    name = "stub"

    def search(self, profile, log_fn=print):
        return [
            RawOpportunity(source="stub", source_id="good", title="Technology Prototype Fund", funder="Example Agency",
                           url="https://example.gov/opp/1", status="posted", close_date=_future(60),
                           award_floor=25000, award_ceiling=200000, applicant_types=["Individuals", "Small businesses"],
                           description="Funds working technology prototypes.", contact_email="apply@example.gov",
                           submission_channel="email", submission_target="apply@example.gov"),
            RawOpportunity(source="stub", source_id="expired", title="Expired Fund", funder="Old Agency",
                           url="https://example.gov/opp/2", status="posted", close_date=_future(-3),
                           applicant_types=["Individuals"], description="Too late."),
            RawOpportunity(source="stub", source_id="wrongtype", title="University-only Fund", funder="Uni Agency",
                           url="https://example.gov/opp/3", status="posted", close_date=_future(90),
                           applicant_types=["Public and State controlled institutions of higher education"],
                           description="Universities only."),
        ]


@pytest.fixture(autouse=True)
def stub_sources(monkeypatch):
    monkeypatch.setattr(pipeline, "all_sources", lambda _settings: [StubSource()])


def _project(session) -> Project:
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
    opp = Opportunity(source="x", source_id="1", title="t", close_date=_future(-1), applicant_types=["Individuals"])
    assert "deadline" in hard_filter(opp, "individual")
    opp.close_date = _future(30)
    assert hard_filter(opp, "individual") is None
    assert "not among eligible types" in hard_filter(opp, "nonprofit_501c3")
    opp.applicant_types = ["Unrestricted (i.e., open to any type of entity above)"]
    assert hard_filter(opp, "nonprofit_501c3") is None
    opp.applicant_types = []
    assert hard_filter(opp, "government") is None


def test_full_pipeline_then_authorize_then_submit(session, capsys):
    p = _project(session)
    logs = []
    result = pipeline.run_pipeline(session, p, logs.append)

    assert p.profile["project_name"] == "Mock Project"
    assert result["opportunities"] == 3
    matches = {m.opportunity.source_id: m for m in session.query(Match).all()}
    assert matches["expired"].state == "rejected" and "deadline" in matches["expired"].rationale
    assert matches["wrongtype"].state == "rejected"
    good = matches["good"]
    assert good.score == 78
    assert good.state == "notified"  # drafted and emailed
    assert good.draft_markdown.startswith("# Mock Application")
    assert Path(good.package_dir, "application.docx").exists()
    assert Path(good.package_dir, "CHECKLIST.md").read_text().startswith("# Submission checklist")

    out = capsys.readouterr().out
    assert "applicant@example.com" in out and f"/review/{good.id}?t=" in out

    # Submission is refused until a human authorizes.
    with pytest.raises(NotAuthorized):
        submit_match(session, good, logs.append)

    from grantforge.db import utcnow

    good.authorized_at = utcnow()
    good.authorized_by = "Jane Builder"
    session.commit()
    res = submit_match(session, good, logs.append)
    assert res["status"] == "submitted" and res["channel"] == "email" and res["recipient"] == "apply@example.gov"
    assert good.state == "submitted" and good.submitted_at is not None
    events = [e.event for e in session.query(AuditEvent).filter_by(match_id=good.id).all()]
    assert events[:3] == ["shortlisted", "drafted", "notified"] and events[-1] == "submission_submitted"
    # The actual outgoing email went to the funder address with the DOCX attached.
    outbox = sorted(Path(settings.data_dir, "outbox").glob("*.eml"))
    last = outbox[-1].read_bytes().decode("utf-8", "ignore")
    assert "To: apply@example.gov" in last and "application.docx" in last


def test_manual_channel_prepares_package(session):
    p = _project(session)
    pipeline.run_profile(session, p, lambda *_: None)
    opp = upsert_opportunities(session, [RawOpportunity(
        source="stub", source_id="fed", title="Federal Thing", funder="Agency", status="posted", close_date=_future(40),
        applicant_types=["Individuals"], description="x", submission_channel="grants_gov",
        raw={"number": "ABC-123", "cfda": ["10.001"]})])[0]
    from grantforge.matching import score_project

    m = score_project(session, p, [opp], lambda *_: None)[0]
    # mock scorer says channel=email but the source already knows it is grants_gov; source wins
    assert m.opportunity.submission_channel == "grants_gov"
    pipeline.run_draft(session, p, lambda *_: None)
    from grantforge.db import utcnow

    m.authorized_at = utcnow()
    session.commit()
    res = submit_match(session, m, lambda *_: None)
    assert res["status"] == "ready_for_manual_upload"
    prefill = json.loads(Path(m.package_dir, "sf424_prefill.json").read_text())
    assert prefill["12_funding_opportunity_number"] == "ABC-123" and prefill["8a_legal_name"] == "Jane Builder"
    assert Path(m.package_dir, "SUBMIT_INSTRUCTIONS.md").exists()
    assert m.state == "ready_for_manual_upload"
