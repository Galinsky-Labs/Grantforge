"""Free-plan resilience: interrupted runs resume from durable state, and email that cannot work says so."""
import httpx
import pytest

from grantforge import llm, notify, pipeline, worker
from grantforge.config import settings
from grantforge.db import Job, Match, Opportunity, Project, SessionLocal, utcnow
from grantforge.drafting import draft_match
from grantforge.setup_status import blocking, setup_checks

from test_pipeline import StubSource, make_project


class Killed(BaseException):
    """Stands in for the process being stopped (a deploy, or a Free instance going to sleep)."""


@pytest.fixture()
def stub_sources(monkeypatch):
    monkeypatch.setattr(pipeline, "all_sources", lambda _settings: [StubSource()])


def _shortlisted_match(session) -> Match:
    p = Project(name="P", profile={"project_name": "P"})
    o = Opportunity(source="manual", source_id="r1", title="Opp", requirements={"sections": []})
    session.add_all([p, o])
    session.flush()
    m = Match(project_id=p.id, opportunity_id=o.id, state="shortlisted", score=80)
    session.add(m)
    session.commit()
    return m


# --------------------------------------------------------------------------
# Drafting resumes from the last finished section
# --------------------------------------------------------------------------

def test_interrupted_draft_continues_from_the_last_finished_section(session, monkeypatch):
    m = _shortlisted_match(session)
    calls = {"plan": 0, "section": 0}
    real_json = llm.complete_json

    def counting_json(task, *a, **k):
        if task == "plan_application":
            calls["plan"] += 1
        return real_json(task, *a, **k)

    monkeypatch.setattr(llm, "complete_json", counting_json)

    def section(_user):
        calls["section"] += 1
        if calls["section"] == 2:
            raise Killed()
        return f"Body of section number {calls['section']}."

    llm.MockProvider.overrides["draft_section"] = section
    with pytest.raises(Killed):
        draft_match(session, m, lambda _l: None)

    # What the first attempt finished is in the database, not only in memory.
    with SessionLocal() as other:
        saved = other.get(Match, m.id).draft_progress
    assert saved and len(saved["sections"]) == 1 and saved["base_version"] == 0
    planned = len(saved["plan"]["sections"])
    assert planned >= 2

    session.expire_all()
    m = session.get(Match, m.id)
    lines = []
    draft_match(session, m, lines.append)
    assert m.state == "drafted" and m.version == 1 and m.draft_progress is None
    assert calls["plan"] == 1  # the plan was not made again
    assert calls["section"] == planned + 1  # one killed call, then each section exactly once
    assert m.draft_sections["sections"][0]["body"] == "Body of section number 1."
    assert any("Resuming the draft" in line for line in lines)


def test_progress_from_an_older_version_is_not_reused(session):
    m = _shortlisted_match(session)
    m.version = 2
    m.draft_progress = {"base_version": 1, "plan": {"sections": [{"heading": "Old"}]},
                        "sections": [{"heading": "Old", "body": "stale"}]}
    session.commit()
    draft_match(session, m, lambda _l: None)
    assert m.version == 3 and "stale" not in m.draft_markdown


# --------------------------------------------------------------------------
# The pipeline resumes from its checkpoint
# --------------------------------------------------------------------------

def test_interrupted_pipeline_job_resumes_after_restart(session, stub_sources, monkeypatch):
    p = make_project(session)
    job = worker.enqueue(session, "pipeline", p.id)
    discovered = {"n": 0}
    real_discover = pipeline.run_discover

    def discover(*a, **k):
        discovered["n"] += 1
        return real_discover(*a, **k)

    monkeypatch.setattr(pipeline, "run_discover", discover)
    real_draft = pipeline.run_draft

    def die_in_draft(*_a, **_k):
        raise Killed()

    monkeypatch.setattr(pipeline, "run_draft", die_in_draft)
    assert worker.claim_next_job() == job.id
    with pytest.raises(Killed):
        worker.run_job(job.id)

    session.expire_all()
    j = session.get(Job, job.id)
    assert j.status == "running" and j.attempts == 1
    assert j.checkpoint["done"] == ["profile", "discover", "score"] and j.checkpoint["opportunity_ids"]

    # The process restarts: the job is re-queued and picked up again.
    monkeypatch.setattr(pipeline, "run_draft", real_draft)
    worker.recover_stale_jobs(older_than=None)
    session.expire_all()
    assert session.get(Job, job.id).status == "queued"
    assert worker.claim_next_job() == job.id
    worker.run_job(job.id)

    session.expire_all()
    j = session.get(Job, job.id)
    assert j.status == "done", j.error
    assert j.attempts == 2 and discovered["n"] == 1  # discovery was not repeated
    assert j.checkpoint["done"] == ["profile", "discover", "score", "draft", "notify"]
    assert "Resuming an interrupted run" in j.log
    states = {m.state for m in session.query(Match).filter_by(project_id=p.id)}
    assert "notified" in states


def test_job_interrupted_too_often_is_failed_not_requeued(session, monkeypatch):
    monkeypatch.setattr(settings, "job_max_attempts", 2)
    j = Job(kind="pipeline", status="running", attempts=2, started_at=utcnow())
    k = Job(kind="pipeline", status="running", attempts=1, started_at=utcnow())
    session.add_all([j, k])
    session.commit()
    worker.recover_stale_jobs(older_than=None)
    session.refresh(j)
    session.refresh(k)
    assert j.status == "failed" and "GF_JOB_MAX_ATTEMPTS" in j.error
    assert k.status == "queued"


# --------------------------------------------------------------------------
# Keep awake while busy
# --------------------------------------------------------------------------

def test_keep_awake_pings_only_while_a_job_runs(monkeypatch):
    import threading

    pinged = []
    got = threading.Event()

    def fake_get(url, timeout):
        pinged.append(url)
        got.set()

    monkeypatch.setattr(worker.httpx, "get", fake_get)
    monkeypatch.setattr(settings, "base_url", "https://gf.example.com")
    monkeypatch.setattr(settings, "keepalive_seconds", 0.05)
    with worker.keep_awake():
        assert got.wait(5)
    n = len(pinged)
    import time

    time.sleep(0.2)
    assert len(pinged) <= n + 1 and pinged[0] == "https://gf.example.com/health"

    monkeypatch.setattr(settings, "base_url", "http://localhost:8000")
    assert worker.keep_awake.url() is None  # never on a local install
    monkeypatch.setattr(settings, "base_url", "https://gf.example.com")
    monkeypatch.setattr(settings, "keepalive_seconds", 0)
    assert worker.keep_awake.url() is None


# --------------------------------------------------------------------------
# Email on a host that blocks SMTP
# --------------------------------------------------------------------------

def _free_hosted(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://grantforge.onrender.com")
    monkeypatch.setattr(settings, "ui_password", "pw")
    monkeypatch.setattr(settings, "hosting_plan", "free")


def test_free_plan_smtp_is_reported_unavailable_and_never_attempted(session, monkeypatch):
    _free_hosted(monkeypatch)
    monkeypatch.setattr(settings, "email_backend", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(settings, "smtp_user", "me@example.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP must not be attempted"))
    checks = {c["name"]: c for c in setup_checks()}
    assert checks["Sending email"]["level"] == "missing" and "RESEND_API_KEY" in checks["Sending email"]["fix"]
    assert checks["Hosting plan"]["level"] == "warn" and "resumes" in checks["Hosting plan"]["text"]
    assert "Sending email" in {c["name"] for c in blocking(setup_checks())}
    assert "Free hosting plan blocks outbound SMTP" in notify.email_unavailable_reason()
    with pytest.raises(RuntimeError, match="blocks outbound SMTP"):
        notify.send_email("me@example.com", "s", "b")


def test_drafts_survive_when_the_notification_cannot_be_sent(session, monkeypatch):
    m = _shortlisted_match(session)
    draft_match(session, m, lambda _l: None)
    project = m.project

    monkeypatch.setattr(settings, "email_backend", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "")
    lines = []
    assert pipeline.run_notify(session, project, None, lines.append) is None
    assert m.state == "drafted" and "Email is not available" in lines[-1]

    monkeypatch.setattr(settings, "email_backend", "console")

    def boom(*_a, **_k):
        raise OSError("network unreachable")

    monkeypatch.setattr(pipeline, "notify_drafts_ready", boom)
    assert pipeline.run_notify(session, project, None, lines.append) is None
    assert m.state == "drafted" and "could not be sent" in lines[-1]


def test_resend_backend_sends_over_https(session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "email_backend", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "grants@example.org")
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"id": "email-123"})

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    att = tmp_path / "app.docx"
    att.write_bytes(b"docx-bytes")
    result = notify.send_email("me@example.com", "Subject", "Body", [att], html="<p>Body</p>", reply_to="r@example.com")
    assert result == {"backend": "resend", "to": "me@example.com", "subject": "Subject", "id": "email-123"}
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["headers"]["Authorization"] == "Bearer re_test_key"
    body = sent["json"]
    assert body["from"] == "grants@example.org" and body["to"] == ["me@example.com"] and body["reply_to"] == "r@example.com"
    assert body["attachments"][0]["filename"] == "app.docx" and body["attachments"][0]["content"] == "ZG9jeC1ieXRlcw=="
    assert notify.email_unavailable_reason() is None

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: httpx.Response(403, json={"message": "domain not verified"}))
    with pytest.raises(RuntimeError, match="domain not verified"):
        notify.send_email("me@example.com", "S", "B")


def test_resend_setup_status_names_what_is_missing(session, monkeypatch):
    _free_hosted(monkeypatch)
    monkeypatch.setattr(settings, "email_backend", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "email_from", "")
    email = next(c for c in setup_checks() if c["name"] == "Sending email")
    assert email["level"] == "missing" and "RESEND_API_KEY" in email["fix"] and "GF_EMAIL_FROM" in email["fix"]
    monkeypatch.setattr(settings, "resend_api_key", "re_secret_value")
    monkeypatch.setattr(settings, "email_from", "grants@example.org")
    checks = setup_checks()
    email = next(c for c in checks if c["name"] == "Sending email")
    assert email["level"] == "ok" and "re_secret_value" not in str(checks)


def test_dashboard_says_email_is_unavailable(session, monkeypatch):
    from fastapi.testclient import TestClient

    from grantforge import main as webmain

    monkeypatch.setattr(settings, "email_backend", "smtp")
    monkeypatch.setattr(settings, "hosting_plan", "free")
    with TestClient(webmain.app, base_url="http://localhost") as client:
        home = client.get("/")
        assert "Email is unavailable" in home.text and "blocks outbound SMTP" in home.text
        r = client.post("/settings/test-email", data={"csrf": client.cookies.get("gf_csrf")}, follow_redirects=True)
        assert "No test email was sent" in r.text
