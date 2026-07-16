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


class _RateLimitedBackend(AgentBackend):
    """Wraps an AgentBackend so all of ITS provider calls share a per-backend-name
    semaphore across concurrently-running jobs (see Orchestrator._call_semaphore).

    This is deliberately separate from Orchestrator.sem: `sem` bounds how many
    JOBS run concurrently (max_parallel job slots); this wrapper bounds how many
    in-flight PROVIDER CALLS share the same backend/account at once. Without it,
    all 5 job slots can call out to the same account-rate-limited provider at
    the same instant and trip a shared limit together. Pacing calls through a
    smaller shared semaphore reduces (does not eliminate) that chance.

    Only wraps the AgentBackend ABC methods, plus `run_research` if the wrapped
    backend defines it (duck-typed, checked via hasattr by
    jsa.pipeline.stages._gather_research) — forwarding is conditional so
    hasattr(wrapper, "run_research") stays False for backends that don't have it.
    """

    def __init__(self, inner: AgentBackend, call_sem: asyncio.Semaphore) -> None:
        self._inner = inner
        self._call_sem = call_sem
        self.name = inner.name
        self.supports_history_replay = getattr(inner, "supports_history_replay", False)
        if hasattr(inner, "run_research"):
            self.run_research = self._run_research  # type: ignore[method-assign]

    async def start_session(self, system_prompt, initial_user_msg):
        async with self._call_sem:
            return await self._inner.start_session(system_prompt, initial_user_msg)

    async def restore_session(self, system_prompt, history, external_id):
        async with self._call_sem:
            return await self._inner.restore_session(system_prompt, history, external_id)

    async def send_message(self, handle, text):
        async with self._call_sem:
            return await self._inner.send_message(handle, text)

    async def end_session(self, handle) -> None:
        # Teardown is cheap/no-op for every current backend — no need to gate
        # it behind the same semaphore as the actual generation calls.
        await self._inner.end_session(handle)

    async def _run_research(self, agent_name: str, query: str) -> str:
        async with self._call_sem:
            return await self._inner.run_research(agent_name, query)


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
        _call_semaphores: Per-backend-name semaphores (lazily created) that pace
            concurrent PROVIDER calls to the same backend — separate from `sem`,
            which paces concurrent JOBS. See _RateLimitedBackend.
        _limit_retry_max_attempts / _limit_retry_base_delay / _limit_retry_max_delay:
            Bounded exponential backoff applied to an AgentLimitReached on the
            SAME backend before falling through to _handle_limit_reached's
            backend-switch/fail logic — gives a transient/shared rate limit a
            chance to clear without discarding a stage's progress via switch.
    """

    def __init__(
        self,
        db_session_factory: Callable[[], AsyncSession],
        backend_factory: Callable,
        backends: list[str] | None = None,
        max_parallel: int = 5,
        output_dir: Path | None = None,
        cv_structure_path: Path | None = None,
        preferences_path: Path | None = None,
        max_concurrent_calls_per_backend: int | None = None,
        limit_retry_max_attempts: int = 3,
        limit_retry_base_delay: float = 0.25,
        limit_retry_max_delay: float = 5.0,
    ) -> None:
        self.sem = asyncio.Semaphore(max_parallel)
        self.wakeup = asyncio.Event()
        self._db_session_factory = db_session_factory
        self._backend_factory = _wrap_factory(backend_factory)
        self._backends = backends if backends is not None else ["claude-cli"]
        self._output_dir = output_dir
        self._cv_structure_path = cv_structure_path
        self._preferences_path = preferences_path
        self._stopping = False
        # Keyed by job_id (not an unkeyed set) so a specific job's in-flight
        # worker task can be looked up and cancelled — see cancel_task().
        self._tasks: dict[str, asyncio.Task] = {}
        # Shared provider-call rate limiter: one semaphore per backend name,
        # created lazily so tests/simple callers that never hit this path pay
        # nothing extra. Defaults to max_parallel (i.e. never actually blocks
        # — an uncontended asyncio.Semaphore.acquire() doesn't suspend, so
        # this is a no-op by default, byte-identical to pre-U6 concurrency)
        # so pacing is opt-in: an operator who wants to throttle a single
        # backend/account below full job-slot concurrency passes a smaller
        # value explicitly.
        self._max_concurrent_calls_per_backend = (
            max_concurrent_calls_per_backend
            if max_concurrent_calls_per_backend is not None
            else max_parallel
        )
        self._call_semaphores: dict[str, asyncio.Semaphore] = {}
        # Backoff/retry (same backend) before treating AgentLimitReached as a
        # switch-worthy event — see _retry_stage_with_backoff.
        self._limit_retry_max_attempts = limit_retry_max_attempts
        self._limit_retry_base_delay = limit_retry_base_delay
        self._limit_retry_max_delay = limit_retry_max_delay

    def _get_call_semaphore(self, backend_name: str) -> asyncio.Semaphore:
        """Return (creating if needed) the shared provider-call semaphore for backend_name."""
        sem = self._call_semaphores.get(backend_name)
        if sem is None:
            sem = asyncio.Semaphore(self._max_concurrent_calls_per_backend)
            self._call_semaphores[backend_name] = sem
        return sem

    def kick(self) -> None:
        """Wake the run() loop. Called by API routes after answer/revise."""
        self.wakeup.set()

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
                # Keying by job_id (rather than an unkeyed set) lets cancel_task()
                # look up and cancel a specific job's in-flight task (e.g. on dismiss).
                task = asyncio.create_task(self._run_one(job.id))
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

    async def _run_one(self, job_id: str) -> None:
        """Worker task: open a session, run the stage, handle outcome.

        The FIRST attempt at the stage uses exactly the original (pre-BF-19
        retry/backoff) single-session shape — open one session, fetch the
        job once, dispatch — so the common case (no limit hit) pays zero
        extra session-churn cost. Only if that first attempt raises
        AgentLimitReached do we fall through to _retry_stage_with_backoff,
        which retries on fresh sessions (see its docstring for why).

        Always releases the semaphore and kicks the loop in finally.
        """
        try:
            backend: AgentBackend | None = None
            stage: Stage | None = None
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
                backend = _RateLimitedBackend(
                    self._backend_factory(active_backend_name),
                    self._get_call_semaphore(active_backend_name),
                )
                try:
                    await stages.run_stage(
                        job, backend, stage, session,
                        output_dir=self._output_dir,
                        cv_structure_path=self._cv_structure_path,
                        preferences_path=self._preferences_path,
                    )
                    return
                except AgentLimitReached:
                    pass  # session closes normally on exit; retry below on fresh ones

            # Only reached if the first attempt (just above) raised
            # AgentLimitReached. backend/stage are always set by this point.
            await self._retry_stage_with_backoff(job_id, backend, stage)

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
            self.kick()

    async def _retry_stage_with_backoff(
        self,
        job_id: str,
        backend: AgentBackend,
        stage: Stage,
    ) -> None:
        """Retry run_stage on the SAME backend with bounded exponential backoff,
        after the caller's (_run_one's) OWN first attempt already raised
        AgentLimitReached once.

        This sits strictly before backend-switch handling: if every retry here
        also raises, this re-raises AgentLimitReached so _run_one's existing
        `except AgentLimitReached` still triggers _handle_limit_reached exactly
        as before — the existing switch-then-eventually-mark-failed behavior is
        unchanged. This only adds a chance for a transient/shared rate limit to
        clear before that happens.

        Only called on the (rare) retry path — the common, no-limit-hit case
        never reaches this method, so it costs nothing there. Each retry here
        opens a BRAND NEW session and re-fetches the Job fresh (rather than
        reusing one across retries): a prior attempt's `run_stage` call may
        have issued reads (e.g. _load_history) that leave an implicit
        transaction open, and/or left the in-memory `job` object holding
        uncommitted mutations (e.g. `job.session_external_id`) when the backend
        call raised. Reusing that same session/object on retry risks a stuck or
        failing later commit — a fresh session + fresh fetch per attempt
        sidesteps that entirely. Since nothing is committed to the DB until a
        stage actually completes (FINAL/NEED_INPUT checkpoint), re-running from
        a fresh fetch is safe and side-effect-free beyond re-paying for the
        calls made so far.
        """
        attempt = 1  # the caller's own first attempt already failed once
        while True:
            if attempt > self._limit_retry_max_attempts:
                raise AgentLimitReached(
                    f"Backend {backend.name} limit reached after "
                    f"{self._limit_retry_max_attempts} retries"
                )
            delay = min(
                self._limit_retry_base_delay * (2 ** (attempt - 1)),
                self._limit_retry_max_delay,
            )
            logger.warning(
                "_retry_stage_with_backoff: job %s hit backend %s limit "
                "(attempt %d/%d) — retrying same backend after %.2fs",
                job_id, backend.name, attempt, self._limit_retry_max_attempts, delay,
            )
            await bus.publish(
                event_to_dict(
                    LogEvent(
                        job_id=job_id,
                        level="warn",
                        text=(
                            f"Backend limit reached — retrying {backend.name} "
                            f"(attempt {attempt}/{self._limit_retry_max_attempts}) "
                            f"after {delay:.1f}s"
                        ),
                    )
                )
            )
            await asyncio.sleep(delay)

            async with self._db_session_factory() as session:
                job = await repo.get_job(session, job_id)
                if job is None:
                    logger.error(
                        "_retry_stage_with_backoff: job %s not found", job_id
                    )
                    return
                try:
                    await stages.run_stage(
                        job, backend, stage, session,
                        output_dir=self._output_dir,
                        cv_structure_path=self._cv_structure_path,
                        preferences_path=self._preferences_path,
                    )
                    return
                except AgentLimitReached:
                    attempt += 1
                    continue

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
