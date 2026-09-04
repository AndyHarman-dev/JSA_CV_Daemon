"""Repository functions: get_job, list_jobs, upsert_job, checkpoint, etc."""

import json
from datetime import datetime
from sqlalchemy import select, or_, exists, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from jsa.db.models import Job, Message, Document, FollowUp, RevisionRequest, JobState, Stage


async def get_job(session: AsyncSession, job_id: str) -> Job | None:
    """Return the Job with the given id, or None if not found."""
    result = await session.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


async def get_state_fresh(session: AsyncSession, job_id: str) -> JobState | None:
    """Read Job.state on a brand-new session bound to the same engine.

    A long-lived session (e.g. an orchestrator worker holding a Job object for
    the duration of a slow agent call) may have an open read transaction that
    predates a concurrent commit elsewhere (e.g. a dismiss). A same-session
    SELECT can therefore return a stale snapshot. Opening a fresh session
    guarantees we see the latest committed state. Returns None if the job no
    longer exists (e.g. deleted concurrently).
    """
    async with AsyncSession(session.bind, expire_on_commit=False) as fresh:
        result = await fresh.execute(select(Job.state).where(Job.id == job_id))
        return result.scalar_one_or_none()


async def get_unconsumed_revision_origin(session: AsyncSession, job_id: str) -> str | None:
    """Return `RevisionRequest.origin_state` for the job's unconsumed revision request.

    Used by three call sites that all need "which state (cv_review or review) did the
    in-flight CV revision come from": _handle_final's revising_cv completion
    (jsa/pipeline/stages.py), backend_switch_reset's BF-19 rewind, and recovery_sweep's
    crash-recovery rewind. Single shared query so the origin_state contract (NULL — legacy
    rows — reads as "review") only has to be maintained in one place.

    The `uq_revision_open` partial unique index on RevisionRequest (job_id WHERE
    consumed_at IS NULL) guarantees at most one row can match, so `.scalar_one_or_none()`
    is safe here rather than an over-defensive `.first()` that would silently mask two
    live unconsumed rows as a bug elsewhere (e.g. a missing duplicate-request guard).
    """
    result = await session.execute(
        select(RevisionRequest.origin_state).where(
            RevisionRequest.job_id == job_id,
            RevisionRequest.consumed_at.is_(None),
        )
    )
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
    If new: insert with state=queued (parked; requires an explicit LAUNCH — see
    ARCH.md "Manual job launch"), all fields from job_data.
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
            # DEPRECATED — no longer populated or read; cv_structure.json is the source
            # of truth for CV content. Kept only because existing sqlite DBs have this
            # column NOT NULL (create_all + additive ALTER TABLE, no migration/drop story).
            cv_text=job_data.get("cv_text", ""),
            state=JobState.queued,
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
    2. state == fit_done (fit check passed/ignored → run cv_adjust)
    3. state == cv_done
    4. state == awaiting_input AND has a FollowUp with answered_at IS NOT NULL for current_stage
    5. state == review AND has an unconsumed RevisionRequest (consumed_at IS NULL)
    6. state == cv_review AND has an unconsumed RevisionRequest (consumed_at IS NULL) —
       a bare cv_review job is parked awaiting user approve/revise, not runnable.
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
                Job.state == JobState.fit_done,
                Job.state == JobState.cv_done,
                (Job.state == JobState.awaiting_input) & answered_followup & no_open_followup,
                (Job.state == JobState.review) & unconsumed_revision,
                (Job.state == JobState.cv_review) & unconsumed_revision,
            )
        )
        .order_by(Job.updated_at.asc())
    )

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_failed(session: AsyncSession, job_id: str, error: str) -> None:
    """Set job.state = failed (via transition), job.error = error. Preserves current_stage (deliberate BF-15 exception — see ARCH.md) so soft_reset_job can discriminate which stage failed."""
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
    # A job that burned its model ladder must not restart permanently pinned to the
    # priciest rung it reached — that would be a silent, surprising expensive-model
    # default on the next attempt, contradicting the whole point of the ladder.
    job.model_name = None
    job.model_hops = 0
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
    # Same reasoning as soft_reset_job above — a full reset must not carry forward a
    # stale model-ladder rung either.
    job.model_name = None
    job.model_hops = 0
    transition(job, JobState.pending, None)
    job.updated_at = datetime.utcnow()
    session.add(job)
    await session.commit()


async def backend_switch_reset(
    session: AsyncSession,
    job: Job,
    new_backend_name: str,
    failed_stage: "Stage",
    *,
    new_model_name: str | None = None,
    increment_model_hops: bool = False,
) -> None:
    """Reset job state after AgentLimitReached so the new backend starts fresh (BF-19).

    Strategy mirrors soft_reset_job but is invoked from a running job (not a
    failed one) and writes backend_name in the same atomic commit.

    failed_stage mapping:
      revising_cv / revising_cl → rewind to review  (delete revision Messages+FollowUps)
      cover_letter              → rewind to cv_done (delete CL Messages only)
      cv_adjust / None          → rewind to pending (delete all Messages)

    Revision stages rewind to review (not cv_done/pending) so the unconsumed
    RevisionRequest is still in place and the orchestrator re-dispatches the
    revision on the new backend.

    Session IDs for the failed stage are cleared so run_stage takes the
    fresh-session branch on the next dispatch.

    `new_model_name` / `increment_model_hops` (Phase 4, model-first fallback ladder):
    when this call is a model-ladder HOP (same backend, next model rung),
    `new_backend_name` is simply `job.backend_name` unchanged and the caller passes
    `new_model_name=<next rung>`, `increment_model_hops=True`. When this call is a
    genuine BACKEND advance (or the ladder is exhausted/inapplicable), the caller
    passes neither — the unconditional assignment below then nulls `model_name` and
    zeroes `model_hops` for every existing backend-advance call site with no extra
    code, so a new backend always starts at its own entry point rather than
    inheriting a rung it may not even host. Both fields land in this function's
    single existing commit, satisfying the Checkpoint rule (CLAUDE.md) — assigning
    them after this call returns would be a second, uncommitted transaction that
    silently drops the hop.
    """
    from jsa.pipeline.state_machine import transition, set_current_stage

    job.backend_name = new_backend_name
    job.model_name = new_model_name
    job.model_hops = job.model_hops + 1 if increment_model_hops else 0

    if failed_stage in (Stage.revising_cv, Stage.revising_cl):
        # Delete revision-stage Messages and ALL FollowUps for this revision.
        # Important: delete answered FollowUps too, not just open ones.
        # _is_revision_resume checks for answered FollowUps with answered_at >
        # rev_req.created_at; leaving them causes the resume path to run with
        # empty history (Messages were deleted) → silent divergence on new backend.
        # Deleting all FollowUps ensures _is_revision_resume returns False and
        # the revision restarts fresh on the new backend.
        await session.execute(
            delete(Message).where(
                Message.job_id == job.id,
                Message.stage == failed_stage,
            )
        )
        await session.execute(
            delete(FollowUp).where(
                FollowUp.job_id == job.id,
                FollowUp.stage == failed_stage,
            )
        )
        # Clear the relevant session ID
        if failed_stage == Stage.revising_cv:
            job.cv_session_id = None
        else:
            job.cl_session_id = None
        job.session_external_id = None

        # Rewind destination: revising_cl always returns to review (no cl_review state
        # exists). revising_cv can have been requested from either the CV gate or final
        # review — see RevisionRequest.origin_state (Phase 1 of the two-lane pipeline
        # split) — so it must rewind to whichever one it came from, not unconditionally
        # to review (a bare cv_review→running(revising_cv)→review would land a job in
        # "final review" with no cover_letter Document yet).
        dest_state = JobState.review
        if failed_stage == Stage.revising_cv:
            origin_state = await get_unconsumed_revision_origin(session, job.id)
            if origin_state == JobState.cv_review.value:
                dest_state = JobState.cv_review

        # transition() sets current_stage=None on entry to review/cv_review (correct —
        # those parked states carry current_stage only while a revision is in flight, via
        # set_current_stage). But we need current_stage restored to revising_* so
        # list_runnable_jobs / _next_stage_for dispatch via the revision path on the new
        # backend. Use set_current_stage after transition.
        transition(job, dest_state, None)
        set_current_stage(job, failed_stage)  # restore revising_* so orchestrator re-dispatches

    elif failed_stage == Stage.cover_letter:
        # Delete cover_letter Messages and open FollowUps — keep cv_adjust Messages.
        await session.execute(
            delete(Message).where(
                Message.job_id == job.id,
                Message.stage == Stage.cover_letter,
            )
        )
        await session.execute(
            delete(FollowUp).where(
                FollowUp.job_id == job.id,
                FollowUp.stage == Stage.cover_letter,
                FollowUp.answered_at.is_(None),
            )
        )
        job.cl_session_id = None
        job.session_external_id = None
        # running(cover_letter) → cv_done (guard extended in state_machine for BF-19)
        transition(job, JobState.cv_done, None)

    else:
        # cv_adjust, or None (failure before any stage started) → rewind to pending
        await session.execute(delete(Message).where(Message.job_id == job.id))
        await session.execute(delete(FollowUp).where(
            FollowUp.job_id == job.id,
            FollowUp.answered_at.is_(None),
        ))
        job.cv_session_id = None
        job.cl_session_id = None
        job.session_external_id = None
        # running(cv_adjust) → pending
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
            reasoning=msg.get("reasoning"),
        )
        session.add(m)

    # 3. Insert Document row (append-only)
    if document is not None:
        doc = Document(
            job_id=job.id,
            stage=document["stage"],
            version=document["version"],
            markdown=document["markdown"],
            structured=document.get("structured"),
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
            suggested_replies = follow_up.get("suggested_replies")
            fu = FollowUp(
                job_id=job.id,
                stage=follow_up["stage"],
                question=follow_up["question"],
                suggested_replies=(
                    json.dumps(suggested_replies) if suggested_replies is not None else None
                ),
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

    - current_stage == revising_cv → rewind to wherever the revision was requested from
      (cv_review or review — see RevisionRequest.origin_state), preserving the unconsumed
      RevisionRequest so it re-dispatches.
    - current_stage == revising_cl → rewind to review (revising_cl always returns to
      review — there is no cl_review parked state), preserving the unconsumed
      RevisionRequest so it re-dispatches. Mirrors the revising_cv branch above; without
      this a crash mid revising_cl fell through to the generic cl_docs heuristic below,
      which finds the pre-existing (pre-revision) cover_letter Document, lands the job on
      the dead-end `cl_done` state (no dispatch arm in list_runnable_jobs), and orphans the
      unconsumed RevisionRequest forever.
    - current_stage == cv_adjust (plain, not a revision) → rewind to pending, never to
      cv_done. A completed cv_adjust run always transitions atomically past `running`
      straight to `cv_review` in the same checkpoint (see stages.py::_handle_final) — being
      caught here mid-run means THIS attempt did not complete, no matter what cv_adjust
      Document a prior job cycle may have left behind (Documents are append-only and are
      never deleted by soft_reset_job/JD-hash resets). Treating an old Document as proof of
      completion — the pre-two-lane-split heuristic this replaces — would silently
      fast-forward the job to cv_done, bypassing the cv_review approval gate for a CV the
      user never actually approved this cycle. See CLAUDE.md → "Two-lane pipeline / CV
      gate" and jsa/pipeline/state_machine.py's running→cv_done guard (narrowed to
      Stage.cover_letter only, in lockstep with this branch).
    - current_stage == cover_letter (plain): a cover_letter Document already exists →
      cl_done (crash-recovery landing state only). No cover_letter Document yet → cv_done
      (rewind to re-dispatch cover_letter fresh — NOT pending, which would needlessly
      discard an already-approved CV; reaching running(cover_letter) at all already
      required cv_done, so neither branch can bypass the CV gate).
    - fit_assessment, or current_stage is None → pending (no signal to discriminate on).
    Jobs in awaiting_input are untouched. Jobs in review/approved/failed/cv_review are
    untouched (only 'running' jobs are swept).
    """
    from jsa.pipeline.state_machine import transition, set_current_stage

    stmt = select(Job).where(Job.state == JobState.running)
    result = await session.execute(stmt)
    running_jobs = list(result.scalars().all())

    for job in running_jobs:
        if job.current_stage in (Stage.revising_cv, Stage.revising_cl):
            revising_stage = job.current_stage  # transition() below clears current_stage
            dest_state = JobState.review
            if revising_stage == Stage.revising_cv:
                origin_state = await get_unconsumed_revision_origin(session, job.id)
                if origin_state == JobState.cv_review.value:
                    dest_state = JobState.cv_review
            transition(job, dest_state, new_stage=None)
            set_current_stage(job, revising_stage)  # restore so it re-dispatches
            job.updated_at = datetime.utcnow()
            session.add(job)
            continue

        if job.current_stage == Stage.cv_adjust:
            # Never treat Document presence as a completion signal here — see docstring.
            transition(job, JobState.pending, new_stage=None)
            job.updated_at = datetime.utcnow()
            session.add(job)
            continue

        if job.current_stage == Stage.cover_letter:
            cl_docs = await get_documents(session, job.id, stage=Stage.cover_letter)
            if cl_docs:
                # A cover_letter FINAL already landed for this cycle (crash happened
                # after the Document was written, e.g. mid-render) — cl_done, a
                # crash-recovery-only landing state (cover_letter's own checkpoint
                # never uses it in normal flow).
                transition(job, JobState.cl_done, new_stage=None)
            else:
                # Crashed before this cycle's cover_letter attempt completed.
                # Reaching running(cover_letter) at all already required cv_done (the
                # state machine gates it) — rewind there, never to pending, which
                # would needlessly discard an already-approved CV and force the whole
                # pipeline (fit_assessment → cv_adjust → cv_review) to redo work.
                transition(job, JobState.cv_done, new_stage=None)
            job.updated_at = datetime.utcnow()
            session.add(job)
            continue

        # fit_assessment, or current_stage is None: no signal to discriminate on.
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


async def set_job_base_cv(session: AsyncSession, job: Job, deck_id: str | None) -> None:
    """Assign (or clear, with `deck_id=None`) the job's base-CV deck reference.

    A plain field write + commit -- not a `Job.state`/`current_stage` transition, so
    this does not go through `jsa.pipeline.state_machine.transition` or `checkpoint()`
    (CLAUDE.md's checkpoint rule covers state-changing writes; `base_cv_id` is an
    auxiliary field the caller (`PUT /api/jobs/{id}/base-cv`) has already gated to
    `state == queued` before calling this). Callers must never write raw SQL for this --
    that rule is why this helper exists instead of an inline UPDATE in the route.
    """
    job.base_cv_id = deck_id
    job.updated_at = datetime.utcnow()
    session.add(job)
    await session.commit()


# States in which a job still "holds" its base CV deck, i.e. deleting that deck is
# refused (409 from DELETE /api/cv-decks/{id}).
#
# The rule is "can this job still open a FRESH agent session against the deck file?",
# not "is this job running right now". A stage only re-reads the deck when it has no
# Message rows for that job+stage (jsa/pipeline/stages.py's fresh-vs-resume branch), so
# a mid-flight job is immune to the file vanishing -- until something wipes its Messages
# and forces a fresh session. `repo.backend_switch_reset` does exactly that on a BF-19
# backend switch or a model-ladder hop, rewinding a cv_adjust failure to `pending`.
# Hence:
#
#   `pending` HOLDS -- it is both "launched, awaiting dispatch" and the landing state of
#   that rewind; releasing it is what let a mid-flight job silently switch to the default
#   deck while `base_cv_id` still named the deleted one.
#
#   `failed` HOLDS -- a re-run resets it to `pending` and re-dispatches into a fresh
#   session, so it can still re-read the deck. Deleting or dismissing the job is the
#   intended escape hatch for a permanently-failed job pinning a deck.
#
#   `queued` RELEASES -- never dispatched, so `clear_base_cv_assignments` just nulls the
#   assignment (unchanged behaviour). `approved`/`dismissed` RELEASE -- terminal, nothing
#   will re-read the deck.
#
# Editing a deck is deliberately NOT gated: a job that has already started has the CV
# baked into its Message rows, so a PUT cannot reach it mid-flight anyway.
DECK_LOCK_STATES: tuple[JobState, ...] = (
    JobState.pending,
    JobState.running,
    JobState.awaiting_input,
    JobState.fit_done,
    JobState.unfit,
    JobState.cv_review,
    JobState.cv_done,
    JobState.cl_done,
    JobState.review,
    JobState.failed,
)


async def count_jobs_holding_decks(session: AsyncSession) -> dict[str, int]:
    """Return `{deck_id: n}` for every deck currently held by >=1 job.

    One grouped query for ALL decks rather than one per deck: `GET /api/cv-decks` needs
    this for every row it renders, and that endpoint's whole contract is that listing
    decks stays cheap. Decks held by nobody are simply absent from the mapping, so
    callers should read it with `.get(deck_id, 0)`.
    """
    stmt = (
        select(Job.base_cv_id, func.count())
        .where(Job.base_cv_id.is_not(None), Job.state.in_(DECK_LOCK_STATES))
        .group_by(Job.base_cv_id)
    )
    rows = await session.execute(stmt)
    return {deck_id: count for deck_id, count in rows.all() if deck_id is not None}


async def count_jobs_holding_deck(session: AsyncSession, deck_id: str) -> int:
    """Single-deck form of `count_jobs_holding_decks`, for the delete guard."""
    stmt = select(func.count()).where(
        Job.base_cv_id == deck_id, Job.state.in_(DECK_LOCK_STATES)
    )
    return int((await session.execute(stmt)).scalar_one())


async def clear_base_cv_assignments(session: AsyncSession, deck_id: str) -> int:
    """Clear `base_cv_id` on undispatched jobs that reference `deck_id`. Returns the count.

    Scoped to `state IN (queued, pending)` -- deliberately narrow. A job already past
    those states (running, awaiting_input, fit_done, cv_review, cv_done, cl_done, review,
    approved, failed) keeps its `base_cv_id` untouched as a record of which base CV it
    was actually built from; only a job that hasn't been dispatched yet has that
    assignment silently invalidated by the deck's deletion. Called by
    `DELETE /api/cv-decks/{id}` (owned by a different phase/agent -- not wired here, see
    the Phase 3 task notes). Uses a bulk ORM `update()`, never raw text SQL.
    """
    stmt = (
        update(Job)
        .where(Job.base_cv_id == deck_id, Job.state.in_([JobState.queued, JobState.pending]))
        .values(base_cv_id=None)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount
