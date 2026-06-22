"""Pipeline stage runners: run_stage.

Handles both fresh sessions (pending/cv_done) and resumed sessions
(awaiting_input, revising_cv, revising_cl).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend, AgentReply, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError
from jsa.render.registry import renderer_for
from jsa.db import repo
from jsa.util import slugify as _slugify
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


async def _render_for_review(session: AsyncSession, job: Job, output_dir: Path) -> None:
    """Render both PDF and DOCX for all review documents and persist paths."""
    cv_docs = await repo.get_documents(session, job.id, Stage.cv_adjust)
    cl_docs = await repo.get_documents(session, job.id, Stage.cover_letter)
    if not cv_docs or not cl_docs:
        return

    cv_doc = cv_docs[0]  # version-desc, so [0] is latest
    cl_doc = cl_docs[0]

    slug = f"{_slugify(job.company)}_{_slugify(job.role)}_{job.id[:8]}"
    out = output_dir / slug
    out.mkdir(parents=True, exist_ok=True)

    cv_pdf  = out / "cv.pdf"
    cv_docx = out / "cv.docx"
    cl_pdf  = out / "cover_letter.pdf"
    cl_docx = out / "cover_letter.docx"

    pdf_r  = renderer_for("weasyprint")
    docx_r = renderer_for("docx")

    await asyncio.gather(
        pdf_r.render(cv_doc.markdown, cv_pdf),
        docx_r.render(cv_doc.markdown, cv_docx),
        pdf_r.render(cl_doc.markdown, cl_pdf),
        docx_r.render(cl_doc.markdown, cl_docx),
    )

    cv_doc.pdf_path  = str(cv_pdf)
    cv_doc.docx_path = str(cv_docx)
    cl_doc.pdf_path  = str(cl_pdf)
    cl_doc.docx_path = str(cl_docx)
    session.add(cv_doc)
    session.add(cl_doc)
    await session.commit()


def _validate_cv_content(content: str) -> None:
    """Validate that a cv_adjust FINAL block contains actual CV content.

    Called before any DB write for Stage.cv_adjust and Stage.revising_cv.
    Raises ValueError (→ job marked failed, user can retry) when the content
    clearly is not a CV:
    - Too short to be any reasonable CV
    - Missing all markdown structural markers that a CV would have

    We check structural markers (headings, separators) rather than specific
    content patterns, to avoid fragile keyword matching.
    """
    MIN_CV_CHARS = 300

    if len(content) < MIN_CV_CHARS:
        raise ValueError(
            f"cv_adjust produced content that is too short ({len(content)} chars; "
            f"minimum is {MIN_CV_CHARS}). The agent may have emitted a summary or "
            "description instead of the adjusted CV. Retry the job to re-run."
        )

    # A valid CV must contain at least one markdown structural marker.
    # Cover letters and plain-text change-log summaries typically have none.
    has_heading = bool(re.search(r"^#{1,3} ", content, re.MULTILINE))
    has_separator = bool(re.search(r"^---\s*$", content, re.MULTILINE))
    has_bold_name = bool(re.search(r"^\*\*[A-Z]", content, re.MULTILINE))

    if not (has_heading or has_separator or has_bold_name):
        raise ValueError(
            "cv_adjust produced content without expected CV structure "
            "(no markdown headings, '---' separators, or bold name header). "
            "The agent may have emitted a cover letter or plain-text summary "
            "instead of the adjusted CV. Retry the job to re-run."
        )


_BAD_CL_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)^cover letter\s+(drafted|written|delivered|prepared)\s+for\b", "starts with a summary header"),
    (r"(?i)^\(cover letter\b", "starts with a parenthetical reference to the letter"),
    (r"(?i)\bdelivered above\b", "references a previous turn"),
    (r"(?i)\bawaiting\s+(any\s+)?(revision|change|feedback|request)", "contains an awaiting-revision meta-comment"),
    (r"(?i)^centerpiece\s*:", "starts with a change-description 'Centerpiece:' marker"),
]


def _validate_cl_content(content: str) -> None:
    """Validate that a cover_letter FINAL block contains actual letter content, not meta-commentary.

    Parallel to _validate_cv_content for the CV stages. Raises ValueError if the model
    emitted a summary, change-log, or reference comment instead of the letter text.
    """
    MIN_CL_CHARS = 150

    if len(content) < MIN_CL_CHARS:
        raise ValueError(
            f"cover_letter produced content that is too short ({len(content)} chars; "
            f"minimum is {MIN_CL_CHARS}). The agent may have emitted a summary or "
            "reference comment instead of the cover letter. Retry the job to re-run."
        )

    for pattern, description in _BAD_CL_PATTERNS:
        if re.search(pattern, content.strip(), re.MULTILINE):
            raise ValueError(
                f"cover_letter produced meta-commentary instead of actual letter content "
                f"({description}). The agent may have emitted a summary or change-log "
                "instead of the letter text. Retry the job to re-run."
            )


async def run_stage(
    job: Job,
    backend: AgentBackend,
    stage: Stage,
    session: AsyncSession,
    output_dir: Path | None = None,
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

    if stage == Stage.fit_assessment:
        # One-shot pre-check: no resume path, no NEED_INPUT, no research. Handled
        # entirely here (FIT → fit_done, anything else → unfit) and returns early.
        await _run_fit_assessment(job, backend, session, system_prompt)
        return

    if stage in (Stage.revising_cv, Stage.revising_cl):
        original_stage = Stage.cv_adjust if stage == Stage.revising_cv else Stage.cover_letter
        revision_session_id = job.cv_session_id if stage == Stage.revising_cv else job.cl_session_id
        # revision_session_id may be None after a backend switch (BF-19): the new backend
        # will restore from history only (AnthropicAPIBackend ignores external_id; CLI
        # backends receive None and start a new session backed by the history array).
        # Only raise if both session ID and Message history are absent, which indicates
        # a job that predates BF-9 (not a backend switch).
        if revision_session_id is None:
            original_stage_check = Stage.cv_adjust if stage == Stage.revising_cv else Stage.cover_letter
            history_check = await _load_history(session, job.id, original_stage_check)
            if not history_check:
                raise ValueError(
                    f"Cannot resume revision for {stage.value}: per-stage session ID was not "
                    f"recorded and no history available (job predates BF-9 fix). "
                    f"Reset the job to re-run from scratch."
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
        output_dir=output_dir,
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


# ---------------------------------------------------------------------------
# Fit assessment (one-shot pre-check before any CV work)
# ---------------------------------------------------------------------------

# Shown in the modal when the agent's verdict cannot be parsed (fail-to-modal).
_FIT_FALLBACK_REASON = (
    "The fit assessment did not return a clear verdict. Review this job manually "
    "before continuing."
)


def _build_fit_user_msg(job: Job) -> str:
    """Build the (single) user message for the fit-assessment stage.

    Deliberately minimal — no research brief — so the pre-check stays cheap.
    """
    return (
        f"COMPANY: {job.company}\n"
        f"ROLE: {job.role}\n\n"
        f"CV TEXT:\n{job.cv_text}\n\n"
        f"JOB DESCRIPTION:\n{job.jd}"
    )


def _parse_fit_verdict(reply: AgentReply) -> tuple[bool, str | None]:
    """Parse a fit-assessment reply into ``(is_fit, reason)``.

    Contract: the FINAL payload's first line carries the verdict ``FIT`` or ``UNFIT``;
    the rest is the reason. We match the verdict as a substring of the (uppercased)
    first line so common decoration is tolerated — ``**FIT**``, ``Verdict: FIT``,
    ``## UNFIT``, ``FIT ✅`` all classify correctly. ``UNFIT`` is checked first because
    it contains ``FIT``.

    Fail-to-modal — anything that is not a clear ``FIT`` returns ``is_fit=False`` with a
    best-effort reason for the modal:
    - ``UNFIT`` → the agent's reason (or a fallback if none given)
    - a ``needs_input`` reply → the agent's question text
    - no recognizable verdict → the raw content (so nothing is hidden from the user)
    """
    if reply.kind == "needs_input":
        return False, (reply.question or "").strip() or _FIT_FALLBACK_REASON

    content = (reply.content or "").strip()
    lines = content.split("\n", 1)
    first_line_upper = lines[0].upper()
    rest = lines[1].strip() if len(lines) > 1 else ""

    if "UNFIT" in first_line_upper:
        # Reason = following lines, else the first line minus everything up to "UNFIT".
        reason = rest or re.sub(r"(?is).*unfit[\s:.\-—*]*", "", lines[0]).strip()
        return False, reason or _FIT_FALLBACK_REASON

    if "FIT" in first_line_upper:
        return True, None

    # No recognizable verdict → fail to modal, surfacing whatever the agent produced.
    return False, content or _FIT_FALLBACK_REASON


async def _run_fit_assessment(
    job: Job,
    backend: AgentBackend,
    session: AsyncSession,
    system_prompt: str,
) -> None:
    """Run the one-shot fit-assessment stage and checkpoint the outcome.

    Always a fresh session expecting a single FINAL with a FIT/UNFIT verdict; there
    is no resume / awaiting_input path for this stage. FIT → ``fit_done`` (pipeline
    continues to cv_adjust). Anything else → ``unfit`` (parked), storing the agent's
    reason in ``job.fit_reason`` for the frontend modal.
    """
    initial_user_msg = _build_fit_user_msg(job)
    job.retry_count = 0

    try:
        handle, reply = await backend.start_session(system_prompt, initial_user_msg)
    except ProtocolError:
        # A malformed / sentinel-less reply is "unparseable" → fail to the modal
        # (closed), consistent with the verdict contract, rather than failing the job.
        job.fit_reason = _FIT_FALLBACK_REASON
        await checkpoint(session, job, JobState.unfit, None)
        await _publish_fit_outcome(job, is_fit=False)
        return

    accumulated_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_msg},
        {"role": "assistant", "content": reply.raw},
    ]
    job.session_external_id = handle.external_id

    is_fit, reason = _parse_fit_verdict(reply)
    target_state = JobState.fit_done if is_fit else JobState.unfit
    job.fit_reason = None if is_fit else reason

    await checkpoint(
        session,
        job,
        target_state,
        None,
        messages=accumulated_messages,
    )
    await backend.end_session(handle)
    await _publish_fit_outcome(job, is_fit=is_fit)


async def _publish_fit_outcome(job: Job, *, is_fit: bool) -> None:
    """Publish the log + stage-complete + status-changed events for a fit outcome."""
    target_state = JobState.fit_done if is_fit else JobState.unfit
    await bus.publish(
        event_to_dict(LogEvent(
            job_id=job.id, level="info",
            text=f"Stage fit_assessment: {'FIT' if is_fit else 'UNFIT'} → {target_state.value}",
        ))
    )
    await bus.publish(
        event_to_dict(StageCompleteEvent(job_id=job.id, stage="fit_assessment"))
    )
    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job.id,
                from_state=JobState.running.value,
                to_state=target_state.value,
            )
        )
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
    output_dir: Path | None = None,
) -> None:
    """Finalize the stage: compute next state, version document, write checkpoint."""
    # A successful FINAL means any soft retry worked — reset the retry counter.
    # checkpoint() calls session.add(job) + commit, so this persists atomically.
    job.retry_count = 0

    # Validate cv_adjust output before writing anything to the DB.
    # If the model emitted a cover letter or a change-log instead of a CV,
    # this raises ValueError → propagates to _run_one → job marked failed.
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        _validate_cv_content(reply.content)
    if stage in (Stage.cover_letter, Stage.revising_cl):
        _validate_cl_content(reply.content)

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
        if output_dir is not None:
            await _render_for_review(session, job, output_dir)
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
        if output_dir is not None:
            await _render_for_review(session, job, output_dir)

    await backend.end_session(handle)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_system_prompt(stage: Stage) -> str:
    """Return the system prompt for the given stage."""
    if stage == Stage.fit_assessment:
        return loader.read_prompt("fit_assessment")
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

    Any backend that implements ``run_research`` (e.g. ClaudeCliBackend,
    GoogleCliBackend) will have it invoked here.  Any backend without
    ``run_research``, or any research failure, yields the NONE placeholder so
    the main prompt's single code path falls back to asking the user directly.
    Research is best-effort and never fails the job.
    """
    if not hasattr(backend, "run_research"):
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
        if open_tag in text:
            return text
        logger.warning(
            "run_research for job %s (%s) returned output missing expected tag %r; "
            "falling back to research placeholder",
            job.id, agent_name, open_tag,
        )
        return _research_placeholder(stage)
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
