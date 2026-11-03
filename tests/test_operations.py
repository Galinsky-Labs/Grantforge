"""Migrations, spend tracking, backups, reminders, job claiming, OCR, URL safety, web-search verification."""
import tarfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text

from grantforge import llm, maintenance
from grantforge.config import settings
from grantforge.db import AuditEvent, Job, LLMUsage, Match, Opportunity, Project, utcnow
from grantforge.llm import BudgetExceeded, Usage, cost_usd
from grantforge.migrate import alembic_config, upgrade_to_head
from grantforge.net import UnsafeURL, check_url


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

def test_v01_database_is_upgraded_in_place(tmp_path):
    from alembic import command

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:  # build exactly the v0.1 schema
        command.upgrade(alembic_config(eng, conn), "0001")
    with eng.begin() as conn:  # make it look like v0.1 made it: no alembic_version, one project row
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("INSERT INTO projects (id, name, notes, status, created_at, updated_at) "
                          "VALUES (1, 'Old project', '', 'new', '2026-10-01', '2026-10-01')"))
    upgrade_to_head(eng)
    insp = inspect(eng)
    assert "llm_usage" in insp.get_table_names() and "web_sessions" in insp.get_table_names()
    assert "is_attachment" in {c["name"] for c in insp.get_columns("documents")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT name FROM projects")).scalar() == "Old project"
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"


def test_models_match_migrations(tmp_path):
    """Fails if a model changes without a new migration."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from grantforge.db import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_to_head(eng)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


# --------------------------------------------------------------------------
# Spend tracking
# --------------------------------------------------------------------------

def test_cost_uses_verified_prices():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000,
              web_searches=100)
    assert cost_usd("claude-sonnet-5-5", u) == pytest.approx(2.00 + 10.00 + 2.50 + 0.10 + 1.00)
    small = Usage(input_tokens=50_000, output_tokens=1_000_000)
    assert cost_usd("claude-haiku-5-5", small) == pytest.approx(0.005 + 0.50)
    large = Usage(input_tokens=200_000)
    assert cost_usd("claude-haiku-5-5", large) == pytest.approx(0.10)


def test_monthly_cap_blocks_calls(session, monkeypatch):
    monkeypatch.setattr(settings, "monthly_budget_usd", 1.0)
    session.add(LLMUsage(task="x", model="claude-sonnet-5-5", cost_usd=1.5))
    session.commit()
    with pytest.raises(BudgetExceeded):
        llm.complete_text("build_profile", "s", "u")
    assert maintenance.budget_notice() is True
    assert maintenance.budget_notice() is False  # once per month


def test_every_call_is_recorded(session):
    llm.complete_text("summarize_document", "s", "hello world")
    assert session.query(LLMUsage).filter_by(task="summarize_document").count() == 1


# --------------------------------------------------------------------------
# Backups and reminders
# --------------------------------------------------------------------------

@pytest.mark.skipif(not settings.database_url.startswith("sqlite"), reason="SQLite archive backup only")
def test_backup_contains_consistent_database(session, monkeypatch):
    monkeypatch.setattr(settings, "backup_keep", 2)
    session.add(Project(name="Backed up"))
    session.commit()
    paths = [maintenance.backup() for _ in range(3)]
    archives = sorted(settings.backup_path.glob("grantforge-backup-*.tar.gz"))
    assert len(archives) <= 2 and Path(paths[-1]).exists()
    with tarfile.open(paths[-1]) as tar:
        names = tar.getnames()
        assert "grantforge.db" in names
        tar.extract("grantforge.db", settings.data_dir / "restore_check", filter="data")
    eng = create_engine(f"sqlite:///{settings.data_dir / 'restore_check' / 'grantforge.db'}")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT name FROM projects")).scalar() == "Backed up"


def test_deadline_reminders_sent_once_per_threshold(session):
    p = Project(name="P")
    o = Opportunity(source="s", source_id="r", title="Soon", close_date=(date.today() + timedelta(days=5)).isoformat())
    session.add_all([p, o])
    session.flush()
    session.add(Match(project_id=p.id, opportunity_id=o.id, state="notified"))
    session.commit()
    assert maintenance.send_reminders() == 1
    assert maintenance.send_reminders() == 0
    assert session.query(AuditEvent).filter_by(event="reminder_7").count() == 1


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def test_job_is_claimed_once(session):
    from grantforge.worker import claim_next_job, enqueue

    job = enqueue(session, "notify", None)
    assert claim_next_job() == job.id
    assert claim_next_job() is None


def test_interrupted_submit_job_is_failed_not_requeued(session):
    from grantforge.worker import recover_stale_jobs

    p = Project(name="P")
    o = Opportunity(source="s", source_id="i", title="T")
    session.add_all([p, o])
    session.flush()
    m = Match(project_id=p.id, opportunity_id=o.id, state="submitting", authorized_at=utcnow())
    session.add(m)
    session.flush()
    sub = Job(kind="submit", status="running", payload={"match_id": m.id}, started_at=utcnow())
    other = Job(kind="pipeline", status="running", started_at=utcnow())
    session.add_all([sub, other])
    session.commit()
    recover_stale_jobs(older_than=None)
    session.refresh(sub)
    session.refresh(other)
    session.refresh(m)
    assert sub.status == "failed" and other.status == "queued" and m.state == "failed"


# --------------------------------------------------------------------------
# OCR and URL safety
# --------------------------------------------------------------------------

def test_scanned_pdf_is_read_by_ocr(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from grantforge.ingest.extract import extract_with_warning, ocr_available

    if not ocr_available():
        pytest.skip("tesseract/pdftoppm not installed")
    img = Image.new("RGB", (1700, 600), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    ImageDraw.Draw(img).text((60, 200), "Potential energy harvester prototype", fill="black", font=font)
    pdf = tmp_path / "scan.pdf"
    img.save(pdf, "PDF", resolution=150)
    text, warning = extract_with_warning(pdf)
    assert "energy harvester" in text.lower()
    assert "OCR" in warning


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8000/", "http://169.254.169.254/latest/",
                                 "http://10.0.0.5/", "file:///etc/passwd", "ftp://example.com/x"])
def test_private_and_non_http_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        check_url(url)


# --------------------------------------------------------------------------
# Web-search results are re-read from the funder's page
# --------------------------------------------------------------------------

def test_web_search_results_are_verified_on_the_funder_page(session, monkeypatch):
    import json

    from grantforge.llm import MockProvider
    from grantforge.sources import manual_url
    from grantforge.sources.web_search import WebSearchSource

    monkeypatch.setattr(manual_url, "fetch_page_text", lambda url: "Real page text. " * 50)
    MockProvider.overrides["parse_opportunity_page"] = lambda user: json.dumps({
        "title": "Mock Foundation Technology Grant", "funder": "Mock Foundation", "close_date": "2027-04-15",
        "award_ceiling": 50000, "applicant_types": ["Nonprofits"], "description": "From the page.", "is_open": True})
    out = WebSearchSource().search({"project_name": "x"}, lambda *_: None)
    assert len(out) == 1
    o = out[0]
    assert o.verification == "verified" and o.close_date == "2027-04-15" and o.award_ceiling == 50000
    assert o.applicant_types == ["Nonprofits"]  # the page's eligibility replaces the search summary's

    MockProvider.overrides["parse_opportunity_page"] = lambda user: json.dumps({"is_open": False, "title": "Closed"})
    assert WebSearchSource().search({"project_name": "x"}, lambda *_: None) == []

    def boom(url):
        raise OSError("unreachable")

    monkeypatch.setattr(manual_url, "fetch_page_text", boom)
    out = WebSearchSource().search({"project_name": "x"}, lambda *_: None)
    assert out[0].verification == "failed"


def test_files_survive_an_erased_disk(session, tmp_path):
    """Render erases the container disk on every deploy: packages and uploads come back from the database."""
    import shutil

    from grantforge import blobstore
    from grantforge.db import ApplicantProfile, Document
    from grantforge.drafting import draft_match
    from grantforge.sources import RawOpportunity, upsert_opportunities
    from grantforge.submit import submit_match

    up = settings.data_dir / "uploads" / "99"
    up.mkdir(parents=True, exist_ok=True)
    (up / "cv.pdf").write_bytes(b"%PDF-1.4 cv")
    blobstore.save(up / "cv.pdf")
    session.get(ApplicantProfile, 1).legal_name = "X"
    p = Project(name="P", profile={"project_name": "P"})
    session.add(p)
    session.flush()
    session.add(Document(project_id=p.id, kind="file", name="cv.pdf", path=str(up / "cv.pdf"), text="cv", is_attachment=True))
    o = upsert_opportunities(session, [RawOpportunity(source="s", source_id="z", title="Fund", status="posted",
                                                      close_date=(date.today() + timedelta(days=40)).isoformat(),
                                                      submission_channel="email", submission_target="f@example.org")])[0]
    m = Match(project_id=p.id, opportunity_id=o.id, state="shortlisted")
    session.add(m)
    session.commit()
    draft_match(session, m, lambda *_: None)

    shutil.rmtree(m.package_dir)
    shutil.rmtree(up)
    names = {n for n, _s, _t in blobstore.list_dir(Path(m.package_dir))}
    assert m.package_files["docx"] in names and "attachments/cv.pdf" in names

    m.authorized_at, m.state = utcnow(), "authorized"
    session.commit()
    res = submit_match(session, m, lambda *_: None)
    assert res["status"] == "submitted"
    assert Path(m.package_dir, m.package_files["docx"]).is_file()  # restored to send it
