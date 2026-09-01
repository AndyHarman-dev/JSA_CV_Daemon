"""Dispatcher: asyncio.Semaphore(5) worker pool and runnable job queue."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend, AgentBackendUnavailable, AgentLimitReached, AgentTimeout
from jsa.agents.google_cli import GoogleCliSessionExpiredError
from jsa.db import repo
from jsa.db.models import Job, JobState, Stage
from jsa.events.bus import bus
from jsa.events.schema import (
    BackendSwitchedEvent,
    ErrorEvent,
    LogEvent,
    StatusChangedEvent,
    event_to_dict,
)
from jsa.pipeline import stages
from jsa.pipeline.stages import PausedForInput, StaleJobResult
from jsa.pipeline.state_machine import transition

logger = logging.getLogger(__name__)


def _next_stage_for(job: Job) -> Stage:
    """Determine which stage to run for the given job.

    State / current_stage mapping:
    - pending                        → fit_assessment (fresh)
    - fit_done                       → cv_adjust (fresh; fit check passed or was ignored)
    - cv_done                        → cover_letter (fresh)
    - awaiting_input                 → job.current_stage (resume)
    - review + unconsumed rev req    → job.current_stage (revising_cv / revising_cl)
    - cv_review + unconsumed rev req → job.current_stage (revising_cv)
    """
    if job.state == JobState.pending:
        return Stage.fit_assessment
    if job.state == JobState.fit_done:
        return Stage.cv_adjust
    if job.state == JobState.cv_done:
        return Stage.cover_letter
    if job.state in (JobState.awaiting_input, JobState.review, JobState.cv_review):
        if job.current_stage is None:
            raise ValueError(
                f"Job {job.id} is in {job.state} but current_stage is None"
            )
        return job.current_stage
    raise ValueError(f"Job {job.id} is in unexpected state {job.state} for dispatch")


def _wrap_factory(backend_factory: Callable) -> Callable[[str], AgentBackend]:
    """Normalise backend_factory to always accept a backend name string.

    Legacy (zero-arg) factories are wrapped so the same code path works for
    both pre-BF-19 callers (tests) and the new name-parameterised form.
    """
    try:
        sig = inspect.signature(backend_factory)
        n_positional = sum(
            1
            for p in sig.parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            and p.default is inspect.Parameter.empty
        )
    except (ValueError, TypeError):
        n_positional = 0

    if n_positional == 0:
        # Legacy zero-arg factory — wrap it to accept (and ignore) the name arg
        return lambda name: backend_factory()
    # New-style: factory(name: str) -> AgentBackend
    return backend_factory


class Orchestrator:
    """Dispatcher loop that manages concurrent pipeline stages.

    Attributes:
        sem: Semaphore limiting parallel agent sessions to max_parallel.
        wakeup: Event set by kick() to wake the run() loop.
        _db_session_factory: Callable that returns a new async SQLAlchemy session.
        _backend_factory: Callable(name: str) -> AgentBackend — instantiates a backend by name.
        _fit_backend_factory: Same, but for the fit_assessment stage only (separate
            model/timeout — see Settings.fit_model). None → fit_assessment reuses
            _backend_factory, which is what every test that omits it gets.
        _backends: Ordered list of backend names forming the fallback chain (BF-19).
        _stopping: Flag to signal graceful shutdown.
    """

    def __init__(
        self,
        db_session_factory: Callable[[], AsyncSession],
        backend_factory: Callable,
        backends: list[str] | None = None,
        max_parallel: int = 5,
        fit_backend_factory: Callable | None = None,
        output_dir: Path | None = None,
        cv_structure_path: Path | None = None,
        preferences_path: Path | None = None,
        max_parallel_per_backend: int = 0,
        dispatch_stagger_seconds: float = 0.0,
    ) -> None:
        self.sem = asyncio.Semaphore(max_parallel)
        # Per-backend in-flight cap, on top of the global `sem`. Throttles one *provider
        # account* — five jobs on one backend are five simultaneous requests on one API key,
        # and the resulting 429/overload is keyed to the account, not the model. `0` =
        # unlimited, which is exactly today's behaviour and what every existing caller
        # (tests included) gets by default; production passes settings.max_parallel_per_backend.
        # Counts are created lazily per backend name, so a chain that never dispatches to a
        # backend never gets an entry.
        self._max_parallel_per_backend = max_parallel_per_backend
        self._backend_inflight: dict[str, int] = {}
        self._dispatch_stagger_seconds = dispatch_stagger_seconds
        self.wakeup = asyncio.Event()
        self._db_session_factory = db_session_factory
        self._backend_factory = _wrap_factory(backend_factory)
        self._fit_backend_factory = (
            _wrap_factory(fit_backend_factory) if fit_backend_factory is not None else None
        )
        self._backends = backends if backends is not None else ["claude-cli"]
        self._output_dir = output_dir
        self._cv_structure_path = cv_structure_path
        self._preferences_path = preferences_path
        self._stopping = False
        # Keyed by job_id (not an unkeyed set) so a specific job's in-flight
        # worker task can be looked up and cancelled — see cancel_task().
        self._tasks: dict[str, asyncio.Task] = {}

    def kick(self) -> None:
        """Wake the run() loop. Called by API routes after answer/revise."""
        self.wakeup.set()

    def _acquire_backend_slot(self, backend_name: str) -> bool:
        """Try to take one in-flight slot for `backend_name`. Never blocks.

        Returns True if a slot was taken (or the cap is disabled), False if this backend
        is already saturated and the caller should skip the job this cycle.

        Non-blocking is the whole point, and the reason this is a plain counter rather
        than an ``asyncio.Semaphore``: the dispatch loop is sequential, so *waiting* on a
        saturated backend would cause head-of-line blocking — every job queued behind it
        would stall, including jobs on completely idle backends. A skipped job stays
        runnable and is re-picked on the next scan (see the starvation note in ``run()``).
        """
        cap = self._max_parallel_per_backend
        if cap <= 0:  # 0 (or negative) = unlimited — today's behaviour
            return True
        if self._backend_inflight_count(backend_name) >= cap:
            return False
        self._backend_inflight[backend_name] = self._backend_inflight_count(backend_name) + 1
        return True

    def _release_backend_slot(self, backend_name: str | None) -> None:
        """Return one in-flight slot for `backend_name`. Idempotent-safe and never negative.

        MUST be called at every site that releases the global ``self.sem`` — the three
        post-acquire bailouts in ``run()`` plus ``_run_one``'s ``finally``. Missing any one
        permanently burns a slot, and after `max_parallel_per_backend` occurrences that
        backend deadlocks and never dispatches again.

        Takes the backend name captured *at dispatch time*, not ``job.backend_name``, so a
        mid-run BF-19 backend switch releases the slot that was actually taken.
        """
        if backend_name is None or self._max_parallel_per_backend <= 0:
            return
        current = self._backend_inflight_count(backend_name)
        if current > 0:
            self._backend_inflight[backend_name] = current - 1

    def _backend_inflight_count(self, backend_name: str) -> int:
        """In-flight job count for one backend (0 when it has never been dispatched to)."""
        return self._backend_inflight.get(backend_name, 0)

    def _backend_for_dispatch(self, job: Job) -> str:
        """Which backend this job will actually run on — mirrors the resolution used by
        ``_advance_backend_or_fail``, so the slot taken at dispatch is keyed to the backend
        the request is really sent to."""
        return job.backend_name or self._backends[0]

    def cancel_task(self, job_id: str) -> bool:
        """Best-effort: cancel the in-flight worker task for job_id, if any.

        Returns True if a running task was found and cancelled. This stops the
        agent turn promptly (saving API cost) but is not the correctness
        guarantee — StaleJobResult (see stages.py) still protects against a
        task that races to completion before cancellation lands.
        """
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def run(self) -> None:
        """Main dispatch loop.

        Repeatedly:
        1. Clear the wakeup event.
        2. Fetch all runnable jobs.
        3. For each runnable job:
           a. Acquire the semaphore (blocks at max_parallel in-flight).
           b. Transition the job to running + commit (prevents double-dispatch).
           c. Spawn _run_one as an asyncio task.
        4. Wait for the wakeup event.
        """
        # Tracks whether the "no CV structure" gate LogEvent has already been published,
        # so it fires once per blocked spell rather than once per loop cycle. Loop-local
        # (not self. state) — it's only ever meaningful within this one run() call.
        cv_gate_blocked_announced = False

        while not self._stopping:
            # DO NOT MOVE THIS BELOW THE DISPATCH SCAN. Skip-on-saturation (the per-backend
            # cap below) leaves a skipped job runnable and relies on a later scan to pick it
            # up — which is starvation-free ONLY because clear() sits here, above the scan,
            # with no await between wait() returning at the bottom of the loop and this
            # clear(). That ordering means a kick() landing *during* the scan is retained and
            # re-runs the loop immediately. If clear() ever moves below the scan, a kick that
            # arrives mid-scan is swallowed and jobs skipped for saturation starve silently
            # until some unrelated event kicks the loop again.
            self.wakeup.clear()

            # Gate: cv_structure is the single source of truth for CV content. Inert
            # when _cv_structure_path is None (tests / legacy callers) — production
            # always passes it (see server.py). Checks loadability, not just existence,
            # so a present-but-corrupt file (bad hand-edit, or a save that raced a crash)
            # blocks dispatch the same as a missing one — jobs stay pending, never failed.
            # The editor's PUT calls kick() on save so this unblocks without a restart.
            if self._cv_structure_path is not None and (
                await stages._read_base_structure(self._cv_structure_path)
            ) is None:
                if not cv_gate_blocked_announced:
                    cv_gate_blocked_announced = True
                    await bus.publish(
                        event_to_dict(
                            LogEvent(
                                job_id="",
                                level="info",
                                text=(
                                    "No usable CV structure — jobs stay pending until you set "
                                    "up your CV in the Structure Editor."
                                ),
                            )
                        )
                    )
                await self.wakeup.wait()
                continue
            cv_gate_blocked_announced = False

            async with self._db_session_factory() as session:
                runnable: list[Job] = await repo.list_runnable_jobs(session)

            for job in runnable:
                # Per-backend cap FIRST, and non-blocking: if this backend already has
                # max_parallel_per_backend jobs in flight, skip this job for now rather than
                # waiting on it. Waiting here would head-of-line block the whole sequential
                # loop — a saturated backend would stall every job behind it, including jobs
                # on completely idle backends. The job stays runnable; see the clear() note
                # at the top of the loop for why it can't starve.
                #
                # The three bailouts below release this slot but deliberately do NOT kick():
                # reaching one means the acquire above SUCCEEDED, i.e. this backend had room,
                # which in a sequential scan means no earlier job was skipped on its account.
                # So the release cannot unblock anything the loop has already walked past.
                dispatch_backend = self._backend_for_dispatch(job)
                if not self._acquire_backend_slot(dispatch_backend):
                    continue

                # Acquire sem BEFORE committing the transition so the in-flight
                # count is accurate. This blocks when 5 tasks are in flight.
                await self.sem.acquire()

                # Transition to running in the DB before spawning, so a new
                # list_runnable_jobs call won't re-pick this job.
                stage = _next_stage_for(job)
                try:
                    async with self._db_session_factory() as session:
                        # Re-fetch to get a fresh, session-bound ORM object
                        db_job = await repo.get_job(session, job.id)
                        if db_job is None:
                            self.sem.release()
                            self._release_backend_slot(dispatch_backend)
                            continue
                        # Skip if the job was already picked up (e.g. by a
                        # concurrent kick that landed before we got here)
                        if db_job.state == JobState.running:
                            self.sem.release()
                            self._release_backend_slot(dispatch_backend)
                            continue
                        transition(db_job, JobState.running, stage)
                        db_job.updated_at = datetime.utcnow()
                        session.add(db_job)
                        await session.commit()
                except Exception as exc:
                    logger.error(
                        "Failed to transition job %s to running: %s", job.id, exc
                    )
                    await bus.publish(
                        event_to_dict(
                            LogEvent(
                                job_id=job.id,
                                level="warn",
                                text=f"Failed to start {stage.value}: {exc}",
                            )
                        )
                    )
                    self.sem.release()
                    self._release_backend_slot(dispatch_backend)
                    continue

                # Publish status change AFTER the DB commit so the UI fetches
                # consistent data.  `job.state` is the pre-transition state
                # (from list_runnable_jobs); the new state is always `running`.
                await bus.publish(
                    event_to_dict(
                        LogEvent(
                            job_id=job.id,
                            level="info",
                            text=f"Picked up: starting {stage.value}",
                        )
                    )
                )
                await bus.publish(
                    event_to_dict(
                        StatusChangedEvent(
                            job_id=job.id,
                            from_state=job.state.value,
                            to_state=JobState.running.value,
                        )
                    )
                )

                # Spawn the worker task; sem is released in the task's finally block.
                # Keying by job_id (rather than an unkeyed set) lets cancel_task()
                # look up and cancel a specific job's in-flight task (e.g. on dismiss).
                task = asyncio.create_task(self._run_one(job.id, dispatch_backend))
                self._tasks[job.id] = task
                # Only pop if the dict still points at *this* task — guards against
                # popping a newer task if the same job_id got re-dispatched before
                # this callback ran.
                task.add_done_callback(
                    lambda t, jid=job.id: (
                        self._tasks.pop(jid, None)
                        if self._tasks.get(jid) is t
                        else None
                    )
                )

            await self.wakeup.wait()

    async def _run_one(self, job_id: str, dispatch_backend: str | None = None) -> None:
        """Worker task: open a session, run the stage, handle outcome.

        Always releases the semaphore and kicks the loop in finally.

        `dispatch_backend` is the backend name whose per-backend slot was taken by the
        dispatch loop. It is passed in (rather than re-read from ``job.backend_name``) so
        that a mid-run BF-19 backend switch still releases the slot that was actually
        taken. Defaults to None for direct callers in tests, which took no slot.
        """
        try:
            # Jittered stagger: spread a burst of simultaneous launches over a small window
            # so one provider account sees a staggered request pattern instead of N requests
            # at the same instant. Deliberately here and not in the dispatch loop — there it
            # would delay dispatch itself while holding both semaphores, converting a
            # throughput fix into a throughput cost.
            if self._dispatch_stagger_seconds > 0:
                await asyncio.sleep(random.uniform(0, self._dispatch_stagger_seconds))
            async with self._db_session_factory() as session:
                job = await repo.get_job(session, job_id)
                if job is None:
                    logger.error("_run_one: job %s not found", job_id)
                    return

                if job.state != JobState.running:
                    logger.warning(
                        "_run_one: job %s expected state running, got %s",
                        job_id,
                        job.state,
                    )
                    return

                stage = job.current_stage
                if stage is None:
                    logger.error(
                        "_run_one: job %s has no current_stage while running", job_id
                    )
                    return

                # Per-job backend selection (BF-19):
                # Use job.backend_name if already set; otherwise assign backends[0].
                if job.backend_name is None:
                    job.backend_name = self._backends[0]
                    job.updated_at = datetime.utcnow()
                    session.add(job)
                    await session.commit()

                active_backend_name = job.backend_name
                backend = self._backend_factory(active_backend_name)
                # Built from active_backend_name, not backends[0], so the fit gate
                # follows the job after a BF-19 limit-triggered backend switch. Only
                # constructed for the stage that uses it.
                fit_backend = None
                if stage == Stage.fit_assessment and self._fit_backend_factory is not None:
                    fit_backend = self._fit_backend_factory(active_backend_name)
                await stages.run_stage(
                    job, backend, stage, session,
                    fit_backend=fit_backend,
                    output_dir=self._output_dir,
                    cv_structure_path=self._cv_structure_path,
                    preferences_path=self._preferences_path,
                )

        except PausedForInput:
            # Job successfully parked — not an error
            pass

        except StaleJobResult as exc:
            # The job was dismissed/cancelled/deleted on another session while
            # this agent turn was in flight. The reply was discarded (no rows
            # written) — this is expected, benign control flow, not a failure.
            logger.info("_run_one: job %s result discarded (stale): %s", job_id, exc)

        except asyncio.CancelledError:
            # Task was cancelled (e.g. dismiss's best-effort cancel_task()).
            # Don't mark the job failed — StaleJobResult / the dismiss commit
            # already reflect the correct outcome. Propagate so the task
            # actually ends cancelled; `finally` below still runs.
            logger.info("_run_one: job %s task cancelled", job_id)
            raise

        except AgentLimitReached as exc:
            logger.warning("_run_one: job %s hit backend limit: %s", job_id, exc)
            await self._handle_limit_reached(job_id, exc)

        except AgentTimeout as exc:
            # A timeout is an availability failure exactly like a quota signal —
            # the backend didn't answer, not "answered no" — so it must engage
            # the same BF-19 fallback chain rather than falling into the generic
            # `except Exception` below (which hard-fails with no retry).
            logger.warning("_run_one: job %s backend timed out: %s", job_id, exc)
            await self._handle_backend_timeout(job_id, exc)

        except AgentBackendUnavailable as exc:
            # Non-timeout, non-quota failure where the SAME backend won't help:
            # bad model/config, auth errors, or a transient overload/gateway
            # error that already exhausted its in-backend retry budget (see
            # OpenCodeZenBackend._call_api). Same BF-19 chain as the other two.
            logger.warning("_run_one: job %s backend unavailable: %s", job_id, exc)
            await self._handle_backend_unavailable(job_id, exc)

        except GoogleCliSessionExpiredError as exc:
            logger.warning("_run_one: job %s google session expired, attempting auto-recovery", job_id)
            await self._handle_session_expired(job_id, exc)

        except Exception as exc:
            logger.exception("_run_one: job %s failed: %s", job_id, exc)
            try:
                async with self._db_session_factory() as err_session:
                    await err_session.rollback()
                    await repo.mark_failed(err_session, job_id, str(exc))
                # Publish after commit so the UI fetches consistent data
                await bus.publish(
                    event_to_dict(LogEvent(job_id=job_id, level="error", text=str(exc)))
                )
                await bus.publish(
                    event_to_dict(ErrorEvent(job_id=job_id, message=str(exc)))
                )
            except Exception as inner_exc:
                logger.error(
                    "_run_one: failed to mark job %s as failed: %s",
                    job_id,
                    inner_exc,
                )

        finally:
            self.sem.release()
            self._release_backend_slot(dispatch_backend)
            self.kick()

    async def _handle_limit_reached(self, job_id: str, exc: AgentLimitReached) -> None:
        """Handle AgentLimitReached: switch to next backend or mark failed (BF-19)."""
        await self._advance_backend_or_fail(
            job_id,
            switch_reason="Backend limit reached",
            exhausted_message="Backend limit reached — switch backends or wait for quota reset",
        )

    async def _handle_backend_timeout(self, job_id: str, exc: AgentTimeout) -> None:
        """Handle AgentTimeout: switch to next backend or mark failed (BF-19).

        Mirrors _handle_limit_reached — a timeout means the active backend is
        unavailable right now, which is exactly the condition BF-19's chain exists
        for. Before this existed, AgentTimeout fell into _run_one's generic
        `except Exception` handler and hard-failed the job on the very first
        backend, even with a working fallback configured in `--backends`.
        """
        await self._advance_backend_or_fail(
            job_id,
            switch_reason="Backend timed out",
            exhausted_message=(
                "Backend timed out on every configured backend — switch backends "
                "or increase the timeout"
            ),
        )

    async def _handle_backend_unavailable(self, job_id: str, exc: AgentBackendUnavailable) -> None:
        """Handle AgentBackendUnavailable: switch to next backend or mark failed (BF-19).

        Mirrors _handle_limit_reached/_handle_backend_timeout. Distinct message from
        both — "wait for quota reset" (limit) and "increase the timeout" (timeout)
        are both wrong advice for a bad model name, an auth error, or a backend
        that stayed overloaded through its own in-process retries.
        """
        await self._advance_backend_or_fail(
            job_id,
            switch_reason="Backend unavailable",
            exhausted_message=(
                "Backend unavailable on every configured backend — check model/API "
                "key configuration, or try again later if this was transient overload"
            ),
        )

    async def _advance_backend_or_fail(
        self, job_id: str, switch_reason: str, exhausted_message: str
    ) -> None:
        """Shared BF-19 chain-advance logic for AgentLimitReached, AgentTimeout, and
        AgentBackendUnavailable.

        If a next backend exists in the chain:
        1. Persist job.backend_name = next backend.
        2. Delete failed stage's Message rows (so next dispatch starts fresh).
        3. Reset job state to the stage's start checkpoint.
        4. Emit BackendSwitchedEvent.
        The finally block in _run_one calls kick() which re-triggers dispatch.

        If chain is exhausted: mark the job failed with `exhausted_message`.
        """
        try:
            async with self._db_session_factory() as session:
                job = await repo.get_job(session, job_id)
                if job is None:
                    logger.error("_advance_backend_or_fail: job %s not found", job_id)
                    return

                current_backend = job.backend_name or self._backends[0]
                failed_stage = job.current_stage  # capture before any transition

                # Find the next backend in the chain
                try:
                    current_idx = self._backends.index(current_backend)
                except ValueError:
                    current_idx = -1

                next_idx = current_idx + 1
                if next_idx < len(self._backends):
                    # Switch to next backend
                    next_backend = self._backends[next_idx]
                    await repo.backend_switch_reset(session, job, next_backend, failed_stage)

                    switch_msg = f"{switch_reason} — switching from {current_backend} to {next_backend}"
                    logger.info("_advance_backend_or_fail: job %s: %s", job_id, switch_msg)

                    await bus.publish(
                        event_to_dict(LogEvent(job_id=job_id, level="warn", text=switch_msg))
                    )
                    await bus.publish(
                        event_to_dict(
                            BackendSwitchedEvent(
                                job_id=job_id,
                                from_backend=current_backend,
                                to_backend=next_backend,
                            )
                        )
                    )
                else:
                    # Chain exhausted — mark failed
                    await repo.mark_failed(session, job_id, exhausted_message)
                    await bus.publish(
                        event_to_dict(LogEvent(job_id=job_id, level="error", text=exhausted_message))
                    )
                    await bus.publish(
                        event_to_dict(ErrorEvent(job_id=job_id, message=exhausted_message))
                    )

        except Exception as inner_exc:
            logger.error(
                "_advance_backend_or_fail: failed to handle backend failover for job %s: %s",
                job_id,
                inner_exc,
            )

    async def _handle_session_expired(self, job_id: str, exc: Exception) -> None:
        """Auto-soft-reset once on Google CLI session expiry; fail permanently on second try.

        soft_reset_job sets retry_count=1. If session expires again on the retry,
        retry_count>0 so we leave the job failed rather than looping.
        """
        try:
            async with self._db_session_factory() as session:
                await repo.mark_failed(session, job_id, str(exc))
            async with self._db_session_factory() as session:
                job = await repo.get_job(session, job_id)
                if job is None:
                    return
                if job.retry_count == 0:
                    logger.info(
                        "_handle_session_expired: auto-soft-resetting job %s after session expiry",
                        job_id,
                    )
                    await repo.soft_reset_job(session, job)
                    await bus.publish(
                        event_to_dict(
                            LogEvent(
                                job_id=job_id,
                                level="warn",
                                text="Google CLI session expired — auto-retrying from scratch",
                            )
                        )
                    )
                    self.kick()
                # retry_count > 0: already auto-recovered once — leave as failed
        except Exception as inner_exc:
            logger.error(
                "_handle_session_expired: failed to handle session expiry for job %s: %s",
                job_id,
                inner_exc,
            )
