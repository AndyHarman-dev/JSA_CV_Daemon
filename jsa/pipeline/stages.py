"""Pipeline stage runners: run_stage.

Handles both fresh sessions (pending/cv_done) and resumed sessions
(awaiting_input, revising_cv, revising_cl).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from jsa.agents.base import AgentBackend, AgentReply, HistoryTurn, SessionHandle
from jsa.db import repo
from jsa.db.models import (
    FollowUp,
    Job,
    JobState,
    Message,
    RevisionRequest,
    Stage,
)
from jsa.events.bus import bus
from jsa.events.schema import (
    FollowUpNeededEvent,
    LogEvent,
    StageCompleteEvent,
    StatusChangedEvent,
    event_to_dict,
)
from jsa.pipeline.checkpoints import checkpoint
from jsa.prompts import loader

logger = logging.getLogger(__name__)


class PausedForInput(Exception):
    """Raised when run_stage parks a job to awaiting_input.

    The job's state has already been checkpointed before this is raised.
    The orchestrator catches this silently — it is control flow, not an error.
    """


async def run_stage(
    job: Job,
    backend: AgentBackend,
    stage: Stage,
    session: AsyncSession,
) -> None:
    """Run one pipeline stage to completion or park.

    Handles:
    - Fresh sessions: cv_adjust (from pending) and cover_letter (from cv_done)
    - Resumed sessions: awaiting_input resume (send answer) — cv_adjust, cover_letter, and revision stages
    - Revision sessions: revising_cv and revising_cl (fresh revision from review state)

    On FINAL: writes Document + Messages + transitions to next state atomically.
    On NEED_INPUT: writes FollowUp + Messages + transitions to awaiting_input, raises PausedForInput.
    """
    system_prompt = _get_system_prompt(stage)

    await bus.publish(
        event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Starting stage: {stage.value}"))
    )

    if stage in (Stage.revising_cv, Stage.revising_cl):
        original_stage = Stage.cv_adjust if stage == Stage.revising_cv else Stage.cover_letter
        revision_session_id = job.cv_session_id if stage == Stage.revising_cv else job.cl_session_id
        if revision_session_id is None:
            raise ValueError(
                f"Cannot resume revision for {stage.value}: per-stage session ID was not "
                f"recorded (job predates BF-9 fix). Reset the job to re-run from scratch."
            )

        # Fetch the unconsumed RevisionRequest — needed for its instruction (fresh path)
        # and its created_at (discriminator).
        rev_req = await _get_revision_request(session, job.id)

        # Discriminate fresh revision vs resume after awaiting_input.
        #
        # The spec's original discriminator (_load_history non-empty) is insufficient
        # because Message rows from prior completed revisions persist under the same
        # stage enum value. The correct discriminator is:
        #
        #   Resume iff a FollowUp for this revision stage was answered AFTER the
        #   current (unconsumed) RevisionRequest was created.
        #
        # This handles all cases correctly:
        #   - Fresh 1st revision: no FollowUp at all → fresh
        #   - Mid-revision park then resume: FollowUp.answered_at > RevReq.created_at → resume
        #   - Fresh 2nd revision: any old FollowUp.answered_at < RevReq.created_at → fresh
        #   - 2nd revision parks then resumes: new FollowUp.answered_at > new RevReq.created_at → resume
        is_resume = await _is_revision_resume(session, job.id, stage, rev_req.created_at)

        if is_resume:
            # Resume: the user answered a follow-up question mid-revision.
            # The CLI session already has the full context; just send the answer.
            # For AnthropicAPIBackend (history-based), pass combined history so
            # the model has the original cover-letter/CV context AND the revision turns.
            revision_turns = await _load_history(session, job.id, stage)
            answer_text = await _get_latest_answer(session, job.id, stage)
            original_history = await _load_history(session, job.id, original_stage)
            combined_history = original_history + revision_turns
            handle = await backend.restore_session(system_prompt, combined_history, revision_session_id)
            reply = await backend.send_message(handle, answer_text)
            accumulated_messages = [
                {"role": "user", "content": answer_text},
                {"role": "assistant", "content": reply.raw},
            ]
        else:
            # Fresh revision: restore the original stage's session and send the
            # revision instruction.
            history = await _load_history(session, job.id, original_stage)
            instruction = rev_req.instruction
            handle = await backend.restore_session(system_prompt, history, revision_session_id)
            reply = await backend.send_message(handle, instruction)
            accumulated_messages = [
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": reply.raw},
            ]
    elif stage in (Stage.cv_adjust, Stage.cover_letter):
        # Determine fresh vs resume by checking whether Message rows exist for
        # this job+stage.  The orchestrator already transitioned the job to
        # `running` before calling us, so we cannot discriminate on job.state.
        history = await _load_history(session, job.id, stage)
        if history:
            # Resume after awaiting_input — send the user's answer as the next turn.
            answer_text = await _get_latest_answer(session, job.id, stage)
            handle = await backend.restore_session(system_prompt, history, job.session_external_id)
            reply = await backend.send_message(handle, answer_text)
            # Only the new turns are new; prior messages already persisted.
            accumulated_messages = [
                {"role": "user", "content": answer_text},
                {"role": "assistant", "content": reply.raw},
            ]
        else:
            # Fresh session — run research pre-step (claude-cli only; best-effort)
            brief = await _gather_research(job, backend, stage)
            initial_user_msg = _build_initial_user_msg(job, brief)
            handle, reply = await backend.start_session(system_prompt, initial_user_msg)
            # Accumulate all messages for this session (system, user, assistant reply)
            accumulated_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": initial_user_msg},
                {"role": "assistant", "content": reply.raw},
            ]
    else:
        raise ValueError(f"Unexpected stage: {stage}")

    # Persist session_external_id while we have the handle in case we need to park
    job.session_external_id = handle.external_id

    # Keep per-stage session IDs so revision can resume the correct conversation.
    # Only set for the primary stages; do NOT overwrite during revising_* branches
    # (revisions continue the original session, so the UUID stays the same).
    if stage == Stage.cv_adjust:
        job.cv_session_id = handle.external_id
    elif stage == Stage.cover_letter:
        job.cl_session_id = handle.external_id

    # Handle the reply
    if reply.kind == "needs_input":
        await _handle_needs_input(
            session=session,
            job=job,
            handle=handle,
            stage=stage,
            reply=reply,
            accumulated_messages=accumulated_messages,
        )
        # Fetch the follow-up row we just inserted so we can include its ID
        # in the WS event.  The checkpoint above already committed, so the row
        # is visible to the same session.
        fu_result = await session.execute(
            select(FollowUp).where(
                FollowUp.job_id == job.id,
                FollowUp.stage == stage,
                FollowUp.answered_at.is_(None),
            )
        )
        fu = fu_result.scalar_one()
        await bus.publish(
            event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Stage {stage.value}: NEED_INPUT — agent is asking for input"))
        )
        await bus.publish(
            event_to_dict(
                FollowUpNeededEvent(
                    job_id=job.id,
                    follow_up_id=fu.id,
                    question=reply.question or "",
                    stage=stage.value,
                )
            )
        )
        raise PausedForInput()

    # reply.kind == "final"
    await _handle_final(
        session=session,
        job=job,
        backend=backend,
        handle=handle,
        stage=stage,
        reply=reply,
        accumulated_messages=accumulated_messages,
    )

    await bus.publish(
        event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Stage {stage.value}: FINAL received"))
    )

    # Publish stage-completion events after a successful FINAL checkpoint.
    # `job.state` has been mutated by transition() inside _handle_final.
    if stage == Stage.cv_adjust:
        await bus.publish(
            event_to_dict(StageCompleteEvent(job_id=job.id, stage="cv_adjust"))
        )
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=JobState.cv_done.value,
                )
            )
        )
    elif stage == Stage.cover_letter:
        await bus.publish(
            event_to_dict(StageCompleteEvent(job_id=job.id, stage="cover_letter"))
        )
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=JobState.review.value,
                )
            )
        )
    elif stage in (Stage.revising_cv, Stage.revising_cl):
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=JobState.review.value,
                )
            )
        )


async def _handle_needs_input(
    *,
    session: AsyncSession,
    job: Job,
    handle: SessionHandle,
    stage: Stage,
    reply: AgentReply,
    accumulated_messages: list[dict],
) -> None:
    """Park the job to awaiting_input, write FollowUp + messages atomically."""
    # Build a display question that includes any pre-sentinel context
    # (e.g. the Intel Brief Claude wrote before <<<NEED_INPUT>>>).
    sentinel_marker = "<<<NEED_INPUT>>>"
    raw_text = reply.raw
    sentinel_pos = raw_text.find(sentinel_marker)
    if sentinel_pos > 0:
        context = raw_text[:sentinel_pos].strip()
        display_question = f"{context}\n\n---\n\n{reply.question}" if context else reply.question
    else:
        display_question = reply.question

    follow_up_data = {
        "stage": stage,
        "question": display_question,
    }
    await checkpoint(
        session,
        job,
        JobState.awaiting_input,
        stage,  # preserve current_stage
        messages=accumulated_messages,
        follow_up=follow_up_data,
    )


async def _handle_final(
    *,
    session: AsyncSession,
    job: Job,
    backend: AgentBackend,
    handle: SessionHandle,
    stage: Stage,
    reply: AgentReply,
    accumulated_messages: list[dict],
) -> None:
    """Finalize the stage: compute next state, version document, write checkpoint."""
    # A successful FINAL means any soft retry worked — reset the retry counter.
    # checkpoint() calls session.add(job) + commit, so this persists atomically.
    job.retry_count = 0

    # Determine document stage (revision docs stored under original stage)
    if stage == Stage.revising_cv:
        doc_stage = Stage.cv_adjust
    elif stage == Stage.revising_cl:
        doc_stage = Stage.cover_letter
    else:
        doc_stage = stage

    # Compute next version: count existing documents for this job+doc_stage, +1
    existing_docs = await repo.get_documents(session, job.id, doc_stage)
    next_version = len(existing_docs) + 1

    document_data = {
        "stage": doc_stage,
        "version": next_version,
        "markdown": reply.content,
    }

    if stage == Stage.cv_adjust:
        # cv_adjust → cv_done
        await checkpoint(
            session,
            job,
            JobState.cv_done,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
    elif stage == Stage.cover_letter:
        # cover_letter → review directly: write messages + document in a single transaction.
        # cl_done is not an observable intermediate state in normal flow; it exists only
        # as a crash-recovery landing state (the startup sweep uses it when it finds a
        # cover_letter Document on a job that crashed mid-run).
        await checkpoint(
            session,
            job,
            JobState.review,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
    elif stage in (Stage.revising_cv, Stage.revising_cl):
        # Mark the RevisionRequest consumed (within same transaction as the checkpoint commit)
        await session.execute(
            update(RevisionRequest)
            .where(
                RevisionRequest.job_id == job.id,
                RevisionRequest.consumed_at.is_(None),
            )
            .values(consumed_at=datetime.utcnow())
        )
        # Revision complete → back to review
        await checkpoint(
            session,
            job,
            JobState.review,
            None,
            messages=accumulated_messages,
            document=document_data,
        )

    await backend.end_session(handle)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_system_prompt(stage: Stage) -> str:
    """Return the system prompt for the given stage."""
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        return loader.read_prompt("cv_adjust")
    else:  # cover_letter or revising_cl
        return loader.read_prompt("cover_letter")


def _build_initial_user_msg(job: Job, brief: str) -> str:
    """Build the initial user message for a fresh session.

    ``brief`` is either a populated research block ([INTEL_BRIEF] or [COMPANY_BRIEF])
    or the NONE placeholder produced by ``_research_placeholder``.  It is always
    injected first so the main agent sees it immediately and the resumed Message
    history replays it verbatim (research runs at most once per stage).
    """
    return (
        f"{brief}\n\n"
        f"CV TEXT:\n{job.cv_text}\n\n"
        f"JOB DESCRIPTION:\n{job.jd}\n\n"
        f"TIER: {job.tier}"
    )


async def _load_history(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
) -> list[HistoryTurn]:
    """Load Message rows for the given job+stage as HistoryTurn list.

    Excludes system messages — only user and assistant turns are included,
    ordered by id (creation order).
    """
    stmt = (
        select(Message)
        .where(
            Message.job_id == job_id,
            Message.stage == stage,
            Message.role.in_(["user", "assistant"]),
        )
        .order_by(Message.id.asc())
    )
    result = await session.execute(stmt)
    messages = result.scalars().all()
    return [HistoryTurn(role=m.role, content=m.content) for m in messages]  # type: ignore[arg-type]



async def _get_latest_answer(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
) -> str:
    """Return the answer from the most recently answered FollowUp for this job+stage."""
    stmt = (
        select(FollowUp)
        .where(
            FollowUp.job_id == job_id,
            FollowUp.stage == stage,
            FollowUp.answered_at.is_not(None),
        )
        .order_by(FollowUp.answered_at.desc())
    )
    result = await session.execute(stmt)
    fu = result.scalars().first()
    if fu is None:
        raise ValueError(
            f"No answered FollowUp found for job {job_id}, stage {stage}"
        )
    if fu.answer is None:
        raise ValueError(
            f"FollowUp for job {job_id}, stage {stage} has no answer text"
        )
    return fu.answer


async def _get_revision_request(
    session: AsyncSession,
    job_id: str,
) -> RevisionRequest:
    """Return the unconsumed RevisionRequest for this job, raising if absent."""
    stmt = (
        select(RevisionRequest)
        .where(
            RevisionRequest.job_id == job_id,
            RevisionRequest.consumed_at.is_(None),
        )
    )
    result = await session.execute(stmt)
    rev = result.scalar_one_or_none()
    if rev is None:
        raise ValueError(f"No unconsumed RevisionRequest found for job {job_id}")
    return rev


async def _is_revision_resume(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
    revision_created_at: datetime,
) -> bool:
    """Return True if this revision invocation is a resume after a mid-revision park.

    A revision is a resume iff a FollowUp for this revision stage was answered
    AFTER the current (unconsumed) RevisionRequest was created. This correctly
    handles multiple sequential revisions: old FollowUp rows from completed
    prior revisions have answered_at < current RevisionRequest.created_at and
    therefore do not trigger the resume path.
    """
    stmt = (
        select(FollowUp)
        .where(
            FollowUp.job_id == job_id,
            FollowUp.stage == stage,
            FollowUp.answered_at.is_not(None),
            FollowUp.answered_at > revision_created_at,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


def _research_placeholder(stage: Stage) -> str:
    """Return a NONE placeholder brief for non-claude-cli backends or research failures."""
    tag = "INTEL_BRIEF" if stage in (Stage.cv_adjust, Stage.revising_cv) else "COMPANY_BRIEF"
    return (
        f"[{tag}]\n"
        f"NONE — no automated research available for this backend. "
        f"Ask the user for company context; do NOT attempt to browse the web.\n"
        f"[/{tag}]"
    )


def _research_spec(job: Job, stage: Stage) -> tuple[str, str, str]:
    """Return (agent_name, query, open_tag) for the given stage.

    Only ``cv_adjust`` and ``cover_letter`` are valid inputs — ``_gather_research``
    is never called for revision stages, but this guard makes that contract explicit.
    """
    if stage == Stage.cv_adjust:
        agent_name = "cv-research"
        open_tag = "[INTEL_BRIEF]"
        query = (
            f"Company: {job.company}\n"
            f"Role: {job.role}\n"
            f"Job posting link: {job.link}\n"
            f"Job description:\n{job.jd}"
        )
    elif stage == Stage.cover_letter:
        agent_name = "cl-research"
        open_tag = "[COMPANY_BRIEF]"
        query = (
            f"Company: {job.company}\n"
            f"Role: {job.role}\n"
            f"Job posting link: {job.link}"
        )
    else:
        raise ValueError(
            f"_research_spec called with unexpected stage {stage!r}; "
            "only cv_adjust and cover_letter are supported."
        )
    return agent_name, query, open_tag


async def _gather_research(job: Job, backend: AgentBackend, stage: Stage) -> str:
    """Return a research brief block to inject into the initial user message.

    claude-cli only: invokes the cv-research / cl-research subagent via
    ``ClaudeCliBackend.run_research``.  Any other backend, or any research
    failure, yields the NONE placeholder so the main prompt's single code path
    falls back to asking the user directly.  Research is best-effort and never
    fails the job.
    """
    from jsa.agents.claude_cli import ClaudeCliBackend  # local import avoids cycle

    if not isinstance(backend, ClaudeCliBackend):
        return _research_placeholder(stage)

    agent_name, query, open_tag = _research_spec(job, stage)
    try:
        await bus.publish(
            event_to_dict(LogEvent(
                job_id=job.id, level="info", text=f"Researching ({agent_name})…"
            ))
        )
        text = await backend.run_research(agent_name, query)
        text = text.strip()
        # Trust the agent's own tags if present; otherwise fall back to placeholder.
        return text if open_tag in text else _research_placeholder(stage)
    except Exception as exc:  # research is best-effort; never fail the job
        logger.warning(
            "research failed for job %s (%s): %s", job.id, agent_name, exc
        )
        await bus.publish(
            event_to_dict(LogEvent(
                job_id=job.id, level="warn",
                text="Research unavailable; proceeding without brief."
            ))
        )
        return _research_placeholder(stage)
