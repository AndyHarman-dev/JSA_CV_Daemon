"""U1 — DB durability: WAL journal mode + busy_timeout on every real sqlite connection.

Covers both engine constructors (`make_engine` for the legacy call site and
`create_engine` for the Phase 8 call site used by server.py/cli.py) since each builds
its own `create_async_engine(...)` and must independently register the connect-event
PRAGMA listener.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from jsa.db.engine import create_engine, make_engine


async def _read_pragmas(engine):
    async with engine.connect() as conn:
        journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        synchronous = (await conn.execute(text("PRAGMA synchronous"))).scalar()
    return journal_mode, busy_timeout, synchronous


@pytest.mark.asyncio
async def test_create_engine_sets_wal_and_busy_timeout(tmp_path: Path):
    """The Phase 8 `create_engine(Path)` call site (server.py/cli.py) must open every
    connection in WAL mode with a 5s busy_timeout, so concurrent writers retry instead
    of raising SQLITE_BUSY -> OperationalError immediately."""
    db_path = tmp_path / "jsa.db"
    engine = create_engine(db_path)
    try:
        journal_mode, busy_timeout, synchronous = await _read_pragmas(engine)
        assert journal_mode.lower() == "wal"
        assert busy_timeout == 5000
        assert synchronous == 1  # NORMAL
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_make_engine_sets_wal_and_busy_timeout(tmp_path: Path):
    """The legacy `make_engine(str)` call site must get the same durability PRAGMAs."""
    db_path = tmp_path / "jsa_legacy.db"
    engine = make_engine(str(db_path))
    try:
        journal_mode, busy_timeout, synchronous = await _read_pragmas(engine)
        assert journal_mode.lower() == "wal"
        assert busy_timeout == 5000
        assert synchronous == 1  # NORMAL
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pragmas_applied_on_every_new_connection(tmp_path: Path):
    """The connect-event listener must fire per-connection, not just once at engine
    creation — open two separate connections from a pooled engine and confirm both see
    the PRAGMAs applied."""
    db_path = tmp_path / "jsa_multi.db"
    engine = create_engine(db_path)
    try:
        first = await _read_pragmas(engine)
        second = await _read_pragmas(engine)
        assert first == second == ("wal", 5000, 1)
    finally:
        await engine.dispose()
