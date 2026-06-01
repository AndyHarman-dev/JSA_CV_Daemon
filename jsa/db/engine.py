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
