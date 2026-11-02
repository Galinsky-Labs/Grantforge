"""Background worker: a thread that drains the ``jobs`` table.

Runs inside the web process (started on app startup) so a single
``uvicorn`` command is the whole deployment. Also runs the autopilot loop
when GF_AUTOPILOT_INTERVAL_HOURS > 0: re-discovers and re-scores every project
on a schedule so new opportunities arrive by email without anyone clicking.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from sqlalchemy import select

from .config import settings
from .db import Job, Match, Project, SessionLocal, utcnow
from .pipeline import run_discover, run_draft, run_notify, run_pipeline, run_profile, run_score
from .submit import submit_match

log = logging.getLogger(__name__)

_stop = threading.Event()


def enqueue(session, kind: str, project_id: int | None = None, payload: dict | None = None) -> Job:
    job = Job(kind=kind, project_id=project_id, payload=payload or {})
    session.add(job)
    session.commit()
    return job


def _run_job(job_id: int) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        job.status = "running"
        job.started_at = utcnow()
        s.commit()

        def log_fn(line: str) -> None:
            log.info("job %s: %s", job_id, line)
            job.append_log(line)
            s.commit()

        try:
            project = s.get(Project, job.project_id) if job.project_id else None
            payload = job.payload or {}
            if job.kind == "pipeline":
                result = run_pipeline(s, project, log_fn, rebuild_profile=payload.get("rebuild_profile", False))
                log_fn(f"Pipeline finished: {result}")
            elif job.kind == "profile":
                run_profile(s, project, log_fn)
            elif job.kind == "discover":
                opps = run_discover(s, project, log_fn)
                run_score(s, project, opps, log_fn)
            elif job.kind == "draft":
                if payload.get("match_id"):
                    m = s.get(Match, payload["match_id"])
                    from .drafting import draft_match

                    draft_match(s, m, log_fn)
                else:
                    drafted = run_draft(s, project, log_fn)
                    run_notify(s, project, drafted, log_fn)
            elif job.kind == "notify":
                run_notify(s, project, None, log_fn)
            elif job.kind == "submit":
                m = s.get(Match, payload["match_id"])
                result = submit_match(s, m, log_fn)
                log_fn(f"Submission result: {result.get('status')}")
            else:
                raise RuntimeError(f"Unknown job kind {job.kind}")
            job.status = "done"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{exc}\n\n{traceback.format_exc()}"
            log.exception("job %s failed", job_id)
        finally:
            job.finished_at = utcnow()
            s.commit()


def _loop() -> None:
    last_autopilot = time.time()
    while not _stop.is_set():
        job_id = None
        with SessionLocal() as s:
            job = s.scalars(select(Job).where(Job.status == "queued").order_by(Job.id)).first()
            if job:
                job_id = job.id
        if job_id:
            _run_job(job_id)
            continue
        if settings.autopilot_interval_hours > 0 and time.time() - last_autopilot > settings.autopilot_interval_hours * 3600:
            last_autopilot = time.time()
            with SessionLocal() as s:
                for p in s.scalars(select(Project).where(Project.profile.is_not(None))).all():
                    enqueue(s, "pipeline", p.id, {"autopilot": True})
            continue
        _stop.wait(2.0)


_thread: threading.Thread | None = None


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    # Requeue anything that was mid-flight when the process last stopped.
    with SessionLocal() as s:
        for j in s.scalars(select(Job).where(Job.status == "running")).all():
            j.status = "queued"
        s.commit()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="grantforge-worker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
