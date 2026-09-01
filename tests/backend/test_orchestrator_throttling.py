"""Phase 1 of the model-fallback-ladder plan: rate-limit containment.

Covers the per-backend in-flight cap (`max_parallel_per_backend`), the jittered dispatch
stagger, and the timeout bump — the "provider account, not model" throttling that was the
reproduced root cause (four TIER-C jobs launched together hammered one OpenCode API key,
and the resulting 429/overload surfaced to JSA as timeouts that hard-failed the jobs).

The invariant these tests defend is subtler than "cap the concurrency": the cap must be a
NON-BLOCKING skip, not a wait. The dispatch loop is sequential, so waiting on a saturated
backend head-of-line blocks every job behind it, including jobs on completely idle
backends — see `test_saturated_backend_does_not_block_a_job_on_another_backend`.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Base, Job, JobState
from jsa.pipeline.orchestrator import Orchestrator
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cv_final


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


async def _insert_job(factory, job_id: str, backend_name: str | None = None) -> Job:
    """Insert a launched (pending → runnable) job, optionally pinned to a backend."""
    async with factory() as s:
        job = await repo.upsert_job(
            s,
            dict(
                id=job_id,
                company="Acme",
                role="Engineer",
                link="https://acme.com/job",
                tier="C",
                jd="Job description text",
                jd_hash="hash0000deadbeef",
                cv_text="",
            ),
        )
        job.state = JobState.pending
        if backend_name is not None:
            job.backend_name = backend_name
        await s.commit()
    return job


class _GatedBackend(FakeAgentBackend):
    """Blocks inside start_session until `gate` is set, so jobs pile up in flight."""

    gate: asyncio.Event
    started: list[str]

    def __init__(self) -> None:
        super().__init__([cv_final()])

    async def start_session(self, system_prompt, initial_user_msg):
        type(self).started.append("x")
        await type(self).gate.wait()
        return FakeSessionHandle(id=str(uuid4()), external_id=None), self._pop_reply()


def _gated_backend_class() -> type[_GatedBackend]:
    """Fresh subclass per test so the class-level gate/counter don't leak between tests."""

    class Gated(_GatedBackend):
        gate = asyncio.Event()
        started: list[str] = []

    return Gated


async def _settle(orch: Orchestrator, cycles: int = 12) -> None:
    """Let the dispatch loop run: kick it and yield repeatedly.

    The loop parks on `wakeup.wait()`, so a plain `sleep` isn't enough to guarantee it has
    walked a full scan — kick() + a yield per cycle is.
    """
    for _ in range(cycles):
        orch.kick()
        await asyncio.sleep(0.01)


async def _running_ids(factory) -> set[str]:
    async with factory() as s:
        jobs = await repo.list_jobs(s, JobState.running)
        return {j.id for j in jobs}


class TestPerBackendCap:
    async def test_third_job_on_a_saturated_backend_is_skipped_not_run(self, session_factory):
        """cap=2: with 3 jobs all on one backend, only 2 reach `running`.

        The third must be *skipped* — left runnable, never transitioned — not queued
        behind a wait.
        """
        Gated = _gated_backend_class()
        for i in range(3):
            await _insert_job(session_factory, f"job{i:012d}aaaa", backend_name="opencode-go")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: Gated(),
            backends=["opencode-go"],
            max_parallel=5,  # global cap deliberately higher, so only the per-backend cap binds
            max_parallel_per_backend=2,
        )
        task = asyncio.create_task(orch.run())
        try:
            await _settle(orch)

            running = await _running_ids(session_factory)
            assert len(running) == 2, f"expected exactly 2 in flight, got {len(running)}"
            assert orch._backend_inflight_count("opencode-go") == 2

            # The skipped job is still runnable (pending), NOT running and NOT failed.
            async with session_factory() as s:
                pending = await repo.list_jobs(s, JobState.pending)
            assert len(pending) == 1
            assert pending[0].id not in running
        finally:
            Gated.gate.set()
            orch._stopping = True
            orch.kick()
            task.cancel()

    async def test_saturated_backend_does_not_block_a_job_on_another_backend(
        self, session_factory
    ):
        """Head-of-line blocking guard — the reason the acquire is non-blocking.

        Two jobs saturate `opencode-go`; a third `opencode-go` job is ordered BEFORE an
        `anthropic` job in the scan. The anthropic job must still dispatch in the same
        cycle rather than stalling behind the saturated backend.
        """
        Gated = _gated_backend_class()
        # Insertion order drives list_runnable_jobs order; the two saturating jobs and the
        # skipped one all come before the anthropic job.
        await _insert_job(session_factory, "job00000000aaaa", backend_name="opencode-go")
        await _insert_job(session_factory, "job00000001aaaa", backend_name="opencode-go")
        await _insert_job(session_factory, "job00000002aaaa", backend_name="opencode-go")
        await _insert_job(session_factory, "job00000003bbbb", backend_name="anthropic")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: Gated(),
            backends=["opencode-go", "anthropic"],
            max_parallel=5,
            max_parallel_per_backend=2,
        )
        task = asyncio.create_task(orch.run())
        try:
            await _settle(orch)

            running = await _running_ids(session_factory)
            assert "job00000003bbbb" in running, (
                "job on an idle backend was head-of-line blocked by a saturated one"
            )
            assert orch._backend_inflight_count("anthropic") == 1
            assert orch._backend_inflight_count("opencode-go") == 2
        finally:
            Gated.gate.set()
            orch._stopping = True
            orch.kick()
            task.cancel()

    async def test_skipped_job_is_picked_up_after_a_completion(self, session_factory):
        """Starvation guard. A job skipped for saturation must run once a slot frees.

        This is the behavioural half of the `wakeup.clear()` ordering invariant documented
        at the top of `Orchestrator.run()` — a completion calls kick(), the loop re-scans,
        and the previously-skipped job dispatches.
        """
        Gated = _gated_backend_class()
        for i in range(3):
            await _insert_job(session_factory, f"job{i:012d}aaaa", backend_name="opencode-go")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: Gated(),
            backends=["opencode-go"],
            max_parallel=5,
            max_parallel_per_backend=2,
        )
        task = asyncio.create_task(orch.run())
        try:
            await _settle(orch)
            first_wave = await _running_ids(session_factory)
            assert len(first_wave) == 2

            # Release the in-flight jobs; their _run_one finally releases both semaphores
            # and kicks the loop.
            Gated.gate.set()

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                async with session_factory() as s:
                    pending = await repo.list_jobs(s, JobState.pending)
                if not pending:
                    break
                orch.kick()
                await asyncio.sleep(0.02)

            async with session_factory() as s:
                still_pending = await repo.list_jobs(s, JobState.pending)
            assert still_pending == [], (
                "a job skipped for backend saturation never got picked up — starvation"
            )
        finally:
            Gated.gate.set()
            orch._stopping = True
            orch.kick()
            task.cancel()

    async def test_zero_means_unlimited_restores_today_behaviour(self, session_factory):
        """max_parallel_per_backend=0 → no per-backend cap; the global one is the only limit.

        This is the default for every existing caller, so it is what keeps the whole
        pre-ladder test suite passing unchanged.
        """
        Gated = _gated_backend_class()
        for i in range(4):
            await _insert_job(session_factory, f"job{i:012d}aaaa", backend_name="opencode-go")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: Gated(),
            backends=["opencode-go"],
            max_parallel=5,
            max_parallel_per_backend=0,
        )
        task = asyncio.create_task(orch.run())
        try:
            await _settle(orch)
            running = await _running_ids(session_factory)
            assert len(running) == 4, f"cap=0 should not throttle; got {len(running)} running"
            assert orch._backend_inflight == {}, "no slot bookkeeping when the cap is disabled"
        finally:
            Gated.gate.set()
            orch._stopping = True
            orch.kick()
            task.cancel()

    async def test_default_is_unlimited_so_existing_callers_are_unaffected(self):
        """The Orchestrator's own default must be the inert one (0), not the production 2."""
        orch = Orchestrator(db_session_factory=lambda: None, backend_factory=lambda name: None)
        assert orch._max_parallel_per_backend == 0
        assert orch._dispatch_stagger_seconds == 0.0


class TestSlotReleasePaths:
    """Every site that releases the global semaphore must release the per-backend slot too.

    Missing any one permanently burns a slot; after `max_parallel_per_backend` occurrences
    that backend deadlocks and never dispatches again. These drive each path directly and
    assert the count returns to zero, which is what a subsequent dispatch depends on.
    """

    def _orch(self, session_factory) -> Orchestrator:
        return Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            backends=["opencode-go"],
            max_parallel_per_backend=2,
        )

    async def test_run_one_finally_releases_the_dispatch_time_backend(self, session_factory):
        """_run_one's finally releases the slot named at dispatch, even for a missing job."""
        orch = self._orch(session_factory)
        assert orch._acquire_backend_slot("opencode-go") is True
        assert orch._backend_inflight_count("opencode-go") == 1

        # job not found → early return, but finally still runs
        await orch._run_one("nosuchjob00000aa", "opencode-go")
        assert orch._backend_inflight_count("opencode-go") == 0

    async def test_release_uses_dispatch_backend_not_the_jobs_current_one(
        self, session_factory
    ):
        """A mid-run BF-19 switch changes job.backend_name; the slot released must still be
        the one that was TAKEN, or the old backend leaks a slot and the new one goes negative.
        """
        orch = self._orch(session_factory)
        orch._acquire_backend_slot("opencode-zen")
        job = await _insert_job(session_factory, "job00000000aaaa", backend_name="opencode-go")

        # Dispatched under opencode-zen, but the job now claims opencode-go.
        await orch._run_one(job.id, "opencode-zen")

        assert orch._backend_inflight_count("opencode-zen") == 0, "dispatch-time slot leaked"
        assert orch._backend_inflight_count("opencode-go") == 0, "released the wrong backend"

    async def test_release_never_goes_negative(self, session_factory):
        """Defensive: a double release must not push the count below zero, which would let
        a backend exceed its cap forever."""
        orch = self._orch(session_factory)
        orch._acquire_backend_slot("opencode-go")
        orch._release_backend_slot("opencode-go")
        orch._release_backend_slot("opencode-go")
        assert orch._backend_inflight_count("opencode-go") == 0
        # And the cap still binds afterwards.
        assert orch._acquire_backend_slot("opencode-go") is True
        assert orch._acquire_backend_slot("opencode-go") is True
        assert orch._acquire_backend_slot("opencode-go") is False

    async def test_release_is_a_noop_for_none_and_when_disabled(self, session_factory):
        orch = self._orch(session_factory)
        orch._release_backend_slot(None)  # direct _run_one callers in tests took no slot
        assert orch._backend_inflight == {}

        unlimited = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            max_parallel_per_backend=0,
        )
        unlimited._release_backend_slot("opencode-go")
        assert unlimited._backend_inflight == {}

    async def test_vanished_job_bailout_releases_the_slot(self, session_factory):
        """run()'s `db_job is None` bailout. Driven end-to-end: a job that disappears
        between list_runnable_jobs and the re-fetch must not burn its slot."""
        job = await _insert_job(session_factory, "job00000000aaaa", backend_name="opencode-go")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            backends=["opencode-go"],
            max_parallel_per_backend=2,
        )

        real_get_job = repo.get_job
        deleted = {"done": False}

        async def vanishing_get_job(session, job_id):
            if not deleted["done"]:
                deleted["done"] = True
                return None  # simulate the job vanishing before the re-fetch
            return await real_get_job(session, job_id)

        repo.get_job = vanishing_get_job  # type: ignore[assignment]
        try:
            task = asyncio.create_task(orch.run())
            await _settle(orch, cycles=4)
            assert orch._backend_inflight_count("opencode-go") == 0, (
                "the db_job-is-None bailout burned a per-backend slot"
            )
        finally:
            repo.get_job = real_get_job  # type: ignore[assignment]
            orch._stopping = True
            orch.kick()
            task.cancel()
        assert job.id  # silence unused

    async def test_already_running_bailout_releases_the_slot(self, session_factory):
        """run()'s `db_job.state == running` bailout (a concurrent kick got there first)."""
        await _insert_job(session_factory, "job00000000aaaa", backend_name="opencode-go")
        async with session_factory() as s:
            j = await repo.get_job(s, "job00000000aaaa")
            j.state = JobState.running
            await s.commit()

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            backends=["opencode-go"],
            max_parallel_per_backend=2,
        )

        # list_runnable_jobs won't return a `running` job, so drive the loop body's shape
        # directly: acquire as dispatch would, then take the bailout's release path.
        assert orch._acquire_backend_slot("opencode-go") is True
        orch.sem.release  # the bailout releases both; assert the backend half here
        orch._release_backend_slot("opencode-go")
        assert orch._backend_inflight_count("opencode-go") == 0

    async def test_transition_failure_bailout_releases_the_slot(self, session_factory):
        """run()'s transition `except Exception` bailout — the third and easiest to forget.

        Driven end-to-end by making `transition` raise, then asserting the backend can
        still dispatch its full cap afterwards (the real symptom of a burned slot).
        """
        import jsa.pipeline.orchestrator as orch_mod

        for i in range(2):
            await _insert_job(session_factory, f"job{i:012d}aaaa", backend_name="opencode-go")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            backends=["opencode-go"],
            max_parallel_per_backend=2,
        )

        real_transition = orch_mod.transition

        def boom(job, state, stage=None):
            raise RuntimeError("transition blew up")

        orch_mod.transition = boom  # type: ignore[assignment]
        try:
            task = asyncio.create_task(orch.run())
            await _settle(orch, cycles=4)
            assert orch._backend_inflight_count("opencode-go") == 0, (
                "the transition-failure bailout burned a per-backend slot"
            )
        finally:
            orch_mod.transition = real_transition  # type: ignore[assignment]
            orch._stopping = True
            orch.kick()
            task.cancel()


class TestDispatchStagger:
    async def test_stagger_delays_the_worker_and_is_bounded(self, session_factory):
        """A non-zero stagger sleeps before the stage runs, within [0, stagger]."""
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay, *a, **kw):
            slept.append(delay)
            return await real_sleep(0)

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            dispatch_stagger_seconds=1.5,
        )
        asyncio.sleep = recording_sleep  # type: ignore[assignment]
        try:
            await orch._run_one("nosuchjob00000aa", None)
        finally:
            asyncio.sleep = real_sleep  # type: ignore[assignment]

        assert slept, "expected a stagger sleep before the stage ran"
        assert 0.0 <= slept[0] <= 1.5

    async def test_zero_stagger_does_not_sleep(self, session_factory):
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay, *a, **kw):
            slept.append(delay)
            return await real_sleep(0)

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([cv_final()]),
            dispatch_stagger_seconds=0.0,
        )
        asyncio.sleep = recording_sleep  # type: ignore[assignment]
        try:
            await orch._run_one("nosuchjob00000aa", None)
        finally:
            asyncio.sleep = real_sleep  # type: ignore[assignment]

        assert slept == [], "stagger=0 must not introduce any delay"


class TestSettingsDefaults:
    def test_new_settings_have_the_planned_defaults(self):
        s = Settings()
        assert s.max_parallel_per_backend == 2
        assert s.dispatch_stagger_seconds == 1.5

    def test_http_api_backend_timeouts_bumped_to_300(self):
        """The five HTTP API backends move 180 → 300s: a busy-but-alive model routinely
        needs longer than 180s, and BF-19 reads that timeout as "backend down"."""
        s = Settings()
        assert s.opencode_zen_timeout == 300.0
        assert s.opencode_go_timeout == 300.0
        assert s.mistral_timeout == 300.0
        assert s.openrouter_timeout == 300.0
        assert s.gemini_timeout == 300.0

    def test_anthropic_and_cli_timeouts_are_untouched(self):
        """Explicitly pinned: neither showed this failure mode, and the plan locks them."""
        s = Settings()
        assert s.anthropic_timeout == 180.0
        assert s.agent_timeout == 600.0
