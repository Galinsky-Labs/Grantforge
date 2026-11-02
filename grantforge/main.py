"""FastAPI application: dashboard, project pages, review/authorize pages."""
from __future__ import annotations

import logging
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import worker
from .config import settings
from .db import ApplicantProfile, AuditEvent, Document, Job, Match, Opportunity, Project, audit, get_session, init_db, utcnow
from .drafting import update_sections_from_markdown
from .ingest import extract_text, ingest_repo
from .notify import review_url, verify_review_token
from .sources import upsert_opportunities
from .sources.manual_url import opportunity_from_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["markdown"] = lambda text: md.markdown(text or "", extensions=["tables", "fenced_code"])
templates.env.globals["settings"] = settings
templates.env.globals["review_url"] = review_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="GrantForge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# --------------------------------------------------------------------------
# Auth: one shared password (GF_UI_PASSWORD) via a cookie; review links carry
# their own signed token so the email link works without the cookie.
# --------------------------------------------------------------------------

def _session_cookie_value() -> str:
    import hashlib

    return hashlib.sha256((settings.app_secret + settings.ui_password).encode()).hexdigest()


def require_login(request: Request):
    if not settings.ui_password:
        return
    if request.cookies.get("gf_session") == _session_cookie_value():
        return
    raise HTTPException(status_code=303, headers={"Location": f"/login?next={request.url.path}"})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": ""})


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if settings.ui_password and not secrets.compare_digest(password, settings.ui_password):
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": "Wrong password"}, status_code=401)
    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie("gf_session", _session_cookie_value(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


# --------------------------------------------------------------------------
# Dashboard & projects
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def index(request: Request, s: Session = Depends(get_session)):
    projects = s.scalars(select(Project).order_by(Project.created_at.desc())).all()
    recent_jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(8)).all()
    pending = s.scalars(
        select(Match).where(Match.state.in_(["drafted", "notified"])).order_by(Match.score.desc())
    ).all()
    applicant = s.get(ApplicantProfile, 1)
    return templates.TemplateResponse(
        request, "index.html",
        {"projects": projects, "jobs": recent_jobs, "pending": pending, "applicant": applicant},
    )


@app.post("/projects", dependencies=[Depends(require_login)])
def create_project(name: str = Form(...), notes: str = Form(""), s: Session = Depends(get_session)):
    p = Project(name=name.strip() or "Untitled project", notes=notes)
    s.add(p)
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def project_page(project_id: int, request: Request, s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    matches = s.scalars(
        select(Match).where(Match.project_id == p.id).order_by(Match.score.desc())
    ).all()
    jobs = s.scalars(select(Job).where(Job.project_id == p.id).order_by(Job.id.desc()).limit(10)).all()
    active = any(j.status in ("queued", "running") for j in jobs)
    groups = {
        "action": [m for m in matches if m.state in {"drafted", "notified", "ready_for_manual_upload"}],
        "authorized": [m for m in matches if m.state in {"authorized", "submitted", "failed"}],
        "shortlisted": [m for m in matches if m.state == "shortlisted"],
        "rejected": [m for m in matches if m.state == "rejected"],
    }
    return templates.TemplateResponse(
        request, "project.html",
        {"p": p, "groups": groups, "jobs": jobs, "active": active, "matches": matches},
    )


@app.post("/projects/{project_id}/notes", dependencies=[Depends(require_login)])
def update_notes(project_id: int, notes: str = Form(""), name: str = Form(None), s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    p.notes = notes
    if name:
        p.name = name
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.post("/projects/{project_id}/upload", dependencies=[Depends(require_login)])
async def upload(project_id: int, files: list[UploadFile] = File(...), s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    dest_dir = settings.data_dir / "uploads" / str(p.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f.filename:
            continue
        dest = dest_dir / Path(f.filename).name
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        text = extract_text(dest)
        s.add(Document(project_id=p.id, kind="file", name=dest.name, path=str(dest), text=text, char_count=len(text)))
    p.status = "new" if not p.profile else p.status
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.post("/projects/{project_id}/repo", dependencies=[Depends(require_login)])
def add_repo(project_id: int, location: str = Form(...), s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    try:
        name, text = ingest_repo(location.strip())
    except Exception as exc:
        raise HTTPException(400, str(exc))
    s.add(Document(project_id=p.id, kind="repo", name=name, path=location.strip(), text=text, char_count=len(text)))
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.post("/projects/{project_id}/text", dependencies=[Depends(require_login)])
def add_text(project_id: int, title: str = Form("Pasted text"), body: str = Form(...), s: Session = Depends(get_session)):
    s.add(Document(project_id=project_id, kind="text", name=title, text=body, char_count=len(body)))
    s.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/documents/{doc_id}/delete", dependencies=[Depends(require_login)])
def delete_document(doc_id: int, s: Session = Depends(get_session)):
    d = s.get(Document, doc_id)
    pid = d.project_id
    s.delete(d)
    s.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{project_id}/run/{kind}", dependencies=[Depends(require_login)])
def run_job(project_id: int, kind: str, rebuild: str = Form(""), s: Session = Depends(get_session)):
    if kind not in {"pipeline", "profile", "discover", "draft", "notify"}:
        raise HTTPException(400)
    worker.enqueue(s, kind, project_id, {"rebuild_profile": bool(rebuild)})
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/add-opportunity", dependencies=[Depends(require_login)])
def add_opportunity(project_id: int, url: str = Form(...), pasted: str = Form(""), s: Session = Depends(get_session)):
    try:
        raw = opportunity_from_url(url.strip(), pasted)
    except Exception as exc:
        raise HTTPException(400, f"Could not read that opportunity: {exc}")
    rows = upsert_opportunities(s, [raw])
    from .matching import score_project

    p = s.get(Project, project_id)
    if p.profile:
        score_project(s, p, rows, log_fn=log.info)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def job_page(job_id: int, request: Request, s: Session = Depends(get_session)):
    j = s.get(Job, job_id)
    if not j:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "job.html", {"j": j})


# --------------------------------------------------------------------------
# Review & authorization
# --------------------------------------------------------------------------

def _load_match_for_review(match_id: int, t: str | None, request: Request, s: Session) -> Match:
    m = s.get(Match, match_id)
    if not m:
        raise HTTPException(404)
    if t and verify_review_token(match_id, t):
        return m
    require_login(request)
    return m


@app.get("/review/{match_id}", response_class=HTMLResponse)
def review(match_id: int, request: Request, t: str | None = None, s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    events = s.scalars(select(AuditEvent).where(AuditEvent.match_id == m.id).order_by(AuditEvent.id)).all()
    applicant = s.get(ApplicantProfile, 1)
    readiness = _readiness(m, applicant)
    return templates.TemplateResponse(
        request, "review.html",
        {"m": m, "o": m.opportunity, "t": t or "", "events": events, "applicant": applicant, "readiness": readiness},
    )


def _readiness(m: Match, a: ApplicantProfile | None) -> list[dict]:
    """Checks shown above the Authorize button. Warnings do not block; the human decides."""
    checks = []
    o = m.opportunity
    gaps = m.draft_gaps or []
    checks.append({"ok": not gaps, "text": f"{len(gaps)} open question(s) from the drafter" if gaps else "No open questions from the drafter"})
    placeholders = (m.draft_markdown or "").count("[APPLICANT:")
    checks.append({"ok": placeholders == 0, "text": f"{placeholders} placeholder(s) still in the text" if placeholders else "No placeholders left in the text"})
    if o.submission_channel == "grants_gov":
        checks.append({"ok": bool(a and a.sam_registered), "text": "SAM.gov registration (required for federal awards)"})
        checks.append({"ok": bool(a and a.grants_gov_registered), "text": "Grants.gov AOR account"})
    if o.submission_channel == "email":
        checks.append({"ok": bool(o.submission_target or o.contact_email), "text": f"Funder email: {o.submission_target or o.contact_email or 'missing'}"})
        if settings.submission_test_mode:
            checks.append({"ok": True, "text": "Submission test mode is ON: the email will go to you, not the funder"})
    checks.append({"ok": bool(a and a.legal_name), "text": "Applicant legal name set in Settings" if a and a.legal_name else "Applicant legal name missing (Settings)"})
    return checks


@app.post("/review/{match_id}/save")
def review_save(match_id: int, request: Request, t: str = Form(""), markdown_text: str = Form(...),
                user_notes: str = Form(""), submission_target: str = Form(""), submission_channel: str = Form(""),
                s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    update_sections_from_markdown(m, markdown_text)
    m.user_notes = user_notes
    if submission_target.strip():
        m.opportunity.submission_target = submission_target.strip()
    if submission_channel in {"email", "grants_gov", "portal", "unknown"}:
        m.opportunity.submission_channel = submission_channel
    audit(s, "edited", m.id)
    s.commit()
    return RedirectResponse(f"/review/{m.id}?t={t}&saved=1", status_code=303)


@app.post("/review/{match_id}/authorize")
def review_authorize(match_id: int, request: Request, t: str = Form(""), confirm: str = Form(""),
                     authorized_by: str = Form(""), s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    if confirm != "SUBMIT":
        raise HTTPException(400, "Type SUBMIT in the confirmation box to authorize.")
    m.authorized_at = utcnow()
    m.authorized_by = authorized_by or (s.get(ApplicantProfile, 1).contact_email if s.get(ApplicantProfile, 1) else "")
    m.state = "authorized"
    audit(s, "authorized", m.id, by=m.authorized_by)
    s.commit()
    worker.enqueue(s, "submit", m.project_id, {"match_id": m.id})
    return RedirectResponse(f"/review/{m.id}?t={t}", status_code=303)


@app.post("/review/{match_id}/reject")
def review_reject(match_id: int, request: Request, t: str = Form(""), s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    m.state = "rejected"
    audit(s, "rejected_by_applicant", m.id)
    s.commit()
    return RedirectResponse(f"/projects/{m.project_id}", status_code=303)


@app.post("/review/{match_id}/redraft")
def review_redraft(match_id: int, request: Request, t: str = Form(""), s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    worker.enqueue(s, "draft", m.project_id, {"match_id": m.id})
    return RedirectResponse(f"/review/{m.id}?t={t}", status_code=303)


@app.post("/review/{match_id}/mark-submitted")
def review_mark_submitted(match_id: int, request: Request, t: str = Form(""), confirmation: str = Form(""),
                          s: Session = Depends(get_session)):
    """For manual channels: the applicant records that they submitted it."""
    m = _load_match_for_review(match_id, t, request, s)
    m.state = "submitted"
    m.submitted_at = utcnow()
    m.submission_result = {**(m.submission_result or {}), "status": "submitted", "manual_confirmation": confirmation}
    audit(s, "submitted_manually", m.id, confirmation=confirmation)
    s.commit()
    return RedirectResponse(f"/review/{m.id}?t={t}", status_code=303)


@app.get("/review/{match_id}/download/{name}")
def download(match_id: int, name: str, request: Request, t: str | None = None, s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    allowed = {"application.docx", "application.md", "package.json", "CHECKLIST.md", "SUBMIT_INSTRUCTIONS.md", "sf424_prefill.json"}
    if name not in allowed or not m.package_dir:
        raise HTTPException(404)
    path = Path(m.package_dir) / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), filename=f"match{m.id}_{name}")


# --------------------------------------------------------------------------
# Settings (applicant profile)
# --------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def settings_page(request: Request, s: Session = Depends(get_session)):
    a = s.get(ApplicantProfile, 1)
    return templates.TemplateResponse(request, "settings.html", {"a": a})


@app.post("/settings", dependencies=[Depends(require_login)])
async def settings_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    a = s.get(ApplicantProfile, 1)
    for field in ("legal_name", "entity_type", "uei", "ein", "contact_name", "contact_title", "contact_email",
                  "contact_phone", "address", "state", "country", "congressional_district", "website", "bio"):
        if field in form:
            setattr(a, field, str(form[field]).strip())
    a.sam_registered = form.get("sam_registered") == "on"
    a.grants_gov_registered = form.get("grants_gov_registered") == "on"
    s.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/opportunities/{opp_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def opportunity_page(opp_id: int, request: Request, s: Session = Depends(get_session)):
    o = s.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "opportunity.html", {"o": o})


@app.get("/health")
def health():
    return {"ok": True}
