"""Async SQLAlchemy engine and session factory."""

from pathlib import Path

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
