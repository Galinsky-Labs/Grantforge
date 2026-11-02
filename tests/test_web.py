"""Web UI smoke tests with the FastAPI test client (worker thread disabled)."""
import os

import pytest
from fastapi.testclient import TestClient

from grantforge import main as webmain
from grantforge import worker
from grantforge.db import Match, Opportunity, Project, utcnow
from grantforge.notify import review_url


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setattr(worker, "start", lambda: None)
    with TestClient(webmain.app) as c:
        yield c


def test_dashboard_and_project_flow(client):
    r = client.get("/")
    assert r.status_code == 200 and "New project" in r.text
    r = client.post("/projects", data={"name": "Web Demo", "notes": "hello"}, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    r = client.post(f"/projects/{pid}/upload", files=[("files", ("notes.md", b"# Notes\nsome text", "text/markdown"))], follow_redirects=True)
    assert "notes.md" in r.text
    r = client.post(f"/projects/{pid}/run/pipeline", follow_redirects=True)
    assert "queued" in r.text  # job enqueued for the worker
    r = client.get("/settings")
    assert r.status_code == 200
    r = client.post("/settings", data={"legal_name": "Org LLC", "entity_type": "small_business", "contact_email": "a@b.c"}, follow_redirects=True)
    assert 'value="Org LLC"' in r.text


def test_review_requires_token_or_login_and_authorize_enqueues(client, session, monkeypatch):
    monkeypatch.setattr(webmain.settings, "ui_password", "pw")
    p = Project(name="P")
    o = Opportunity(source="s", source_id="1", title="Opp", submission_channel="email", submission_target="x@y.z")
    session.add_all([p, o])
    session.flush()
    m = Match(project_id=p.id, opportunity_id=o.id, score=80, state="notified", draft_markdown="# T\n\n## S\nbody",
              draft_sections={"title": "T", "sections": [{"heading": "S", "body": "body"}]}, draft_gaps=[], draft_checklist=[])
    session.add(m)
    session.commit()

    r = client.get(f"/review/{m.id}", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    r = client.get(f"/review/{m.id}?t=badtoken", follow_redirects=False)
    assert r.status_code == 303
    url = review_url(m.id).replace("http://testserver", "")
    r = client.get(url)
    assert r.status_code == 200 and "Authorize submission" in r.text
    token = url.split("t=")[1]

    r = client.post(f"/review/{m.id}/authorize", data={"t": token, "confirm": "nope"})
    assert r.status_code == 400
    r = client.post(f"/review/{m.id}/authorize", data={"t": token, "confirm": "SUBMIT", "authorized_by": "Me"}, follow_redirects=False)
    assert r.status_code == 303
    session.refresh(m)
    assert m.state == "authorized" and m.authorized_by == "Me" and m.authorized_at is not None
    from grantforge.db import Job

    jobs = session.query(Job).filter_by(kind="submit").all()
    assert len(jobs) == 1 and jobs[0].payload["match_id"] == m.id
