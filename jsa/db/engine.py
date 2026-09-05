"""Async SQLAlchemy engine and session factory."""

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from jsa.db.models import Base


def make_engine(db_path: str):
    """Create an async SQLAlchemy engine. db_path may be ':memory:' for tests."""
    url = f"sqlite+aiosqlite:///{db_path}"
    return create_async_engine(url, echo=False)


def make_session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine) -> None:
    """Run create_all — creates tables if they don't exist. Safe to call on re-run."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns to existing databases that predate this migration.
        # SQLite raises OperationalError if the column already exists; safe to ignore.
        for col in ("cv_session_id", "cl_session_id"):
            try:
                await conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} VARCHAR(128)"))
            except OperationalError:
                pass  # column already exists — safe to ignore
        # retry_count uses a different type/clause so it gets its own migration block.
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # docx_path added in BF-20.
        try:
            await conn.execute(text("ALTER TABLE documents ADD COLUMN docx_path TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # backend_name added in BF-19: tracks the active backend per job.
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN backend_name TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # fit_reason: agent's reason when the fit-assessment stage parks a job as unfit.
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN fit_reason TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # language: snapshot of the global language pref, set on launch.
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN language VARCHAR(8)"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # origin_state: JobState the revision was requested from ("cv_review" | "review");
        # NULL (legacy rows) is read as "review". Added for the CV/cover-letter lane split.
        try:
            await conn.execute(text("ALTER TABLE revision_requests ADD COLUMN origin_state VARCHAR(24)"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # model_name / model_hops: per-job model-ladder tracking (BF-19 model-first
        # fallback). Two separate try/except blocks, deliberately not one — if a DB
        # already has model_name (partial prior migration) but not model_hops, a single
        # block would raise on the first ALTER and skip the second forever.
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN model_name TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN model_hops INTEGER NOT NULL DEFAULT 0"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # suggested_replies: JSON-encoded list of model-generated quick-reply strings,
        # populated only on a structured-mode question turn (agent chat upgrade Phase 4).
        try:
            await conn.execute(text("ALTER TABLE follow_ups ADD COLUMN suggested_replies TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # reasoning: finished, joined reasoning text for a streamed turn (agent chat
        # upgrade Phase 6). Nullable — most turns have none.
        try:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN reasoning TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore
        # base_cv_id: deck id from cv_decks.json (CV Decks feature, Phase 3). NULL means
        # "use the default deck". Assignable only pre-launch (state == queued).
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN base_cv_id VARCHAR(32)"))
        except OperationalError:
            pass  # column already exists — safe to ignore

        # injection: per-job prompt overrides as JSON {prefix, postfix, first_msg};
        # NULL = none (the normalized all-blank case too). See jsa/schema/injection.py.
        try:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN injection TEXT"))
        except OperationalError:
            pass  # column already exists — safe to ignore


# ---------------------------------------------------------------------------
# New-style helpers used by server.py and cli.py (Phase 8)
# ---------------------------------------------------------------------------


def create_engine(db_path: Path) -> AsyncEngine:
    """Create an async SQLAlchemy engine from a Path."""
    url = f"sqlite+aiosqlite:///{db_path}"
    return create_async_engine(url, echo=False)


def create_session_factory(engine: AsyncEngine):
    """Return an async session factory bound to the given engine."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
