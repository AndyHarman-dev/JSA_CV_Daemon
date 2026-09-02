"""Tests for Phase 3 of the model-fallback-ladder plan: per-job model tracking.

Covers:
1. Job.model_name / Job.model_hops round-trip through the DB.
2. Orchestrator._wrap_factory correctly detects the three factory shapes (zero-arg
   legacy, old-style one-arg `(name)`, new-style `(name, model=None)`) and calls each
   the right way -- in particular, the one-arg case must NOT receive a model.
3. Both reset paths (soft_reset_job, nuclear_reset_job) clear model_name/model_hops.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.db import repo
from jsa.db.models import Base, Job, JobState
from jsa.pipeline.orchestrator import _wrap_factory


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


def _job_data(job_id: str | None = None, **overrides) -> dict:
    data = dict(
        id=job_id or uuid4().hex[:16],
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description",
        jd_hash="hash0000deadbeef",
        cv_text="CV text",
    )
    data.update(overrides)
    return data


class TestModelColumnsRoundTrip:
    async def test_model_name_and_hops_default(self, session_factory):
        async with session_factory() as s:
            job = await repo.upsert_job(s, _job_data())
            await s.commit()

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.model_name is None
        assert refreshed.model_hops == 0

    async def test_model_name_and_hops_persist(self, session_factory):
        async with session_factory() as s:
            job = await repo.upsert_job(s, _job_data())
            job.model_name = "kimi-k3"
            job.model_hops = 3
            await s.commit()

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.model_name == "kimi-k3"
        assert refreshed.model_hops == 3


class TestWrapFactoryShapes:
    """Detection must be by parameter name/count, NOT by counting no-default
    positional params -- `(name)` and `(name, model=None)` both count as exactly 1
    no-default positional param, which would make a real per-job model rung silently
    vanish into an ignored second argument if detection were still keyed on that."""

    def test_zero_arg_legacy_factory(self):
        calls = []

        def legacy_factory():
            calls.append(())
            return "backend-instance"

        wrapped = _wrap_factory(legacy_factory)
        result = wrapped("claude-cli", "some-model")
        assert result == "backend-instance"
        assert calls == [()]

    def test_bound_class_as_factory_zero_arg(self):
        """`backend_factory=SomeClass` where SomeClass.__init__ takes no args (a
        pattern used by several existing tests, e.g. LimitReachedBackend) must still
        work -- the class itself is zero-arg from the caller's perspective."""

        class NoArgBackend:
            def __init__(self):
                self.tag = "no-arg"

        wrapped = _wrap_factory(NoArgBackend)
        result = wrapped("claude-cli", "some-model")
        assert isinstance(result, NoArgBackend)

    def test_old_style_one_arg_factory_does_not_receive_model(self):
        calls = []

        def one_arg_factory(name):
            calls.append(name)
            return f"backend-for-{name}"

        wrapped = _wrap_factory(one_arg_factory)
        result = wrapped("opencode-zen", "mimo-v2.5-free")
        assert result == "backend-for-opencode-zen"
        # The model must NOT have been silently passed anywhere -- the underlying
        # factory only ever saw the name.
        assert calls == ["opencode-zen"]

    def test_new_style_name_model_factory_receives_both(self):
        calls = []

        def new_factory(name, model=None):
            calls.append((name, model))
            return f"backend-for-{name}-{model}"

        wrapped = _wrap_factory(new_factory)
        result = wrapped("opencode-go", "kimi-k3")
        assert result == "backend-for-opencode-go-kimi-k3"
        assert calls == [("opencode-go", "kimi-k3")]

    def test_new_style_factory_omitted_model_defaults_to_none(self):
        calls = []

        def new_factory(name, model=None):
            calls.append((name, model))
            return "backend"

        wrapped = _wrap_factory(new_factory)
        wrapped("opencode-go")
        assert calls == [("opencode-go", None)]

    def test_lambda_name_only_shape(self):
        """The exact shape many existing tests use: `lambda name: FakeAgentBackend(...)`."""
        calls = []
        wrapped = _wrap_factory(lambda name: calls.append(name) or f"backend-{name}")
        result = wrapped("claude-cli", "should-be-dropped")
        assert result == "backend-claude-cli"
        assert calls == ["claude-cli"]


class TestResetPathsClearModelFields:
    async def test_soft_reset_clears_model_fields(self, session_factory):
        async with session_factory() as s:
            job = await repo.upsert_job(s, _job_data())
            job.state = JobState.failed
            job.current_stage = None
            job.model_name = "kimi-k3"
            job.model_hops = 4
            job.retry_count = 0
            await s.commit()

        async with session_factory() as s:
            job = await repo.get_job(s, job.id)
            await repo.soft_reset_job(s, job)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.model_name is None
        assert refreshed.model_hops == 0
        assert refreshed.state == JobState.pending

    async def test_nuclear_reset_clears_model_fields(self, session_factory):
        async with session_factory() as s:
            job = await repo.upsert_job(s, _job_data())
            job.state = JobState.failed
            job.current_stage = None
            job.model_name = "hy4-preview"
            job.model_hops = 2
            job.retry_count = 1
            await s.commit()

        async with session_factory() as s:
            job = await repo.get_job(s, job.id)
            await repo.nuclear_reset_job(s, job)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.model_name is None
        assert refreshed.model_hops == 0
        assert refreshed.state == JobState.pending
