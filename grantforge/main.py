"""FastAPI application: dashboard, project pages, review/authorize pages."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import blobstore, worker
from .config import settings
from .db import ApplicantProfile, AuditEvent, Document, Job, Match, Opportunity, Project, audit, get_session, init_db, utcnow
from .drafting import update_sections_from_markdown
from .ingest import extract_with_warning, ingest_repo
from .llm import month_spend_usd
from .notify import review_url, verify_review_token
from .security import (
    SESSION_COOKIE, SecurityMiddleware, client_ip, create_session, csrf_field, csrf_protect,
    delete_session, is_logged_in, login_limiter, password_missing_on_public_host, sanitize_html,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["markdown"] = lambda text: sanitize_html(md.markdown(text or "", extensions=["tables", "fenced_code"]))
templates.env.globals["settings"] = settings
templates.env.globals["review_url"] = review_url
templates.env.globals["csrf_field"] = csrf_field


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="GrantForge", lifespan=lifespan, dependencies=[Depends(csrf_protect)],
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityMiddleware)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def require_login(request: Request):
    if password_missing_on_public_host():
        raise HTTPException(503, "GF_UI_PASSWORD must be set before GrantForge is reachable at a public address.")
    if is_logged_in(request):
        return
    raise HTTPException(status_code=303, headers={"Location": f"/login?next={request.url.path}"})


def _safe_next(target: str) -> str:
    # Only same-site paths: blocks open redirects such as next=//evil.example
    return target if target.startswith("/") and not target.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": ""})


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    ip = client_ip(request)
    if login_limiter.blocked(ip):
        return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next),
                                          "error": "Too many failed attempts. Wait 15 minutes."}, status_code=429)
    if not settings.ui_password or not secrets.compare_digest(password.encode(), settings.ui_password.encode()):
        login_limiter.fail(ip)
        return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": "Wrong password"},
                                          status_code=401)
    login_limiter.reset(ip)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(SESSION_COOKIE, create_session(ip), httponly=True, samesite="lax", secure=settings.https,
                    max_age=60 * 60 * 24 * settings.session_days)
    return resp


@app.post("/logout")
def logout(request: Request):
    delete_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --------------------------------------------------------------------------
# Dashboard & projects
# --------------------------------------------------------------------------

def _spend() -> dict:
    spent = month_spend_usd()
    cap = settings.monthly_budget_usd
    return {"spent": spent, "cap": cap, "over": bool(cap) and spent >= cap}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def index(request: Request, s: Session = Depends(get_session)):
    projects = s.scalars(select(Project).order_by(Project.created_at.desc())).all()
    recent_jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(8)).all()
    pending = s.scalars(select(Match).where(Match.state.in_(["drafted", "notified", "ready_for_manual_upload"]))
                        .order_by(Match.score.desc())).all()
    return templates.TemplateResponse(request, "index.html", {
        "projects": projects, "jobs": recent_jobs, "pending": pending,
        "applicant": s.get(ApplicantProfile, 1), "spend": _spend()})


@app.post("/projects", dependencies=[Depends(require_login)])
def create_project(name: str = Form(...), notes: str = Form(""), s: Session = Depends(get_session)):
    p = Project(name=name.strip()[:200] or "Untitled project", notes=notes)
    s.add(p)
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


def _project_or_404(s: Session, project_id: int) -> Project:
    p = s.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    return p


@app.get("/projects/{project_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def project_page(project_id: int, request: Request, s: Session = Depends(get_session)):
    p = _project_or_404(s, project_id)
    matches = s.scalars(select(Match).where(Match.project_id == p.id).order_by(Match.score.desc())).all()
    jobs = s.scalars(select(Job).where(Job.project_id == p.id).order_by(Job.id.desc()).limit(10)).all()
    active = any(j.status in ("queued", "running") for j in jobs)
    groups = {
        "action": [m for m in matches if m.state in {"drafted", "notified", "ready_for_manual_upload"}],
        "authorized": [m for m in matches if m.state in {"authorized", "submitting", "submitted", "failed"}],
        "shortlisted": [m for m in matches if m.state == "shortlisted"],
        "held": [m for m in matches if m.state == "held_forecast"],
        "rejected": [m for m in matches if m.state == "rejected"],
    }
    return templates.TemplateResponse(request, "project.html", {
        "p": p, "groups": groups, "jobs": jobs, "active": active, "matches": matches, "spend": _spend()})


@app.post("/projects/{project_id}/notes", dependencies=[Depends(require_login)])
def update_notes(project_id: int, notes: str = Form(""), s: Session = Depends(get_session)):
    p = _project_or_404(s, project_id)
    p.notes = notes
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.post("/projects/{project_id}/upload", dependencies=[Depends(require_login)])
async def upload(project_id: int, files: list[UploadFile] = File(...), s: Session = Depends(get_session)):
    p = _project_or_404(s, project_id)
    dest_dir = settings.data_dir / "uploads" / str(p.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    limit = settings.max_upload_mb * 1024 * 1024
    for f in files:
        if not f.filename:
            continue
        name = Path(f.filename).name.replace("\x00", "")[:200] or "upload"
        dest = dest_dir / name
        if dest.exists():
            dest = dest_dir / f"{dest.stem}_{secrets.token_hex(3)}{dest.suffix}"
        written = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"{name} is larger than {settings.max_upload_mb} MB")
                out.write(chunk)
        blobstore.save(dest)
        text, warning = extract_with_warning(dest)
        s.add(Document(project_id=p.id, kind="file", name=dest.name, path=str(dest), text=text,
                       char_count=len(text), warning=warning))
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.post("/projects/{project_id}/repo", dependencies=[Depends(require_login)])
def add_repo(project_id: int, location: str = Form(...), s: Session = Depends(get_session)):
    p = _project_or_404(s, project_id)
    try:
        name, text = ingest_repo(location.strip(), allow_local=False)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    s.add(Document(project_id=p.id, kind="repo", name=name, path=location.strip(), text=text, char_count=len(text),
                   warning="" if text.strip() else "No readable text was found in this repository."))
    s.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.post("/projects/{project_id}/text", dependencies=[Depends(require_login)])
def add_text(project_id: int, title: str = Form("Pasted text"), body: str = Form(...), s: Session = Depends(get_session)):
    _project_or_404(s, project_id)
    s.add(Document(project_id=project_id, kind="text", name=title[:200] or "Pasted text", text=body, char_count=len(body)))
    s.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/documents/{doc_id}/delete", dependencies=[Depends(require_login)])
def delete_document(doc_id: int, s: Session = Depends(get_session)):
    d = s.get(Document, doc_id)
    if not d:
        raise HTTPException(404)
    pid = d.project_id
    if d.path and d.kind == "file":
        Path(d.path).unlink(missing_ok=True)
        blobstore.remove(Path(d.path))
    s.delete(d)
    s.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/documents/{doc_id}/attachment", dependencies=[Depends(require_login)])
def toggle_attachment(doc_id: int, s: Session = Depends(get_session)):
    d = s.get(Document, doc_id)
    if not d or d.kind != "file":
        raise HTTPException(400, "Only uploaded files can be sent as attachments.")
    d.is_attachment = not d.is_attachment
    s.commit()
    return RedirectResponse(f"/projects/{d.project_id}", status_code=303)


@app.post("/projects/{project_id}/run/{kind}", dependencies=[Depends(require_login)])
def run_job(project_id: int, kind: str, rebuild: str = Form(""), s: Session = Depends(get_session)):
    if kind not in {"pipeline", "profile", "discover", "draft", "notify"}:
        raise HTTPException(400)
    _project_or_404(s, project_id)
    worker.enqueue(s, kind, project_id, {"rebuild_profile": bool(rebuild)})
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/add-opportunity", dependencies=[Depends(require_login)])
def add_opportunity(project_id: int, url: str = Form(...), pasted: str = Form(""), s: Session = Depends(get_session)):
    _project_or_404(s, project_id)
    if not url.strip().startswith(("http://", "https://")):
        raise HTTPException(400, "Enter the http(s) address of the call for proposals.")
    worker.enqueue(s, "add_opportunity", project_id, {"url": url.strip(), "pasted": pasted})
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


def _package_listing(m: Match) -> list[dict]:
    """Listed from the database copy, so the list is complete even after the server's disk was erased."""
    if not m.package_dir:
        return []
    current = set()
    for v in (m.package_files or {}).values():
        current.update(v if isinstance(v, list) else [v])
    rows = sorted(blobstore.list_dir(Path(m.package_dir)), key=lambda r: (r[2] is not None, r[2]), reverse=True)
    return [{"name": name, "current": name in current, "kb": max(1, size // 1024)} for name, size, _ts in rows]


@app.get("/review/{match_id}", response_class=HTMLResponse)
def review(match_id: int, request: Request, t: str | None = None, s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    events = s.scalars(select(AuditEvent).where(AuditEvent.match_id == m.id).order_by(AuditEvent.id)).all()
    applicant = s.get(ApplicantProfile, 1)
    return templates.TemplateResponse(request, "review.html", {
        "m": m, "o": m.opportunity, "t": t or "", "events": events, "applicant": applicant,
        "readiness": _readiness(m, applicant), "files": _package_listing(m),
        "unsupported": [f for f in m.fact_check or [] if f.get("status") in {"unsupported", "contradicted"}],
        "needs_password": bool(settings.ui_password),
    })


def _readiness(m: Match, a: ApplicantProfile | None) -> list[dict]:
    """Checks shown above the Authorize button. Warnings do not block; the human decides."""
    o = m.opportunity
    checks = []
    gaps = m.draft_gaps or []
    checks.append({"ok": not gaps, "text": f"{len(gaps)} open question(s) remain" if gaps else "No open questions"})
    placeholders = (m.draft_markdown or "").count("[APPLICANT:")
    checks.append({"ok": placeholders == 0,
                   "text": f"{placeholders} [APPLICANT: …] placeholder(s) still in the text" if placeholders
                   else "No placeholders left in the text"})
    bad = [f for f in m.fact_check or [] if f.get("status") in {"unsupported", "contradicted"}]
    fc_version = (m.draft_sections or {}).get("fact_check_version")
    if m.fact_check is not None:
        text = (f"{len(bad)} claim(s) not found in your material" if bad else "Every checked claim was found in your material")
        if fc_version and fc_version != m.version:
            text += f" (checked on version {fc_version}; you have edited since)"
        checks.append({"ok": not bad, "text": text})
    over = [r for r in m.length_report or [] if not r.get("ok")]
    if m.length_report:
        checks.append({"ok": not over, "text": f"{len(over)} section(s) over the funder's length limit" if over
                       else "Every section is within the funder's length limit"})
    if o.verification in {"unverified", "failed"}:
        checks.append({"ok": False, "text": f"Opportunity details not confirmed: {o.verification_note}"})
    if o.source == "grants_gov":
        checks.append({"ok": bool(o.nofo_text), "text": "Full funding notice was read" if o.nofo_text
                       else "The full funding notice could not be read; requirements come from the synopsis only"})
    if o.submission_channel == "grants_gov":
        checks.append({"ok": bool(a and a.sam_registered), "text": "SAM.gov registration (required for federal awards)"})
        checks.append({"ok": bool(a and a.grants_gov_registered), "text": "Grants.gov account with submitting role"})
    if o.submission_channel == "email":
        checks.append({"ok": bool(o.submission_target or o.contact_email),
                       "text": f"Funder email: {o.submission_target or o.contact_email or 'missing'}"})
        if settings.submission_test_mode:
            checks.append({"ok": True, "text": "Submission test mode is ON: the email will go to you, not the funder"})
    checks.append({"ok": bool(a and a.legal_name), "text": "Applicant legal name set in Settings" if a and a.legal_name
                   else "Applicant legal name missing (Settings)"})
    return checks


def _back(m: Match, t: str, extra: str = "") -> RedirectResponse:
    q = f"?t={t}" if t else "?"
    return RedirectResponse(f"/review/{m.id}{q}{extra}", status_code=303)


@app.post("/review/{match_id}/save")
def review_save(match_id: int, request: Request, t: str = Form(""), markdown_text: str = Form(...),
                user_notes: str = Form(""), submission_target: str = Form(""), submission_channel: str = Form(""),
                s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    if m.state in {"authorized", "submitting", "submitted"}:
        raise HTTPException(409, "This application has already been authorized; it can no longer be edited.")
    update_sections_from_markdown(m, markdown_text)
    m.user_notes = user_notes
    if submission_target.strip():
        m.opportunity.submission_target = submission_target.strip()[:2000]
    if submission_channel in {"email", "grants_gov", "portal", "unknown"}:
        m.opportunity.submission_channel = submission_channel
    audit(s, "edited", m.id, version=m.version)
    s.commit()
    return _back(m, t, "&saved=1")


@app.post("/review/{match_id}/authorize")
def review_authorize(match_id: int, request: Request, t: str = Form(""), confirm: str = Form(""),
                     authorized_by: str = Form(""), password: str = Form(""), s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    if confirm != "SUBMIT":
        raise HTTPException(400, "Type SUBMIT in the confirmation box to authorize.")
    if settings.ui_password and not secrets.compare_digest(password.encode(), settings.ui_password.encode()):
        ip = client_ip(request)
        login_limiter.fail(ip)
        if login_limiter.blocked(ip):
            raise HTTPException(429, "Too many failed attempts. Wait 15 minutes.")
        raise HTTPException(403, "Wrong password. Authorizing a submission requires the GrantForge password.")
    if m.state not in {"drafted", "notified", "ready_for_manual_upload", "failed"}:
        raise HTTPException(409, f"This application is {m.state}; it cannot be authorized now.")
    applicant = s.get(ApplicantProfile, 1)
    m.authorized_at = utcnow()
    m.authorized_by = (authorized_by or (applicant.contact_name if applicant else ""))[:200]
    m.state = "authorized"
    audit(s, "authorized", m.id, by=m.authorized_by, version=m.version, ip=client_ip(request))
    s.commit()
    worker.enqueue(s, "submit", m.project_id, {"match_id": m.id})
    return _back(m, t)


@app.post("/review/{match_id}/reject")
def review_reject(match_id: int, request: Request, t: str = Form(""), s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    if m.state in {"submitting", "submitted"}:
        raise HTTPException(409, "Already submitted.")
    m.state = "rejected"
    audit(s, "rejected_by_applicant", m.id)
    s.commit()
    return _back(m, t)


@app.post("/review/{match_id}/redraft")
def review_redraft(match_id: int, request: Request, t: str = Form(""), s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    if m.state in {"authorized", "submitting", "submitted"}:
        raise HTTPException(409, "Already authorized.")
    worker.enqueue(s, "draft", m.project_id, {"match_id": m.id})
    return _back(m, t, "&redraft=1")


@app.post("/review/{match_id}/mark-submitted")
def review_mark_submitted(match_id: int, request: Request, t: str = Form(""), confirmation: str = Form(""),
                          s: Session = Depends(get_session)):
    """For manual channels: the applicant records that they submitted it."""
    m = _load_match_for_review(match_id, t, request, s)
    m.state = "submitted"
    m.submitted_at = utcnow()
    m.submission_result = {**(m.submission_result or {}), "status": "submitted", "manual_confirmation": confirmation[:200]}
    audit(s, "submitted_manually", m.id, confirmation=confirmation[:200])
    s.commit()
    return _back(m, t)


@app.get("/review/{match_id}/download/{name:path}")
def download(match_id: int, name: str, request: Request, t: str | None = None, s: Session = Depends(get_session)):
    m = _load_match_for_review(match_id, t, request, s)
    if not m.package_dir:
        raise HTTPException(404)
    root = Path(m.package_dir).resolve()
    path = (root / name).resolve()
    if root not in path.parents or not blobstore.ensure_local(path):
        raise HTTPException(404)
    return FileResponse(str(path), filename=path.name)


# --------------------------------------------------------------------------
# Settings (applicant profile)
# --------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def settings_page(request: Request, s: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "settings.html", {"a": s.get(ApplicantProfile, 1), "spend": _spend()})


@app.post("/settings", dependencies=[Depends(require_login)])
async def settings_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    a = s.get(ApplicantProfile, 1)
    for field in ("legal_name", "entity_type", "uei", "ein", "contact_name", "contact_title", "contact_email",
                  "contact_phone", "address", "state", "country", "congressional_district", "website", "bio"):
        if field in form:
            setattr(a, field, str(form[field]).strip()[:5000])
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
