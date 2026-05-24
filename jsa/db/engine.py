"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from jsa.db.models import Base


def make_engine(db_path: str):
    """Create an async SQLAlchemy engine. db_path may be ':memory:' for tests."""
    url = f"sqlite+aiosqlite:///{db_path}"
    return create_async_engine(url, echo=False)


def make_session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine):
    """Run create_all — creates tables if they don't exist. Safe to call on re-run."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
