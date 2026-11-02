"""Web UI tests with the FastAPI test client (worker thread disabled)."""
import re
import time

import pytest
from fastapi.testclient import TestClient

from grantforge import main as webmain
from grantforge.config import settings
from grantforge.db import Job, Match, Opportunity, Project
from grantforge.notify import review_url, sign_review_token


@pytest.fixture()
def client(session):
    with TestClient(webmain.app, base_url="http://localhost") as c:
        yield c


def csrf(client, path="/") -> str:
    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    return m.group(1) if m else client.cookies.get("gf_csrf")


def make_match(session, state="notified") -> Match:
    p = Project(name="P")
    o = Opportunity(source="s", source_id="1", title="Opp <script>alert(1)</script>", submission_channel="email",
                    submission_target="x@y.z", description="<img src=x onerror=alert(1)> **bold** text")
    session.add_all([p, o])
    session.flush()
    m = Match(project_id=p.id, opportunity_id=o.id, score=80, state=state, version=1,
              draft_markdown="# T\n\n## S\nbody <script>alert(2)</script>",
              draft_sections={"title": "T", "sections": [{"heading": "S", "body": "body"}]},
              draft_gaps=[], draft_checklist=[], fact_check=[], length_report=[])
    session.add(m)
    session.commit()
    return m


def test_dashboard_and_project_flow(client):
    token = csrf(client)
    r = client.post("/projects", data={"name": "Web Demo", "notes": "hello", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    r = client.post(f"/projects/{pid}/upload", data={"csrf": token},
                    files=[("files", ("notes.md", b"# Notes\nsome text", "text/markdown"))], follow_redirects=True)
    assert "notes.md" in r.text
    r = client.post(f"/projects/{pid}/run/pipeline", data={"csrf": token}, follow_redirects=True)
    assert "queued" in r.text
    r = client.post("/settings", data={"legal_name": "Org LLC", "entity_type": "small_business", "csrf": token},
                    follow_redirects=True)
    assert 'value="Org LLC"' in r.text


def test_post_without_csrf_token_is_rejected(client):
    client.get("/")
    r = client.post("/projects", data={"name": "x"})
    assert r.status_code == 403
    r = client.post("/projects", data={"name": "x", "csrf": "wrong-token-value-1234567"})
    assert r.status_code == 403


def test_attachment_toggle(client, session):
    token = csrf(client)
    r = client.post("/projects", data={"name": "A", "csrf": token}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/projects/{pid}/upload", data={"csrf": token}, files=[("files", ("cv.md", b"CV", "text/markdown"))])
    from grantforge.db import Document

    doc = session.query(Document).filter_by(project_id=pid).one()
    client.post(f"/documents/{doc.id}/attachment", data={"csrf": token})
    session.refresh(doc)
    assert doc.is_attachment is True


def test_web_repo_ingestion_refuses_local_paths(client):
    token = csrf(client)
    r = client.post("/projects", data={"name": "R", "csrf": token}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    for loc in ["/etc", "file:///etc", "git@github.com:x/y.git", "http://127.0.0.1/repo.git"]:
        r = client.post(f"/projects/{pid}/repo", data={"location": loc, "csrf": token})
        assert r.status_code == 400, loc


def test_external_html_is_sanitised_on_review_and_opportunity_pages(client, session):
    m = make_match(session)
    r = client.get(review_url(m.id).replace("http://localhost:8000", ""))
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)" in r.text
    r = client.get(f"/opportunities/{m.opportunity_id}")
    assert "onerror" not in r.text and "<strong>bold</strong>" in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert r.headers["referrer-policy"] == "no-referrer"


def test_review_link_expiry_and_login_requirement(client, session, monkeypatch):
    monkeypatch.setattr(settings, "ui_password", "pw")
    m = make_match(session)
    assert client.get(f"/review/{m.id}", follow_redirects=False).status_code == 303
    expired = sign_review_token(m.id, int(time.time()) - 10)
    assert client.get(f"/review/{m.id}?t={expired}", follow_redirects=False).status_code == 303
    forged = expired.split(".")[0] + ".deadbeef"
    assert client.get(f"/review/{m.id}?t={forged}", follow_redirects=False).status_code == 303
    good = review_url(m.id).replace("http://localhost:8000", "")
    assert client.get(good).status_code == 200


def test_authorize_needs_confirmation_and_password(client, session, monkeypatch):
    monkeypatch.setattr(settings, "ui_password", "pw")
    m = make_match(session)
    url = review_url(m.id).replace("http://localhost:8000", "")
    token = url.split("t=")[1]
    page = client.get(url)
    form_csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    base = {"t": token, "csrf": form_csrf, "authorized_by": "Me"}
    assert client.post(f"/review/{m.id}/authorize", data={**base, "confirm": "nope", "password": "pw"}).status_code == 400
    assert client.post(f"/review/{m.id}/authorize", data={**base, "confirm": "SUBMIT", "password": "bad"}).status_code == 403
    r = client.post(f"/review/{m.id}/authorize", data={**base, "confirm": "SUBMIT", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303
    session.refresh(m)
    assert m.state == "authorized" and m.authorized_by == "Me"
    jobs = session.query(Job).filter_by(kind="submit").all()
    assert len(jobs) == 1 and jobs[0].payload["match_id"] == m.id
    # A second authorize (double click) is refused, so only one submit job exists.
    assert client.post(f"/review/{m.id}/authorize", data={**base, "confirm": "SUBMIT", "password": "pw"}).status_code == 409
    # Editing after authorization is refused.
    assert client.post(f"/review/{m.id}/save", data={**base, "markdown_text": "# x"}).status_code == 409


def test_login_rate_limit_session_and_open_redirect(client, monkeypatch):
    monkeypatch.setattr(settings, "ui_password", "pw")
    from grantforge.security import login_limiter

    login_limiter._fails.clear()
    token = csrf(client, "/login")
    r = client.post("/login", data={"password": "pw", "next": "//evil.example", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout", data={"csrf": token})
    assert client.get("/", follow_redirects=False).status_code == 303
    for _ in range(5):
        assert client.post("/login", data={"password": "bad", "csrf": token}).status_code == 401
    assert client.post("/login", data={"password": "pw", "csrf": token}).status_code == 429
    login_limiter._fails.clear()


def test_public_url_without_password_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://grants.example.com")
    monkeypatch.setattr(settings, "ui_password", "")
    assert client.get("/", follow_redirects=False).status_code == 503


def test_download_cannot_escape_package_dir(client, session, tmp_path):
    m = make_match(session)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.md").write_text("ok")
    (tmp_path / "secret.txt").write_text("secret")
    m.package_dir = str(pkg)
    session.commit()
    t = review_url(m.id).split("t=")[1]
    assert client.get(f"/review/{m.id}/download/a.md?t={t}").text == "ok"
    assert client.get(f"/review/{m.id}/download/..%2Fsecret.txt?t={t}").status_code == 404
    assert client.get(f"/review/{m.id}/download/../secret.txt?t={t}").status_code in (404, 400)
