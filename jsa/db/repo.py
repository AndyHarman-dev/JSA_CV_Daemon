"""Repository functions: get_job, list_jobs, upsert_job, checkpoint, etc."""

from datetime import datetime
from sqlalchemy import select, or_, exists, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from jsa.db.models import Job, Message, Document, FollowUp, RevisionRequest, JobState, Stage


async def get_job(session: AsyncSession, job_id: str) -> Job | None:
    """Return the Job with the given id, or None if not found."""
    result = await session.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


async def list_jobs(session: AsyncSession, state: JobState | None = None) -> list[Job]:
    """Return all jobs, optionally filtered by state."""
    stmt = select(Job)
    if state is not None:
        stmt = stmt.where(Job.state == state)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_job(session: AsyncSession, job_data: dict) -> Job:
    """INSERT or UPDATE a job.

    If the job exists: update fields that can change (jd, jd_hash, tier, link).
    Do NOT overwrite state/current_stage/session_external_id/error.
    If new: insert with state=pending, all fields from job_data.
    """
    job_id = job_data["id"]
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()

    if job is None:
        # Insert new job
        job = Job(
            id=job_id,
            company=job_data["company"],
            role=job_data["role"],
            link=job_data["link"],
            tier=job_data["tier"],
            jd=job_data["jd"],
            jd_hash=job_data["jd_hash"],
            cv_text=job_data.get("cv_text", ""),
            state=JobState.pending,
            current_stage=None,
            session_external_id=None,
            error=None,
        )
        session.add(job)
        await session.flush()
    else:
        # Update only the fields that can change
        job.jd = job_data["jd"]
        job.jd_hash = job_data["jd_hash"]
        job.tier = job_data["tier"]
        job.link = job_data["link"]
        if "cv_text" in job_data:
            job.cv_text = job_data["cv_text"]
        # Per ARCH.md § Job identity: failed jobs reset to pending on re-run so
        # the pipeline re-processes them without requiring a manual /reset call.
        # CSV re-import of a failed job is always nuclear (ARCH.md BF-15).
        # nuclear_reset_job includes its own commit, which persists the field
        # updates already set above together with the nuclear reset.
        if job.state == JobState.failed:
            await nuclear_reset_job(session, job)
        else:
            job.updated_at = datetime.utcnow()
            await session.flush()

    return job


async def list_runnable_jobs(session: AsyncSession) -> list[Job]:
    """Return jobs that are ready to be worked on, FIFO by updated_at.

    Runnable conditions:
    1. state == pending
    2. state == cv_done
    3. state == awaiting_input AND has a FollowUp with answered_at IS NOT NULL for current_stage
    4. state == review AND has an unconsumed RevisionRequest (consumed_at IS NULL)
    """
    # Condition 3: awaiting_input with an answered follow-up for the current stage
    answered_followup = (
        select(FollowUp.id)
        .where(
            FollowUp.job_id == Job.id,
            FollowUp.stage == Job.current_stage,
            FollowUp.answered_at.is_not(None),
        )
        .correlate(Job)
        .exists()
    )

    # Condition 3b: no open (unanswered) follow-up still pending for this stage
    no_open_followup = ~(
        select(FollowUp.id)
        .where(
            FollowUp.job_id == Job.id,
            FollowUp.stage == Job.current_stage,
            FollowUp.answered_at.is_(None),
        )
        .correlate(Job)
        .exists()
    )

    # Condition 4: review with an unconsumed revision request
    unconsumed_revision = (
        select(RevisionRequest.id)
        .where(
            RevisionRequest.job_id == Job.id,
            RevisionRequest.consumed_at.is_(None),
        )
        .correlate(Job)
        .exists()
    )

    stmt = (
        select(Job)
        .where(
            or_(
                Job.state == JobState.pending,
                Job.state == JobState.cv_done,
                (Job.state == JobState.awaiting_input) & answered_followup & no_open_followup,
                (Job.state == JobState.review) & unconsumed_revision,
            )
        )
        .order_by(Job.updated_at.asc())
    )

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_failed(session: AsyncSession, job_id: str, error: str) -> None:
    """Set job.state = failed (via transition), job.error = error, clear current_stage."""
    from jsa.pipeline.state_machine import transition

    job = await get_job(session, job_id)
    if job is None:
        return
    if job.state in (JobState.approved, JobState.dismissed):
        return  # terminal user-controlled state — do not mark failed
    prev_state = job.state
    stage_at_failure = job.current_stage          # capture before transition clears it
    transition(job, JobState.failed, None)        # validates state→failed, sets current_stage=None
    # Deliberately restore current_stage after transition — ARCH.md BF-15 documented exception.
    # transition() forces current_stage=None on entry to 'failed', but we need to remember
    # which stage failed so soft_reset_job can discriminate. failed jobs are never dispatched
    # by list_runnable_jobs, so this is safe.
    job.current_stage = stage_at_failure
    job.error = error
    job.updated_at = datetime.utcnow()
    await session.commit()

    # Notify the frontend so it can refresh the job list immediately.
    from jsa.events.bus import bus
    from jsa.events.schema import StatusChangedEvent, event_to_dict

    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job_id,
                from_state=prev_state.value,
                to_state=JobState.failed.value,
            )
        )
    )


async def soft_reset_job(session: AsyncSession, job: Job) -> None:
    """Rewind the failed stage only. Clears its Messages/FollowUps/session ID.

    failed_stage == cover_letter or revising_cl → rewind to cv_done
    failed_stage == cv_adjust, revising_cv, or None → rewind to pending

    Sets retry_count = 1. The job must be in state 'failed' on entry.
    """
    from jsa.pipeline.state_machine import transition

    failed_stage = job.current_stage

    if failed_stage in (Stage.cover_letter, Stage.revising_cl):
        # Delete cover_letter and revising_cl messages/followups only —
        # keep cv_adjust messages so the orchestrator can resume with full CV context.
        await session.execute(
            delete(Message).where(
                Message.job_id == job.id,
                Message.stage.in_([Stage.cover_letter, Stage.revising_cl]),
            )
        )
        await session.execute(
            delete(FollowUp).where(
                FollowUp.job_id == job.id,
                FollowUp.stage.in_([Stage.cover_letter, Stage.revising_cl]),
            )
        )
        # Delete any unconsumed revision request (covers revision-stage failures)
        await session.execute(
            delete(RevisionRequest).where(
                RevisionRequest.job_id == job.id,
                RevisionRequest.consumed_at.is_(None),
            )
        )
        job.cl_session_id = None
        job.session_external_id = None
        # Transition failed → cv_done (new edge added to ALLOWED in BF-15)
        transition(job, JobState.cv_done, None)
    else:
        # cv_adjust, revising_cv, or None — rewind to pending
        await session.execute(delete(Message).where(Message.job_id == job.id))
        await session.execute(delete(FollowUp).where(FollowUp.job_id == job.id))
        await session.execute(delete(RevisionRequest).where(RevisionRequest.job_id == job.id))
        job.cv_session_id = None
        job.cl_session_id = None
        job.session_external_id = None
        transition(job, JobState.pending, None)

    job.retry_count = 1
    job.error = None
    job.updated_at = datetime.utcnow()
    session.add(job)
    await session.commit()


async def nuclear_reset_job(session: AsyncSession, job: Job) -> None:
    """Delete all job data and reset to pending (retry_count=0).

    Explicit DELETE statements are used (not ORM cascade) because the job row
    is reset, not deleted — there is no cascade to trigger.
    """
    from jsa.pipeline.state_machine import transition

    await session.execute(delete(Message).where(Message.job_id == job.id))
    await session.execute(delete(Document).where(Document.job_id == job.id))
    await session.execute(delete(FollowUp).where(FollowUp.job_id == job.id))
    await session.execute(delete(RevisionRequest).where(RevisionRequest.job_id == job.id))

    job.cv_session_id = None
    job.cl_session_id = None
    job.session_external_id = None
    job.error = None
    job.retry_count = 0
    transition(job, JobState.pending, None)
    job.updated_at = datetime.utcnow()
    session.add(job)
    await session.commit()


async def checkpoint(
    session: AsyncSession,
    job: Job,
    new_state: JobState,
    new_stage: Stage | None,
    *,
    messages: list[dict] | None = None,
    document: dict | None = None,
    follow_up: dict | None = None,
) -> None:
    """Write all state changes in a single atomic transaction.

    Steps:
    1. Call transition(job, new_state, new_stage) — raises InvalidTransition with nothing staged
    2. Insert Message rows (if any)
    3. Insert Document row (if provided) — append-only, new version
    4. Insert or update FollowUp row (if provided)
    5. Update Job.state, Job.current_stage, Job.updated_at, then commit.

    messages: list of {role, content} dicts — stage inferred from job.current_stage
    document: {stage, version, markdown} — inserts a new Document row
    follow_up: {stage, question} to insert a new FollowUp,
               or {follow_up_id, answer, answered_at} to mark an existing one answered
    """
    # Local import to avoid potential circular imports
    from jsa.pipeline.state_machine import transition

    if messages is None:
        messages = []

    # Capture the current stage before transition mutates it; Messages are
    # stamped with the stage that was active when they were produced.
    message_stage = job.current_stage

    # 1. Validate and apply the state transition (mutates job in-place).
    # This is a pure in-memory guard; if it raises, no rows are ever staged.
    transition(job, new_state, new_stage)

    # 2. Insert Message rows
    for msg in messages:
        m = Message(
            job_id=job.id,
            stage=message_stage,
            role=msg["role"],
            content=msg["content"],
        )
        session.add(m)

    # 3. Insert Document row (append-only)
    if document is not None:
        doc = Document(
            job_id=job.id,
            stage=document["stage"],
            version=document["version"],
            markdown=document["markdown"],
        )
        session.add(doc)

    # 4. Insert or update FollowUp row
    if follow_up is not None:
        if "follow_up_id" in follow_up:
            # Mark existing FollowUp as answered
            await session.execute(
                update(FollowUp)
                .where(FollowUp.id == follow_up["follow_up_id"])
                .values(
                    answer=follow_up["answer"],
                    answered_at=follow_up["answered_at"],
                )
            )
        else:
            # Delete any stale open FollowUp for this (job, stage) before inserting,
            # so a reset-from-failed job or a re-run doesn't hit the partial unique index.
            await session.execute(
                delete(FollowUp)
                .where(
                    FollowUp.job_id == job.id,
                    FollowUp.stage == follow_up["stage"],
                    FollowUp.answered_at.is_(None),
                )
            )
            # Insert new FollowUp
            fu = FollowUp(
                job_id=job.id,
                stage=follow_up["stage"],
                question=follow_up["question"],
            )
            session.add(fu)

    # 5. Update Job row with new state, stage, and timestamp
    job.updated_at = datetime.utcnow()
    session.add(job)

    await session.commit()


async def get_documents(
    session: AsyncSession, job_id: str, stage: Stage | None = None
) -> list[Document]:
    """Return documents for the job, optionally filtered by stage, ordered by version desc."""
    stmt = select(Document).where(Document.job_id == job_id)
    if stage is not None:
        stmt = stmt.where(Document.stage == stage)
    stmt = stmt.order_by(Document.version.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_follow_ups(
    session: AsyncSession, job_id: str, answered: bool | None = None
) -> list[FollowUp]:
    """Return follow_ups for the job, optionally filtered by whether answered_at is None or not."""
    stmt = select(FollowUp).where(FollowUp.job_id == job_id)
    if answered is True:
        stmt = stmt.where(FollowUp.answered_at.is_not(None))
    elif answered is False:
        stmt = stmt.where(FollowUp.answered_at.is_(None))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def recovery_sweep(session: AsyncSession) -> None:
    """On startup: revert any jobs stuck in 'running' to their last safe checkpoint.

    - Has cover_letter Document → set cl_done
    - Has cv_adjust Document → set cv_done
    - Neither → set pending (running→pending is now a direct transition via crash-recovery)
    Jobs in awaiting_input are untouched. Jobs in review/approved/failed are untouched.
    """
    from jsa.pipeline.state_machine import transition

    stmt = select(Job).where(Job.state == JobState.running)
    result = await session.execute(stmt)
    running_jobs = list(result.scalars().all())

    for job in running_jobs:
        cl_docs = await get_documents(session, job.id, stage=Stage.cover_letter)
        cv_docs = await get_documents(session, job.id, stage=Stage.cv_adjust)

        if cl_docs:
            # running → cl_done requires current_stage == cover_letter; set it first
            job.current_stage = Stage.cover_letter
            transition(job, JobState.cl_done, new_stage=None)
        elif cv_docs:
            # running → cv_done requires current_stage == cv_adjust; set it first
            job.current_stage = Stage.cv_adjust
            transition(job, JobState.cv_done, new_stage=None)
        else:
            transition(job, JobState.pending, new_stage=None)

        job.updated_at = datetime.utcnow()
        session.add(job)

    await session.commit()


async def answer_follow_up(
    session: AsyncSession, follow_up_id: int, answer_text: str
) -> FollowUp:
    """Record the user's answer on the FollowUp row."""
    stmt = select(FollowUp).where(FollowUp.id == follow_up_id)
    result = await session.execute(stmt)
    fu = result.scalar_one_or_none()
    if fu is None:
        raise ValueError(f"FollowUp {follow_up_id} not found")
    if fu.answered_at is not None:
        raise ValueError(f"FollowUp {follow_up_id} already answered")
    fu.answer = answer_text
    fu.answered_at = datetime.utcnow()
    session.add(fu)
    await session.commit()
    await session.refresh(fu)
    return fu
