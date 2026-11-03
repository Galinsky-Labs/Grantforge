"""Daily maintenance run by the worker: backups, deadline reminders, spend-cap notice."""
from __future__ import annotations

import logging
import sqlite3
import tarfile
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import AuditEvent, Match, SessionLocal, audit

log = logging.getLogger(__name__)

MARKER = "last_maintenance"


def due_today() -> bool:
    marker = settings.data_dir / f".{MARKER}"
    today = date.today().isoformat()
    if marker.exists() and marker.read_text().strip() == today:
        return False
    marker.write_text(today)
    return True


def run_daily() -> dict:
    out = {}
    for name, fn in (("backup", backup), ("reminders", send_reminders), ("budget", budget_notice)):
        try:
            out[name] = fn()
        except Exception as exc:  # one failure must not stop the others
            log.exception("maintenance step %s failed", name)
            out[name] = f"failed: {exc}"
    return out


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

def backup() -> str:
    """Write data/backups/grantforge-backup-<date>.tar.gz with a consistent database snapshot,
    the uploads and the packages. Keeps the newest GF_BACKUP_KEEP archives.

    With Postgres (Neon) the database, including every stored file, is backed up by Neon itself
    (restore history and scheduled snapshots), so no local archive is written: on hosts such as
    Render the local disk is erased on every restart and an archive there would be lost anyway."""
    if not settings.database_url.startswith("sqlite"):
        return "skipped: Postgres database is backed up by Neon (restore history and snapshots)"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    dest = settings.backup_path / f"grantforge-backup-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="gf_backup_") as tmp:
        snapshot = Path(tmp) / "grantforge.db"
        if settings.database_url.startswith("sqlite:///"):
            src_path = settings.database_url.removeprefix("sqlite:///")
            src = sqlite3.connect(src_path)
            dst = sqlite3.connect(str(snapshot))
            with dst:
                src.backup(dst)  # SQLite online backup: consistent even while the app is writing
            src.close()
            dst.close()
        with tarfile.open(dest, "w:gz") as tar:
            if snapshot.exists():
                tar.add(snapshot, arcname="grantforge.db")
            for sub in ("uploads", "packages"):
                p = settings.data_dir / sub
                if p.exists():
                    tar.add(p, arcname=sub)
            secret = settings.data_dir / ".app_secret"
            if secret.exists():
                tar.add(secret, arcname=".app_secret")
    archives = sorted(settings.backup_path.glob("grantforge-backup-*.tar.gz"))
    for old in archives[: max(0, len(archives) - settings.backup_keep)]:
        old.unlink(missing_ok=True)
    log.info("backup written: %s", dest)
    return str(dest)


# --------------------------------------------------------------------------
# Deadline reminders
# --------------------------------------------------------------------------

def _days_left(close_date: str) -> int | None:
    try:
        return (datetime.strptime(close_date[:10], "%Y-%m-%d").date() - date.today()).days
    except (TypeError, ValueError):
        return None


def send_reminders() -> int:
    from .notify import send_deadline_reminders

    thresholds = settings.reminder_day_list
    if not thresholds:
        return 0
    with SessionLocal() as s:
        pending = s.scalars(select(Match).where(Match.state.in_(["drafted", "notified", "ready_for_manual_upload"]))).all()
        due: list[tuple[Match, int]] = []
        for m in pending:
            days = _days_left(m.opportunity.close_date)
            if days is None or days < 0:
                continue
            # The smallest threshold at or above days_left, sent once per threshold.
            hit = [t for t in thresholds if days <= t]
            if not hit:
                continue
            threshold = min(hit)
            tag = f"reminder_{threshold}"
            already = s.scalar(select(AuditEvent).where(AuditEvent.match_id == m.id, AuditEvent.event == tag))
            if already:
                continue
            due.append((m, days))
            audit(s, tag, m.id, days_left=days)
        if due:
            send_deadline_reminders(due)
        s.commit()
        return len(due)


# --------------------------------------------------------------------------
# Spend cap notice (once per month)
# --------------------------------------------------------------------------

def budget_notice() -> bool:
    from .llm import month_spend_usd
    from .notify import send_budget_notice

    if not settings.monthly_budget_usd:
        return False
    spent = month_spend_usd()
    if spent < settings.monthly_budget_usd:
        return False
    tag = f"budget_cap_{date.today():%Y-%m}"
    with SessionLocal() as s:
        if s.scalar(select(AuditEvent).where(AuditEvent.event == tag)):
            return False
        audit(s, tag, None, spent=round(spent, 2))
        s.commit()
    send_budget_notice(spent)
    return True
