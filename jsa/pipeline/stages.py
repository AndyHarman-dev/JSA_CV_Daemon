"""Pipeline stage runners: run_stage.

Handles both fresh sessions (pending/cv_done) and resumed sessions
(awaiting_input, revising_cv, revising_cl).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend, AgentReply, HistoryTurn, SessionHandle
from jsa.db import repo
from jsa.db.models import (
    Document,
    FollowUp,
    Job,
    JobState,
    Message,
    RevisionRequest,
    Stage,
)
from jsa.pipeline.checkpoints import checkpoint
from jsa.prompts import loader

if TYPE_CHECKING:
    pass

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
    - Resumed sessions: awaiting_input resume (send answer)
    - Revision sessions: revising_cv and revising_cl

    On FINAL: writes Document + Messages + transitions to next state atomically.
    On NEED_INPUT: writes FollowUp + Messages + transitions to awaiting_input, raises PausedForInput.
    """
    system_prompt = _get_system_prompt(stage)

    if stage in (Stage.cv_adjust, Stage.cover_letter):
        # Fresh session path
        initial_user_msg = _build_initial_user_msg(job)
        handle, reply = await backend.start_session(system_prompt, initial_user_msg)
        # Accumulate messages for this session (system, user, then assistant reply)
        accumulated_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_user_msg},
            {"role": "assistant", "content": reply.raw},
        ]
    elif stage in (Stage.revising_cv, Stage.revising_cl):
        # Revision path: restore session with history from the original stage
        original_stage = Stage.cv_adjust if stage == Stage.revising_cv else Stage.cover_letter
        history = await _load_history(session, job.id, original_stage)
        instruction = await _get_revision_instruction(session, job.id)
        handle = await backend.restore_session(system_prompt, history, job.session_external_id)
        reply = await backend.send_message(handle, instruction)
        # Only the new turns (user instruction + assistant reply) are new
        accumulated_messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": reply.raw},
        ]
    elif job.state == JobState.awaiting_input:
        # Resume after user answered a follow-up question
        history = await _load_history(session, job.id, job.current_stage)
        answer_text = await _get_latest_answer(session, job.id, job.current_stage)
        handle = await backend.restore_session(system_prompt, history, job.session_external_id)
        reply = await backend.send_message(handle, answer_text)
        # Only the new turns are new
        accumulated_messages = [
            {"role": "user", "content": answer_text},
            {"role": "assistant", "content": reply.raw},
        ]
    else:
        raise ValueError(f"Unexpected stage/state combination: stage={stage}, state={job.state}")

    # Persist session_external_id while we have the handle in case we need to park
    job.session_external_id = handle.external_id

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
    follow_up_data = {
        "stage": stage,
        "question": reply.question,
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
        # cover_letter → cl_done (with document+messages), then cl_done → review (bare)
        await checkpoint(
            session,
            job,
            JobState.cl_done,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
        # Immediately advance to review — cl_done is not a resting state
        await checkpoint(
            session,
            job,
            JobState.review,
            None,
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


def _build_initial_user_msg(job: Job) -> str:
    """Build the initial user message for a fresh session."""
    return (
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


async def _get_revision_instruction(
    session: AsyncSession,
    job_id: str,
) -> str:
    """Return the instruction text from the unconsumed RevisionRequest for this job."""
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
    return rev.instruction


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
