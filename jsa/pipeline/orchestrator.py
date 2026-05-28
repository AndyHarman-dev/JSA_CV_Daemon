"""Dispatcher: asyncio.Semaphore(5) worker pool and runnable job queue."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend
from jsa.db import repo
from jsa.db.models import Job, JobState, Stage
from jsa.events.bus import bus
from jsa.events.schema import ErrorEvent, LogEvent, StatusChangedEvent, event_to_dict
from jsa.pipeline import stages
from jsa.pipeline.stages import PausedForInput
from jsa.pipeline.state_machine import transition

logger = logging.getLogger(__name__)


def _next_stage_for(job: Job) -> Stage:
    """Determine which stage to run for the given job.

    State / current_stage mapping:
    - pending                     → cv_adjust (fresh)
    - cv_done                     → cover_letter (fresh)
    - awaiting_input              → job.current_stage (resume)
    - review + unconsumed rev req → job.current_stage (revising_cv / revising_cl)
    """
    if job.state == JobState.pending:
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


class Orchestrator:
    """Dispatcher loop that manages concurrent pipeline stages.

    Attributes:
        sem: Semaphore limiting parallel agent sessions to max_parallel.
        wakeup: Event set by kick() to wake the run() loop.
        _db_session_factory: Callable that returns a new async SQLAlchemy session.
        _backend_factory: Callable that returns a fresh AgentBackend instance.
        _stopping: Flag to signal graceful shutdown.
    """

    def __init__(
        self,
        db_session_factory: Callable[[], AsyncSession],
        backend_factory: Callable[[], AgentBackend],
        max_parallel: int = 5,
    ) -> None:
        self.sem = asyncio.Semaphore(max_parallel)
        self.wakeup = asyncio.Event()
        self._db_session_factory = db_session_factory
        self._backend_factory = backend_factory
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
                runnable = await repo.list_runnable_jobs(session)

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

                backend = self._backend_factory()
                await stages.run_stage(job, backend, stage, session)

        except PausedForInput:
            # Job successfully parked — not an error
            pass

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
