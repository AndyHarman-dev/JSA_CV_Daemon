"""ORM models: Job, Message, Document, FollowUp, RevisionRequest."""

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Enum as SAEnum, Index
from sqlalchemy import text
from datetime import datetime
import enum


class Base(DeclarativeBase): ...


class JobState(str, enum.Enum):
    queued = "queued"            # fresh ingest; parked until the user clicks LAUNCH
    pending = "pending"
    running = "running"
    awaiting_input = "awaiting_input"
    fit_done = "fit_done"        # fit assessment passed; ready for cv_adjust
    unfit = "unfit"              # fit assessment flagged a mismatch; parked for user decision
    cv_review = "cv_review"      # CV lane parked: tailored CV rendered, awaiting user approve/revise
    cv_done = "cv_done"
    cl_done = "cl_done"
    review = "review"
    approved = "approved"
    failed = "failed"
    dismissed = "dismissed"


class Stage(str, enum.Enum):
    fit_assessment = "fit_assessment"
    cv_adjust = "cv_adjust"
    cover_letter = "cover_letter"
    revising_cv = "revising_cv"
    revising_cl = "revising_cl"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_job_state", "state"),
    )
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # sha1[:16]
    company: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(255))
    link: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(1))                   # A | B | C
    jd: Mapped[str] = mapped_column(Text)
    jd_hash: Mapped[str] = mapped_column(String(16))
    cv_text: Mapped[str] = mapped_column(Text)                     # DEPRECATED — no longer read; cv_structure.json is the source of truth
    state: Mapped[JobState] = mapped_column(SAEnum(JobState))
    current_stage: Mapped[Stage | None] = mapped_column(SAEnum(Stage), nullable=True)
    session_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # backend resume token
    cv_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # resume token for cv_adjust stage
    cl_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # resume token for cover_letter stage
    backend_name: Mapped[str | None] = mapped_column(Text, nullable=True)  # active backend for this job (BF-19)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)  # active model rung for this job (model ladder); None = not yet hopped
    model_hops: Mapped[int] = mapped_column(Integer, default=0)  # number of model-ladder hops taken (capped at 5)
    base_cv_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # deck id from cv_decks.json; NULL = use the default deck. Set pre-launch only.
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)  # snapshot of the global language pref, set on LAUNCH; null until launched (falls back to the live global pref)
    fit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # agent's reason when state==unfit
    injection: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {prefix, postfix, first_msg}; NULL = none. See jsa/schema/injection.py
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages: Mapped[list["Message"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    follow_ups: Mapped[list["FollowUp"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    revision_requests: Mapped[list["RevisionRequest"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Message(Base):
    """Full transcript for replay. role in {system, user, assistant}."""
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_msg_job_stage", "job_id", "stage", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[Stage] = mapped_column(SAEnum(Stage))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # Finished, joined reasoning text for this turn (streaming's REASONING channel),
    # persisted only once the turn completes -- never the per-token buffer. Nullable:
    # most turns have no separate reasoning channel. _load_history projects rows to a
    # two-field HistoryTurn(role, content), so this column is structurally invisible
    # to replay -- no exclusion code needed.
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[Job] = relationship(back_populates="messages")


class Document(Base):
    """Markdown output per stage. Latest version is the highest `version` per (job, stage)."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[Stage] = mapped_column(SAEnum(Stage))
    version: Mapped[int] = mapped_column(Integer)
    markdown: Mapped[str] = mapped_column(Text)  # canonical Markdown serialized from `structured`
    structured: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON source-of-truth (CVDocument/CoverLetter)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # set on approval
    docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # set on approval
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[Job] = relationship(back_populates="documents")


class FollowUp(Base):
    __tablename__ = "follow_ups"
    __table_args__ = (
        Index("ix_followup_job_answered", "job_id", "answered_at"),
        Index(
            "uq_followup_open",
            "job_id",
            "stage",
            unique=True,
            sqlite_where=text("answered_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[Stage] = mapped_column(SAEnum(Stage))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_replies: Mapped[str | None] = mapped_column(Text, nullable=True)
    asked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    job: Mapped[Job] = relationship(back_populates="follow_ups")


class RevisionRequest(Base):
    """User-requested revision against a Document. Consumed by the orchestrator."""
    __tablename__ = "revision_requests"
    __table_args__ = (
        Index(
            "uq_revision_open",
            "job_id",
            unique=True,
            sqlite_where=text("consumed_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    target: Mapped[Stage] = mapped_column(SAEnum(Stage))           # cv_adjust | cover_letter (the doc being revised)
    instruction: Mapped[str] = mapped_column(Text)
    origin_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # JobState value the revision was requested from: "cv_review" | "review".
    # NULL (legacy rows) is read as "review".
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[Job] = relationship(back_populates="revision_requests")
