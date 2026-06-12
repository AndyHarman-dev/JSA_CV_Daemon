"""Dispatcher: asyncio.Semaphore(5) worker pool and runnable job queue."""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend, AgentLimitReached
from jsa.agents.gemini_cli import GeminiSessionExpiredError
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
from jsa.pipeline.stages import PausedForInput
from jsa.pipeline.state_machine import transition

logger = logging.getLogger(__name__)


def _next_stage_for(job: Job) -> Stage:
    """Determine which stage to run for the given job.

    State / current_stage mapping:
    - pending                     → fit_assessment (fresh)
    - fit_done                    → cv_adjust (fresh; fit check passed or was ignored)
    - cv_done                     → cover_letter (fresh)
    - awaiting_input              → job.current_stage (resume)
    - review + unconsumed rev req → job.current_stage (revising_cv / revising_cl)
    """
    if job.state == JobState.pending:
        return Stage.fit_assessment
    if job.state == JobState.fit_done:
        return Stage.cv_adjust
    if job.state == JobState.cv_done:
        return Stage.cover_letter
    if job.state in (JobState.awaiting_input, JobState.review):
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
        _backends: Ordered list of backend names forming the fallback chain (BF-19).
        _stopping: Flag to signal graceful shutdown.
    """

    def __init__(
        self,
        db_session_factory: Callable[[], AsyncSession],
        backend_factory: Callable,
        backends: list[str] | None = None,
        max_parallel: int = 5,
        output_dir: Path | None = None,
    ) -> None:
        self.sem = asyncio.Semaphore(max_parallel)
        self.wakeup = asyncio.Event()
        self._db_session_factory = db_session_factory
        self._backend_factory = _wrap_factory(backend_factory)
        self._backends = backends if backends is not None else ["claude-cli"]
        self._output_dir = output_dir
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()

    def kick(self) -> None:
        """Wake the run() loop. Called by API routes after answer/revise."""
        self.wakeup.set()

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
        while not self._stopping:
            self.wakeup.clear()

            async with self._db_session_factory() as session:
                runnable: list[Job] = await repo.list_runnable_jobs(session)

            for job in runnable:
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
                            continue
                        # Skip if the job was already picked up (e.g. by a
                        # concurrent kick that landed before we got here)
                        if db_job.state == JobState.running:
                            self.sem.release()
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
                # Retain a strong reference to prevent premature GC.
                task = asyncio.create_task(self._run_one(job.id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

            await self.wakeup.wait()

    async def _run_one(self, job_id: str) -> None:
        """Worker task: open a session, run the stage, handle outcome.

        Always releases the semaphore and kicks the loop in finally.
        """
        try:
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
                await stages.run_stage(job, backend, stage, session, output_dir=self._output_dir)

        except PausedForInput:
            # Job successfully parked — not an error
            pass

        except AgentLimitReached as exc:
            logger.warning("_run_one: job %s hit backend limit: %s", job_id, exc)
            await self._handle_limit_reached(job_id, exc)

        except GeminiSessionExpiredError as exc:
            logger.warning("_run_one: job %s gemini session expired, attempting auto-recovery", job_id)
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
            self.kick()

    async def _handle_limit_reached(self, job_id: str, exc: AgentLimitReached) -> None:
        """Handle AgentLimitReached: switch to next backend or mark failed (BF-19).

        If a next backend exists in the chain:
        1. Persist job.backend_name = next backend.
        2. Delete failed stage's Message rows (so next dispatch starts fresh).
        3. Reset job state to the stage's start checkpoint.
        4. Emit BackendSwitchedEvent.
        The finally block in _run_one calls kick() which re-triggers dispatch.

        If chain is exhausted: mark the job failed with a human-readable message.
        """
        try:
            async with self._db_session_factory() as session:
                job = await repo.get_job(session, job_id)
                if job is None:
                    logger.error("_handle_limit_reached: job %s not found", job_id)
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

                    switch_msg = (
                        f"Backend limit reached — switching from "
                        f"{current_backend} to {next_backend}"
                    )
                    logger.info("_handle_limit_reached: job %s: %s", job_id, switch_msg)

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
                    human_msg = (
                        "Backend limit reached — switch backends or wait for quota reset"
                    )
                    await repo.mark_failed(session, job_id, human_msg)
                    await bus.publish(
                        event_to_dict(LogEvent(job_id=job_id, level="error", text=human_msg))
                    )
                    await bus.publish(
                        event_to_dict(ErrorEvent(job_id=job_id, message=human_msg))
                    )

        except Exception as inner_exc:
            logger.error(
                "_handle_limit_reached: failed to handle limit for job %s: %s",
                job_id,
                inner_exc,
            )

    async def _handle_session_expired(self, job_id: str, exc: Exception) -> None:
        """Auto-soft-reset once on Gemini session expiry; fail permanently on second try.

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
                                text="Gemini session expired — auto-retrying from scratch",
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
