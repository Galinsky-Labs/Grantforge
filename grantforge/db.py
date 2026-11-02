"""Database models (SQLAlchemy 2.0, SQLite by default)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str] = mapped_column(Text, default="")  # free-text the user types in
    profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # structured project profile
    status: Mapped[str] = mapped_column(String(40), default="new")  # new|profiled|searched|drafted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    documents: Mapped[list["Document"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    matches: Mapped[list["Match"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Document(Base):
    """One ingested input: an uploaded file, a repository, or a URL."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(String(20))  # file|repo|url|text
    name: Mapped[str] = mapped_column(String(500))
    path: Mapped[str] = mapped_column(String(1000), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="documents")


class Opportunity(Base):
    """A funding opportunity from any source, normalised."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))  # grants_gov|web|manual
    source_id: Mapped[str] = mapped_column(String(200), index=True)  # unique within source
    title: Mapped[str] = mapped_column(String(1000))
    funder: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(String(2000), default="")
    status: Mapped[str] = mapped_column(String(40), default="")  # posted|forecasted|closed|unknown
    open_date: Mapped[str] = mapped_column(String(40), default="")
    close_date: Mapped[str] = mapped_column(String(40), default="")
    award_floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    award_ceiling: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applicant_types: Mapped[list | None] = mapped_column(JSON, nullable=True)
    eligibility_text: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    contact_email: Mapped[str] = mapped_column(String(300), default="")
    submission_channel: Mapped[str] = mapped_column(String(40), default="unknown")  # grants_gov|email|portal|unknown
    submission_target: Mapped[str] = mapped_column(String(2000), default="")  # email address or portal URL
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    matches: Mapped[list["Match"]] = relationship(back_populates="opportunity")

    def as_prompt_text(self, limit: int = 6000) -> str:
        parts = [
            f"Title: {self.title}",
            f"Funder: {self.funder}",
            f"Status: {self.status}",
            f"Opens: {self.open_date}  Closes: {self.close_date}",
            f"Award range: {self.award_floor or 'n/a'} – {self.award_ceiling or 'n/a'}",
            f"Applicant types: {', '.join(self.applicant_types or []) or 'n/a'}",
            f"URL: {self.url}",
        ]
        if self.eligibility_text:
            parts.append(f"Eligibility detail: {self.eligibility_text[:1500]}")
        parts.append("Description:\n" + (self.description or "")[:limit])
        return "\n".join(parts)


class Match(Base):
    """The scored link between a project and an opportunity, and its draft."""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"))
    score: Mapped[int] = mapped_column(Integer, default=0)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    blockers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # lifecycle: candidate -> shortlisted -> drafted -> notified -> authorized -> submitted | rejected | failed
    state: Mapped[str] = mapped_column(String(30), default="candidate")

    draft_markdown: Mapped[str] = mapped_column(Text, default="")
    draft_sections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_checklist: Mapped[list | None] = mapped_column(JSON, nullable=True)  # required forms/attachments
    draft_gaps: Mapped[list | None] = mapped_column(JSON, nullable=True)  # questions for the applicant
    package_dir: Mapped[str] = mapped_column(String(1000), default="")

    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorized_by: Mapped[str] = mapped_column(String(200), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    user_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="matches")
    opportunity: Mapped[Opportunity] = relationship(back_populates="matches")


class ApplicantProfile(Base):
    """Who is applying. Single row. Everything a form like the SF-424 asks for."""

    __tablename__ = "applicant_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(300), default="")
    entity_type: Mapped[str] = mapped_column(String(60), default="individual")
    # individual | for_profit | small_business | nonprofit_501c3 | nonprofit_other | university | government | tribal
    uei: Mapped[str] = mapped_column(String(40), default="")  # SAM.gov Unique Entity ID
    ein: Mapped[str] = mapped_column(String(20), default="")
    sam_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    grants_gov_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    contact_title: Mapped[str] = mapped_column(String(200), default="")
    contact_email: Mapped[str] = mapped_column(String(300), default="")
    contact_phone: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(10), default="")
    country: Mapped[str] = mapped_column(String(60), default="USA")
    congressional_district: Mapped[str] = mapped_column(String(20), default="")
    website: Mapped[str] = mapped_column(String(300), default="")
    bio: Mapped[str] = mapped_column(Text, default="")  # organisation / PI background used in narratives
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def as_prompt_text(self) -> str:
        return json.dumps(
            {
                "legal_name": self.legal_name,
                "entity_type": self.entity_type,
                "sam_registered": self.sam_registered,
                "grants_gov_registered": self.grants_gov_registered,
                "contact_name": self.contact_name,
                "contact_title": self.contact_title,
                "contact_email": self.contact_email,
                "state": self.state,
                "country": self.country,
                "website": self.website,
                "bio": self.bio,
            },
            indent=1,
        )


class Job(Base):
    """Background work item processed by the worker thread."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))  # pipeline|profile|discover|draft|submit|autopilot
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    progress: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project | None] = relationship(back_populates="jobs")

    def append_log(self, line: str) -> None:
        stamp = utcnow().strftime("%H:%M:%S")
        self.log = (self.log or "") + f"[{stamp}] {line}\n"
        self.progress = line


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str] = mapped_column(String(60))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Engine / session helpers
# --------------------------------------------------------------------------

_connect_args: dict[str, Any] = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        if s.get(ApplicantProfile, 1) is None:
            s.add(ApplicantProfile(id=1, contact_email=settings.notify_to))
            s.commit()


def get_session() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def audit(session: Session, event_name: str, match_id: int | None = None, **detail: Any) -> None:
    session.add(AuditEvent(match_id=match_id, event=event_name, detail=detail or None))
