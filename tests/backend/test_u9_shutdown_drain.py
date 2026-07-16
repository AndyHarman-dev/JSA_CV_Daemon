"""U9: shutdown drains in-flight Orchestrator worker tasks before disposing
the DB engine.

Problem (see CLAUDE.md's checkpoint-atomicity rule): jsa/server.py's
_shutdown() used to set orchestrator._stopping = True (stopping NEW
dispatch) and then immediately dispose() the engine, without waiting for any
_run_one task that might still be mid-commit. If a task's checkpoint() commit
raced engine.dispose(), that commit could fail against a disposed engine.

These tests exercise the real create_app()/_shutdown() via FastAPI's
lifespan_context, with a fake in-flight task planted directly into
Orchestrator._tasks (the same dict Orchestrator.run() populates in
production — see jsa/pipeline/orchestrator.py). We patch Orchestrator.run to
a no-op (as every other test in this suite does) so the dispatch loop itself
never runs; only the shutdown-drain behavior is under test.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jsa.config import Settings
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    from tests.backend.fakes.fake_backend import FakeAgentBackend

    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name: FakeAgentBackend([]))

    settings = Settings(
        output_dir=tmp_path / "output",
        db_path=tmp_path / "test.sqlite",
        backend="claude-cli",
        port=8765,
        no_browser=True,
    )
    return create_app(settings)


class TestShutdownDrain:
    async def test_shutdown_awaits_inflight_task_before_disposing_engine(
        self, test_app, monkeypatch
    ):
        order: list[str] = []

        # AsyncEngine uses __slots__, so its `dispose` bound method can't be
        # reassigned on the instance — patch the class method instead (scoped
        # to this test by monkeypatch's automatic teardown).
        real_dispose = AsyncEngine.dispose

        async def fake_dispose(self) -> None:
            order.append("engine_disposed")
            await real_dispose(self)

        monkeypatch.setattr(AsyncEngine, "dispose", fake_dispose)

        async with test_app.router.lifespan_context(test_app):
            orchestrator = test_app.state.orchestrator

            async def fake_job() -> None:
                await asyncio.sleep(0.2)
                order.append("task_completed")

            task = asyncio.create_task(fake_job())
            orchestrator._tasks["fake-job-id"] = task
            # Exiting this `async with` block fires FastAPI's shutdown event,
            # which should await `task` (via Orchestrator._tasks) BEFORE
            # calling engine.dispose().

        assert order == ["task_completed", "engine_disposed"]
        assert task.done()

    async def test_shutdown_sets_stopping_before_draining(self, test_app):
        """_stopping must flip before the drain-await, so no NEW job gets
        dispatched while shutdown is waiting for in-flight ones to finish."""
        seen_stopping_at_task_start = []

        async with test_app.router.lifespan_context(test_app):
            orchestrator = test_app.state.orchestrator

            async def fake_job() -> None:
                # By the time _shutdown's gather() gets a chance to run this
                # task, _stopping should already be True.
                await asyncio.sleep(0)
                seen_stopping_at_task_start.append(orchestrator._stopping)

            task = asyncio.create_task(fake_job())
            orchestrator._tasks["fake-job-id"] = task

        assert seen_stopping_at_task_start == [True]

    async def test_shutdown_with_no_inflight_tasks_still_disposes_engine(
        self, test_app, monkeypatch
    ):
        """Sanity check: the drain step is a no-op (not a hang) when
        Orchestrator._tasks is empty, and the engine still gets disposed."""
        disposed = []
        real_dispose = AsyncEngine.dispose

        async def fake_dispose(self) -> None:
            disposed.append(True)
            await real_dispose(self)

        monkeypatch.setattr(AsyncEngine, "dispose", fake_dispose)

        async with test_app.router.lifespan_context(test_app):
            pass

        assert disposed == [True]
