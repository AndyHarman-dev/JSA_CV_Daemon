"""Repository functions: get_job, list_jobs, upsert_job, checkpoint, etc."""

from datetime import datetime
from sqlalchemy import select, or_, exists, update
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
                (Job.state == JobState.awaiting_input) & answered_followup,
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
    if job.state == JobState.approved:
        return  # terminal state — do not mark failed
    transition(job, JobState.failed, None)  # validates transition and clears current_stage
    job.error = error
    job.updated_at = datetime.utcnow()
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
