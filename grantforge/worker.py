"""Background worker: a thread that drains the ``jobs`` table.

Runs inside the web process (started on app startup) so a single ``uvicorn``
command is the whole deployment. It also runs:

* the autopilot loop when GF_AUTOPILOT_INTERVAL_HOURS > 0, and
* daily maintenance (backups, deadline reminders, spend-cap notice).

Jobs are claimed with a conditional UPDATE, so even if several processes run
a worker, each job runs once. Set GF_RUN_WORKER=false on extra web processes.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import timedelta

from sqlalchemy import select, update

from .config import settings
from .db import Job, Match, Project, SessionLocal, utcnow
from .pipeline import run_discover, run_draft, run_notify, run_pipeline, run_profile, run_score
from .submit import submit_match

log = logging.getLogger(__name__)

_stop = threading.Event()
STALE_AFTER = timedelta(hours=3)


def enqueue(session, kind: str, project_id: int | None = None, payload: dict | None = None) -> Job:
    job = Job(kind=kind, project_id=project_id, payload=payload or {})
    session.add(job)
    session.commit()
    return job


def claim_next_job() -> int | None:
    """Atomically move the oldest queued job to running and return its id."""
    with SessionLocal() as s:
        for job_id in s.scalars(select(Job.id).where(Job.status == "queued").order_by(Job.id).limit(5)).all():
            res = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued")
                            .values(status="running", started_at=utcnow()))
            s.commit()
            if res.rowcount == 1:
                return job_id
    return None


def run_job(job_id: int) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return

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
                    from .drafting import draft_match

                    draft_match(s, s.get(Match, payload["match_id"]), log_fn)
                else:
                    drafted = run_draft(s, project, log_fn)
                    run_notify(s, project, drafted, log_fn)
            elif job.kind == "add_opportunity":
                from .matching import score_project
                from .sources import upsert_opportunities
                from .sources.manual_url import opportunity_from_url

                log_fn(f"Reading {payload['url']}")
                rows = upsert_opportunities(s, [opportunity_from_url(payload["url"], payload.get("pasted", ""))])
                log_fn(f"Added '{rows[0].title[:80]}' (deadline {rows[0].close_date or 'not stated'})")
                if project.profile:
                    score_project(s, project, rows, log_fn)
                else:
                    log_fn("The project has no profile yet; it will be scored on the next run")
            elif job.kind == "notify":
                run_notify(s, project, None, log_fn)
            elif job.kind == "submit":
                result = submit_match(s, s.get(Match, payload["match_id"]), log_fn)
                log_fn(f"Submission result: {result.get('status')}")
            else:
                raise RuntimeError(f"Unknown job kind {job.kind}")
            job.status = "done"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{exc}\n\n{traceback.format_exc()}"
            job.progress = f"Failed: {exc}"
            log.exception("job %s failed", job_id)
        finally:
            job.finished_at = utcnow()
            s.commit()


def recover_stale_jobs(older_than: timedelta | None = STALE_AFTER) -> None:
    """Jobs left 'running' by a stopped process. Pipeline-type jobs are re-queued; a submit job is
    marked failed instead, because it may already have sent an email and must not send twice.

    At worker start-up every 'running' job is stale (only one worker process should run; see
    GF_RUN_WORKER), so start() passes older_than=None."""
    cutoff = utcnow() - (older_than or timedelta(0))
    with SessionLocal() as s:
        for j in s.scalars(select(Job).where(Job.status == "running")).all():
            started = j.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=cutoff.tzinfo)
            if older_than is not None and started is not None and started > cutoff:
                continue  # probably still running in another process
            if j.kind == "submit":
                j.status = "failed"
                j.error = ("The process stopped while this submission was running. Check the funder's inbox or "
                           "portal before authorizing again, so the application is not sent twice.")
                m = s.get(Match, (j.payload or {}).get("match_id"))
                if m is not None and m.state == "submitting":
                    m.state = "failed"
                    m.submission_result = {"status": "failed", "error": j.error}
            else:
                j.status = "queued"
                j.append_log("Re-queued after an interrupted run")
        s.commit()


def _loop() -> None:
    from . import maintenance

    last_autopilot = time.time()
    while not _stop.is_set():
        job_id = claim_next_job()
        if job_id:
            run_job(job_id)
            continue
        if maintenance.due_today():
            log.info("daily maintenance: %s", maintenance.run_daily())
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
    if not settings.run_worker or (_thread and _thread.is_alive()):
        return
    recover_stale_jobs(older_than=None)
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="grantforge-worker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
