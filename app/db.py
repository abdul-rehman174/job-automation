"""Persistent storage: schema, session factory, and lifecycle helpers.

Runs on SQLite by default (reliable, zero-config, transactional). The schema
is intentionally relational so application state survives restarts.
"""
from __future__ import annotations

import datetime as _dt
from enum import Enum as PyEnum
from pathlib import Path

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _dt_json(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat() if dt.tzinfo else dt.replace(tzinfo=dt.isoformat() and _dt.timezone.utc).isoformat()


# ---------------------------------------------------------------- enums

class AppStatus(str, PyEnum):
    discovered = "discovered"
    evaluating = "evaluating"
    eligible = "eligible"
    preparing = "preparing"
    preparing_failed = "preparing_failed"
    applying = "applying"
    submitted = "submitted"
    needs_verification = "needs_verification"
    skipped = "skipped"
    needs_manual_review = "needs_manual_review"
    ineligible = "ineligible"
    failed = "failed"
    duplicate = "duplicate"


class SkipCodes(str, PyEnum):
    CAPTCHA = "captcha"
    OTP = "otp"
    LOGIN_REQUIRED = "login_required"
    MISSING_INFO = "missing_information"
    UNSURE_ANSWER = "unverified_answer_required"
    LEGAL_DECLARATION = "legal_declaration"
    ATS_UNVALIDATED = "ats_not_validated"
    ATS_UNSUPPORTED = "ats_unsupported"
    FORM_UNPARSABLE = "form_not_reliably_parsable"
    NOT_FOUND = "job_expired_or_missing"
    ELIGIBLE_DECLINE = "eligibility_declined_at_apply"
    HUMAN_REQUIRED = "human_verification_required"


# ---------------------------------------------------------------- tables

class ProfileEntry(Base):
    """One verified piece of candidate information (answer, contact field, flag)."""
    __tablename__ = "profile_entries"
    __table_args__ = (UniqueConstraint("key", "variant", name="uq_profile_key_variant"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)      # e.g. first_name, years_python, work_authorization
    variant: Mapped[str] = mapped_column(String(64), default="*") # optional per-question overload
    value: Mapped[str] = mapped_column(Text)
    human_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_human_approval: Mapped[bool] = mapped_column(Boolean, default=False)  # legal/EEO-type
    source: Mapped[str] = mapped_column(String(64), default="")   # where verified from
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class CVRecord(Base):
    __tablename__ = "cv_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)   # display name
    path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    keywords: Mapped[str] = mapped_column(Text, default="")       # comma separated relevance keywords
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    usages: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    company: Mapped[str] = mapped_column(String(256))
    canonical_url: Mapped[str] = mapped_column(String(1024), index=True)
    apply_url: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(256), default="")
    remote_status: Mapped[str] = mapped_column(String(32), default="unknown")  # remote|hybrid|onsite|unknown
    country_eligible: Mapped[bool] = mapped_column(Boolean, default=True)      # verified eligibility, not assumed
    country_evidence: Mapped[str] = mapped_column(String(768), default="")
    salary: Mapped[str] = mapped_column(String(128), default="")
    employment_type: Mapped[str] = mapped_column(String(64), default="")
    published_at: Mapped[str] = mapped_column(String(64), default="")
    ats: Mapped[str] = mapped_column(String(32), default="", index=True)
    source_id: Mapped[str] = mapped_column(String(48), index=True)   # source name, e.g. greenhouse_api
    source_url: Mapped[str] = mapped_column(String(1024), default="")
    ats_job_id: Mapped[str] = mapped_column(String(128), default="")
    dedupe_key: Mapped[str] = mapped_column(String(384), index=True)
    discovered_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    last_verified_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)   # original employer listing verified
    eligibility_status: Mapped[str] = mapped_column(String(32), default="", index=True)
    eligibility_score: Mapped[float] = mapped_column(Float, default=0.0)
    eligibility_reasons: Mapped[str] = mapped_column(Text, default="")


class Application(Base):
    """One application execution. Each row owns its state machine independently."""
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", name="uq_app_job"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job = relationship("Job")
    status: Mapped[str] = mapped_column(String(32), default=AppStatus.eligible.value, index=True)
    cv_id: Mapped[int | None] = mapped_column(ForeignKey("cv_records.id"), nullable=True)
    cv_name: Mapped[str] = mapped_column(String(128), default="")
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    prepared_json: Mapped[str] = mapped_column(Text, default="")   # form answers package
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    skip_code: Mapped[str] = mapped_column(String(64), default="")
    skip_reason: Mapped[str] = mapped_column(Text, default="")
    confirmation_text: Mapped[str] = mapped_column(Text, default="")
    confirmation_url: Mapped[str] = mapped_column(String(1024), default="")
    confirmation_slug: Mapped[str] = mapped_column(String(128), default="")
    evidence_dir: Mapped[str] = mapped_column(String(512), default="")
    started_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)
    evidence = relationship("EvidenceRecord", back_populates="application", uselist=True, cascade="all, delete-orphan")


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    application = relationship("Application", foreign_keys=[application_id])
    kind: Mapped[str] = mapped_column(String(32))    # screenshot|page_text|confirm|form_snapshot
    path: Mapped[str] = mapped_column(String(512), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class RunRecord(Base):
    __tablename__ = "run_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default="running")  # running|paused|finished|stopped
    started_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)
    stage: Mapped[str] = mapped_column(String(48), default="discover")  # discover|apply|finalize
    note: Mapped[str] = mapped_column(Text, default="")


class SourceRun(Base):
    """Health monitoring per source execution."""
    __tablename__ = "source_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(48), index=True)
    executed_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    results_returned: Mapped[int] = mapped_column(Integer, default=0)
    valid_listings: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str] = mapped_column(Text, default="")
    pagination_progress: Mapped[str] = mapped_column(String(256), default="")
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info|warn|error
    scope: Mapped[str] = mapped_column(String(48), default="")
    message: Mapped[str] = mapped_column(Text)


class ATSCredential(Base):
    """Placeholder for optional authenticated sessions; secrets never logged."""
    __tablename__ = "ats_credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    ats: Mapped[str] = mapped_column(String(32), unique=True)
    storage_note: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------- engine

_engine = None
_SessionLocal = None


def init_engine(db_path: str) -> None:
    global _engine, _SessionLocal
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{p}", future=True, connect_args={"check_same_thread": False})

    # SQLite WAL for restart-safe crash recovery.
    @event.listens_for(_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, future=True, expire_on_commit=False)


def get_session() -> Session:
    if _SessionLocal is None:
        init_engine(str(DATA_DIR / "jobagent.db"))
    return _SessionLocal()


def log_event(session: Session, message: str, level: str = "info", scope: str = "") -> None:
    session.add(Event(level=level, scope=scope, message=message))


def log_and_print(session: Session, message: str, level: str = "info", scope: str = "") -> None:
    # Never log secrets; messages are pre-redacted by callers.
    tag = {"info": "·", "warn": "!", "error": "✗"}[level]
    print(f"{tag} [{scope or 'agent'}] {message}")
    log_event(session, message, level=level, scope=scope)


def reset_db(db_path: str) -> None:
    p = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        f = p.parent / (p.name + suffix)
        if f.exists():
            f.unlink()
    init_engine(db_path)
