"""Pipeline stage runners: run_stage.

Handles both fresh sessions (pending/cv_done) and resumed sessions
(awaiting_input, revising_cv, revising_cl).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError
from jsa.render.registry import renderer_for
from jsa.render.serialize import cover_letter_to_markdown, cv_to_markdown
from jsa.schema import CVDocument, CoverLetter, cv_has_summary
from jsa.db import repo
from jsa.util import slugify as _slugify
from jsa.db.models import (
    Document,
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
from jsa.i18n.languages import language_name
from jsa.pipeline.checkpoints import checkpoint
from jsa.prompts import loader
from jsa.store import cv_structure as cv_structure_store
from jsa.store import preferences as preferences_store

logger = logging.getLogger(__name__)


class PausedForInput(Exception):
    """Raised when run_stage parks a job to awaiting_input.

    The job's state has already been checkpointed before this is raised.
    The orchestrator catches this silently — it is control flow, not an error.
    """


class StaleJobResult(Exception):
    """Raised when the job's DB state changed (dismiss/cancel/delete) while the
    agent was working, so the just-produced reply is stale and must be
    discarded rather than checkpointed.

    The orchestrator worker holds its Job ORM object in memory for the whole,
    potentially slow, agent turn. If another request (e.g. dismiss) commits a
    state change on a separate session in the meantime, writing this reply's
    checkpoint would resurrect the job — see checkpoint()'s in-memory-only
    transition() guard in jsa/db/repo.py. No rows are written when this fires;
    the orchestrator catches it silently, like PausedForInput.
    """


_RENDER_FILENAMES = {Stage.cv_adjust: "cv", Stage.cover_letter: "cover_letter"}


async def _render(
    session: AsyncSession,
    job: Job,
    output_dir: Path,
    stages: tuple[Stage, ...] = (Stage.cv_adjust, Stage.cover_letter),
) -> None:
    """Render PDF+DOCX for the latest Document of each given stage, persisting paths.

    A stage with no Document yet is skipped rather than erroring — e.g. at the CV gate
    only ``cv_adjust`` has one; at final review both do.
    """
    docs: dict[Stage, Document] = {}
    for st in stages:
        st_docs = await repo.get_documents(session, job.id, st)
        if st_docs:
            docs[st] = st_docs[0]  # version-desc, so [0] is latest
    if not docs:
        return

    slug = f"{_slugify(job.company)}_{_slugify(job.role)}_{job.id[:8]}"
    out = output_dir / slug
    out.mkdir(parents=True, exist_ok=True)

    pdf_r  = renderer_for("weasyprint")
    docx_r = renderer_for("docx")

    render_tasks = []
    doc_paths: dict[Stage, tuple[Path, Path]] = {}
    for st, doc in docs.items():
        name = _RENDER_FILENAMES[st]
        pdf_path = out / f"{name}.pdf"
        docx_path = out / f"{name}.docx"
        render_tasks.append(pdf_r.render(doc.markdown, pdf_path))
        render_tasks.append(docx_r.render(doc.markdown, docx_path))
        doc_paths[st] = (pdf_path, docx_path)

    await asyncio.gather(*render_tasks)

    for st, doc in docs.items():
        pdf_path, docx_path = doc_paths[st]
        doc.pdf_path = str(pdf_path)
        doc.docx_path = str(docx_path)
        session.add(doc)
    await session.commit()


async def _render_for_review(session: AsyncSession, job: Job, output_dir: Path) -> None:
    """Render both PDF and DOCX for the CV and cover-letter documents and persist paths."""
    await _render(session, job, output_dir, stages=(Stage.cv_adjust, Stage.cover_letter))


async def _render_cv(session: AsyncSession, job: Job, output_dir: Path) -> None:
    """Render PDF and DOCX for the cv_adjust document only — the CV gate."""
    await _render(session, job, output_dir, stages=(Stage.cv_adjust,))


class FinalContentError(ValueError):
    """Raised when a FINAL payload fails stage content validation.

    Subclasses ValueError so the existing _run_one handler still marks the job
    ``failed`` when correction attempts are exhausted. The self-heal loop catches
    it specifically to re-prompt the agent before giving up.
    """


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding ```json ...``` (or plain ```) fence if the model added one."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _capture_failed_payload(label: str, job_id: str | None, content: str, reason: str) -> None:
    """Best-effort dump of a FINAL payload that failed validation, for offline debugging.

    Pydantic truncates ``input_value`` in its error text, so the ``error`` column never
    holds the full payload. This writes the raw pre-validation FINAL to
    ``~/.jsa/debug/`` so a failure can be reproduced and the schema/serializer iterated
    offline. Never raises — debugging aid only.
    """
    if not job_id:
        return
    import os
    if "PYTEST_CURRENT_TEST" in os.environ:  # don't litter ~/.jsa during the test suite
        return
    try:
        debug_dir = Path.home() / ".jsa" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        path = debug_dir / f"{label}_{job_id}_{ts}.txt"
        path.write_text(f"# reason: {reason}\n\n{content}", encoding="utf-8")
    except Exception:  # pragma: no cover - diagnostics must never break the pipeline
        logger.debug("failed to capture FINAL payload for %s/%s", label, job_id, exc_info=True)


def _parse_structured(
    content: str,
    model: type[BaseModel],
    label: str,
    job_id: str | None = None,
    language: str = "en",
) -> BaseModel:
    """Parse a FINAL payload as JSON and validate it against ``model``.

    Raises ``FinalContentError`` (→ self-heal re-prompt, then job ``failed``) on invalid
    JSON or schema violation; returns the validated model instance otherwise. This
    replaces the old regex heuristics: a change-log, summary, mixed CV/CL payload, or
    third-person description cannot satisfy the schema, so it is rejected *structurally*
    rather than by pattern-matching.

    ``language`` is passed through as validation context (``{"language": language}``);
    only ``CVDocument``'s content-kind guard reads it (to pick the right per-language
    letter-formula pattern) — other models ignore the unused context key harmlessly.
    """
    text = _strip_code_fence(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _capture_failed_payload(label, job_id, content, f"invalid JSON: {exc}")
        raise FinalContentError(
            f"{label} FINAL block was not valid JSON ({exc}). Re-emit ONLY a single JSON "
            "object conforming to the schema inside <<<FINAL>>>...<<<END>>>."
        ) from exc
    try:
        return model.model_validate(data, context={"language": language})
    except ValidationError as exc:
        _capture_failed_payload(label, job_id, content, f"schema violation: {exc}")
        # Build a concise, model-facing reason from the validators' own messages. Never use
        # str(exc) here: pydantic embeds the entire input_value (the whole CV payload) per
        # error, which would echo the document back at the model in the self-heal correction.
        reasons = "; ".join(
            e.get("msg", "").removeprefix("Value error, ") for e in exc.errors()
        ) or "the payload did not conform to the schema"
        raise FinalContentError(
            f"{label} JSON did not match the required schema: {reasons}. Re-emit a corrected "
            "JSON object inside <<<FINAL>>>...<<<END>>>."
        ) from exc


def _validate_final_content(
    stage: Stage, content: str, job: Job, language: str = "en"
) -> BaseModel | None:
    """Parse + validate a FINAL payload for the given stage.

    Returns the validated structured object (``CVDocument`` / ``CoverLetter``) for the
    document stages, or ``None`` for stages without structured output (e.g.
    fit_assessment). Raises ``FinalContentError`` on invalid JSON / schema violation.
    Single source of truth used by both the self-heal loop (to decide whether to
    re-prompt) and _handle_final (the authoritative gate before any DB write).

    ``language`` (default ``"en"``) is threaded through to ``_parse_structured`` for the
    ``CVDocument`` content-kind guard's per-language letter-formula matching.
    """
    job_id = job.id if job is not None else None
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        return _parse_structured(content, CVDocument, "cv_adjust", job_id, language)
    if stage in (Stage.cover_letter, Stage.revising_cl):
        return _parse_structured(content, CoverLetter, "cover_letter", job_id, language)
    return None


# How many times to re-prompt the same session when a FINAL block fails content
# validation before giving up and letting the job fail.
MAX_FINAL_CORRECTIONS = 2

_CV_CORRECTION = (
    "Your previous <<<FINAL>>> block was not a valid CV JSON object. Re-emit now: put ONLY "
    "a single JSON object conforming to the CV schema inside one <<<FINAL>>>...<<<END>>> "
    "block — a `contact` object (name plus email and/or phone) and an ordered `sections` "
    "array mirroring the base CV's sections. No Markdown, no prose, no change-log, no "
    "commentary inside the block, and do not wrap the JSON in code fences."
)
_CL_CORRECTION = (
    "Your previous <<<FINAL>>> block was not a valid cover-letter JSON object. Re-emit now: "
    "put ONLY a single JSON object conforming to the cover-letter schema inside one "
    "<<<FINAL>>>...<<<END>>> block — an optional `salutation`, a `paragraphs` array holding "
    "the letter's body paragraphs, and an optional `signoff`. No Markdown, no commentary, "
    "and do not wrap the JSON in code fences."
)

# Soft nudge (not a hard failure): the CV validated fine but has no summary/profile section.
# The user wants a summary to always appear; the model can always write one from the CV's own
# content, so we re-prompt for it — but a CV that still lacks one ships as a slightly-thin CV
# rather than failing the job (a missing summary is thinness, not corruption).
_CV_SUMMARY_NUDGE = (
    "Your CV JSON is valid but is missing a Summary section. Re-emit the SAME CV with one "
    "change: add a section named \"Summary\" to `sections`, with a `text` value holding a "
    "2–3 sentence professional summary tailored to this role and drawn from the candidate's "
    "own experience (do not invent facts). If a base CV structure was provided, insert it "
    "wherever that structure placed (or would place) a Summary; otherwise put it first. Keep "
    "everything else, including the order of every other section, identical. Emit the "
    "complete CV JSON object inside one <<<FINAL>>>...<<<END>>> block — no Markdown, no "
    "commentary, no code fences."
)


async def _self_heal_final(
    *,
    backend: AgentBackend,
    handle: SessionHandle,
    stage: Stage,
    job: Job,
    reply: AgentReply,
    accumulated_messages: list[dict],
    language: str = "en",
) -> tuple[AgentReply, list[dict]]:
    """Re-prompt the open session when a FINAL block fails content validation.

    Robustness layer for when the model ignores the prompt and emits a change-log,
    "task complete" summary, or file-write announcement instead of the artifact. On
    each failure we send a precise correction on the *same* session (preserving full
    context) and re-validate, up to MAX_FINAL_CORRECTIONS times.

    Returns the (possibly updated) reply and accumulated messages. The caller then
    dispatches normally: a recovered FINAL proceeds; a correction that turns into
    NEED_INPUT parks for the user; a still-invalid FINAL falls through to
    _handle_final, whose validation raises and fails the job (after auto-retries).
    """
    if stage not in (Stage.cv_adjust, Stage.revising_cv, Stage.cover_letter, Stage.revising_cl):
        return reply, accumulated_messages

    correction = (
        _CV_CORRECTION if stage in (Stage.cv_adjust, Stage.revising_cv) else _CL_CORRECTION
    )

    is_cv_stage = stage in (Stage.cv_adjust, Stage.revising_cv)

    async def _reprompt(message: str, label: str) -> None:
        nonlocal reply
        await bus.publish(
            event_to_dict(
                LogEvent(
                    job_id=job.id,
                    level="warning",
                    text=(
                        f"Stage {stage.value}: {label}; "
                        f"re-prompting agent (attempt {attempts}/{MAX_FINAL_CORRECTIONS})"
                    ),
                )
            )
        )
        reply = await backend.send_message(handle, message)
        accumulated_messages.append({"role": "user", "content": message})
        accumulated_messages.append({"role": "assistant", "content": reply.raw})

    attempts = 0
    while reply.kind == "final":
        try:
            obj = _validate_final_content(stage, reply.content, job, language)
        except FinalContentError as exc:
            if attempts >= MAX_FINAL_CORRECTIONS:
                break  # budget exhausted; _handle_final re-validates, raises, fails the job
            attempts += 1
            # Forward the concise validator feedback so the model knows *what* to fix.
            await _reprompt(
                f"{correction}\n\nValidation feedback on your last attempt: {exc}",
                f"FINAL failed validation ({exc})",
            )
            continue
        # Valid payload. Soft, non-fatal nudge: a CV with no summary section gets one
        # re-prompt to add it (if budget remains); an unfixed summary still ships.
        if is_cv_stage and isinstance(obj, CVDocument) and not cv_has_summary(obj) \
                and attempts < MAX_FINAL_CORRECTIONS:
            attempts += 1
            await _reprompt(_CV_SUMMARY_NUDGE, "valid CV missing a Summary section")
            continue
        return reply, accumulated_messages

    return reply, accumulated_messages


async def _read_base_structure(cv_structure_path: Path | None) -> CVDocument | None:
    """Read the standalone base-CV structure, tolerating a corrupt/invalid file.

    ``None`` covers both "no path given" and "file missing" (see
    ``cv_structure_store.read``'s own contract) as well as "file present but not valid
    JSON / doesn't pass the CVDocument schema" — a hand-edited or partially-written file.
    Orchestrator.run()'s gate normally keeps jobs pending until the file is loadable, but
    a job already past that gate could still race a concurrent edit; treating a corrupt
    read the same as "absent" here means that race degrades to a missing-CV prompt rather
    than crashing the job (caught by run_one's generic except → mark_failed).
    """
    if cv_structure_path is None:
        return None
    try:
        return await cv_structure_store.read(cv_structure_path)
    except (json.JSONDecodeError, ValidationError):
        logger.warning(
            "base-CV structure at %s is present but invalid — treating as absent",
            cv_structure_path,
        )
        return None


async def run_stage(
    job: Job,
    general_purpose_backend: AgentBackend,
    stage: Stage,
    session: AsyncSession,
    fit_backend: AgentBackend | None = None,
    output_dir: Path | None = None,
    cv_structure_path: Path | None = None,
    preferences_path: Path | None = None,
) -> None:
    """Run one pipeline stage to completion or park.

    `fit_backend` is an optional pre-built backend used *only* for the
    `fit_assessment` stage, so the cheap one-shot fit gate can run on a different
    model than the rest of the pipeline (see Settings.fit_model). It is injected by
    the caller rather than constructed here — the pipeline must not reach back into
    `jsa.server` for a factory. When None, `general_purpose_backend` runs every stage.

    Handles:
    - Fresh sessions: cv_adjust (from pending) and cover_letter (from cv_done)
    - Resumed sessions: awaiting_input resume (send answer) — cv_adjust, cover_letter, and revision stages
    - Revision sessions: revising_cv and revising_cl (fresh revision from review state)

    On FINAL: writes Document + Messages + transitions to next state atomically.
    On NEED_INPUT: writes FollowUp + Messages + transitions to awaiting_input, raises PausedForInput.
    """
    system_prompt = _get_system_prompt(stage)

    # A launched job snapshots the global language preference at LAUNCH time
    # (job.language — see routes_jobs.py::launch_job) so its whole pipeline stays
    # in one language even if the global preference changes mid-run. Jobs launched
    # before this snapshot existed (job.language is None) fall back to reading the
    # live global preference fresh at stage time (no cache) — mirrors
    # cv_structure_path's read-at-stage-time rationale. Only used to steer NEW
    # sessions (start_session); resumed sessions (restore_session) never see this
    # — see _with_language_directive's docstring.
    if job.language is not None:
        language_code = job.language
    else:
        language_code = "en"
        if preferences_path is not None:
            prefs = await preferences_store.read(preferences_path)
            language_code = prefs.language

    await bus.publish(
        event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Starting stage: {stage.value}"))
    )

    if stage == Stage.fit_assessment:
        # One-shot pre-check: no resume path, no NEED_INPUT, no research. Handled
        # entirely here (FIT → fit_done, anything else → unfit) and returns early.
        base_structure = await _read_base_structure(cv_structure_path)
        await _run_fit_assessment(
            job,
            fit_backend if fit_backend is not None else general_purpose_backend,
            session,
            system_prompt,
            language_code,
            base_structure,
        )
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
            handle = await general_purpose_backend.restore_session(system_prompt, combined_history, revision_session_id)
            reply = await general_purpose_backend.send_message(handle, answer_text)
            accumulated_messages = [
                {"role": "user", "content": answer_text},
                {"role": "assistant", "content": reply.raw},
            ]
        else:
            # Fresh revision: restore the original stage's session and send the
            # revision instruction.
            history = await _load_history(session, job.id, original_stage)
            instruction = rev_req.instruction
            handle = await general_purpose_backend.restore_session(system_prompt, history, revision_session_id)
            reply = await general_purpose_backend.send_message(handle, instruction)
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
            handle = await general_purpose_backend.restore_session(system_prompt, history, job.session_external_id)
            reply = await general_purpose_backend.send_message(handle, answer_text)
            # Only the new turns are new; prior messages already persisted.
            accumulated_messages = [
                {"role": "user", "content": answer_text},
                {"role": "assistant", "content": reply.raw},
            ]
        else:
            # Fresh session. cover_letter gets a company-research brief and the approved
            # tailored CV (the two-lane split's whole point: write the letter against what
            # will actually be submitted, not the base structure). cv_adjust gets neither —
            # research is unwired for it (see CLAUDE.md → "CV structure — single source of
            # truth") and it works from the base structure directly.
            if stage is Stage.cover_letter:
                brief = await _gather_research(job, general_purpose_backend, stage)
                cv_docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
                if cv_docs:
                    cv_block = (
                        "TAILORED CV (approved by the user — write the letter against this)",
                        cv_docs[0].markdown,
                    )
                else:
                    logger.warning(
                        "cover_letter fresh session for job %s has no cv_adjust Document "
                        "yet; falling back to the base CV structure (should be unreachable "
                        "past the CV gate)",
                        job.id,
                    )
                    cv_block = await _base_structure_cv_block(cv_structure_path)
            else:
                brief = None
                cv_block = await _base_structure_cv_block(cv_structure_path)
            initial_user_msg = _build_initial_user_msg(job, brief, cv_block)
            fresh_system_prompt = _with_language_directive(system_prompt, language_code)
            handle, reply = await general_purpose_backend.start_session(fresh_system_prompt, initial_user_msg)
            # Accumulate all messages for this session (system, user, assistant reply)
            accumulated_messages = [
                {"role": "system", "content": fresh_system_prompt},
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

    # Self-heal: if a FINAL block fails content validation (e.g. the agent emitted a
    # change-log or "done" summary instead of the artifact), re-prompt the SAME session
    # to re-emit a clean artifact before the reply is dispatched below. Recovered →
    # proceeds as FINAL; turned into NEED_INPUT → parks; still invalid → _handle_final
    # raises and fails the job.
    reply, accumulated_messages = await _self_heal_final(
        backend=general_purpose_backend,
        handle=handle,
        stage=stage,
        job=job,
        reply=reply,
        accumulated_messages=accumulated_messages,
        language=language_code,
    )

    # Guard against a stale result: the agent turn above may have run for a long
    # time, during which the job could have been dismissed/cancelled/deleted on
    # a separate session. checkpoint()'s transition() guard only validates
    # against this in-memory `job` object (still `running`), so without this
    # re-check a stale NEED_INPUT/FINAL would silently overwrite the real DB
    # state (e.g. resurrect a dismissed job — see StaleJobResult docstring).
    # Read via a fresh session: this session may hold a stale snapshot.
    current_state = await repo.get_state_fresh(session, job.id)
    if current_state != JobState.running:
        raise StaleJobResult(job.id, current_state)

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
        backend=general_purpose_backend,
        handle=handle,
        stage=stage,
        reply=reply,
        accumulated_messages=accumulated_messages,
        output_dir=output_dir,
        language=language_code,
    )

    await bus.publish(
        event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Stage {stage.value}: FINAL received"))
    )

    # Publish stage-completion events after a successful FINAL checkpoint.
    # `job.state` has been mutated by transition() inside _handle_final — read it rather
    # than hardcoding a destination. cv_adjust always lands on cv_review (cv_done is only
    # reached later, via the approve-cv endpoint); revising_cv lands on one of two states
    # depending on RevisionRequest.origin_state (cv_review or review).
    if stage == Stage.cv_adjust:
        await bus.publish(
            event_to_dict(StageCompleteEvent(job_id=job.id, stage="cv_adjust"))
        )
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=job.state.value,
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
                    to_state=job.state.value,
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

# Shown in the modal when the fit-assessment backend times out (fail-to-modal,
# same contract as _FIT_FALLBACK_REASON — see AgentTimeout handling below).
_FIT_TIMEOUT_REASON = (
    "The fit assessment timed out before returning a verdict. Review this job "
    "manually before continuing."
)


def _build_fit_user_msg(job: Job, base_structure: CVDocument | None) -> str:
    """Build the (single) user message for the fit-assessment stage.

    Deliberately minimal — no research brief — so the pre-check stays cheap.
    ``base_structure`` is the standalone base-CV structure (Structure Editor); rendered
    to markdown for readability. Absent (no structure saved yet) → no CV block — the
    gate in Orchestrator.run() keeps jobs pending until a structure exists, so this
    should not normally happen in production.
    """
    cv_block = f"CV:\n{cv_to_markdown(base_structure)}\n\n" if base_structure is not None else ""
    return (
        f"COMPANY: {job.company}\n"
        f"ROLE: {job.role}\n\n"
        f"{cv_block}"
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
    language_code: str = "en",
    base_structure: CVDocument | None = None,
) -> None:
    """Run the one-shot fit-assessment stage and checkpoint the outcome.

    Always a fresh session expecting a single FINAL with a FIT/UNFIT verdict; there
    is no resume / awaiting_input path for this stage. FIT → ``fit_done`` (pipeline
    continues to cv_adjust). Anything else → ``unfit`` (parked), storing the agent's
    reason in ``job.fit_reason`` for the frontend modal.
    """
    initial_user_msg = _build_fit_user_msg(job, base_structure)
    job.retry_count = 0
    # Always a fresh start_session (no resume path for this stage) — safe to inject here.
    system_prompt = _with_language_directive(system_prompt, language_code, fit_verdict=True)

    try:
        handle, reply = await backend.start_session(system_prompt, initial_user_msg)
    except (ProtocolError, AgentTimeout) as exc:
        # A malformed / sentinel-less reply, or a fit-backend timeout (e.g. an
        # aggressively low --fit-timeout), is "unparseable" → fail to the modal
        # (closed), consistent with the verdict contract, rather than failing the
        # job outright. Without this, AgentTimeout would propagate past run_stage
        # into the orchestrator's generic handler and hard-fail the job.
        #
        # Same stale-result guard as run_stage's post-reply check (see StaleJobResult):
        # fit_assessment is the FIRST stage and runs before run_stage's guard is ever
        # reached (that check sits after this whole function returns), so it needs its
        # own re-check here — this is the exact "dismissed before it ever outputs
        # anything" window from the reported bug.
        current_state = await repo.get_state_fresh(session, job.id)
        if current_state != JobState.running:
            raise StaleJobResult(job.id, current_state)
        job.fit_reason = _FIT_TIMEOUT_REASON if isinstance(exc, AgentTimeout) else _FIT_FALLBACK_REASON
        await checkpoint(session, job, JobState.unfit, None)
        await _publish_fit_outcome(job, is_fit=False)
        return

    # Same guard for the normal (non-ProtocolError) path — see comment above.
    current_state = await repo.get_state_fresh(session, job.id)
    if current_state != JobState.running:
        raise StaleJobResult(job.id, current_state)

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
    language: str = "en",
) -> None:
    """Finalize the stage: compute next state, version document, write checkpoint."""
    # A successful FINAL means any soft retry worked — reset the retry counter.
    # checkpoint() calls session.add(job) + commit, so this persists atomically.
    job.retry_count = 0

    # Authoritative content gate before writing anything to the DB. If the model
    # emitted a change-log/summary instead of the artifact (and self-heal could not
    # recover it), this raises FinalContentError → propagates to _run_one → job failed.
    structured_obj = _validate_final_content(stage, reply.content, job, language)

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

    # Serialize the validated structured object to canonical Markdown — the program owns
    # 100% of layout. Persist both the JSON source-of-truth (`structured`) and the
    # rendered Markdown; downstream renderers consume `markdown` unchanged.
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        canonical_md = cv_to_markdown(structured_obj)
    else:  # cover_letter / revising_cl
        canonical_md = cover_letter_to_markdown(structured_obj)

    document_data = {
        "stage": doc_stage,
        "version": next_version,
        "markdown": canonical_md,
        "structured": structured_obj.model_dump_json(),
    }

    # Second stale-result guard: get_documents() above was a real DB round-trip
    # (an await point) since run_stage's pre-check, during which a dismiss/cancel
    # on another session could have landed. Re-check immediately before the
    # checkpoint write that would otherwise resurrect the job (see StaleJobResult
    # docstring / run_stage's guard for the general rationale).
    current_state = await repo.get_state_fresh(session, job.id)
    if current_state != JobState.running:
        raise StaleJobResult(job.id, current_state)

    if stage == Stage.cv_adjust:
        # cv_adjust → cv_review: the CV lane parks for user approve/revise. cv_done is
        # reached only via the approve-cv endpoint (cv_review → cv_done), never directly
        # from here — see CLAUDE.md → "Two-lane pipeline / CV gate".
        await checkpoint(
            session,
            job,
            JobState.cv_review,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
        if output_dir is not None:
            await _render_cv(session, job, output_dir)
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
        # A CV revision can be requested from either the CV gate or final review (both are
        # revisable there — see CLAUDE.md); revising_cl only ever returns to review (no
        # cl_review state exists). Read origin_state BEFORE marking the RevisionRequest
        # consumed. NULL (legacy rows) reads as "review".
        dest_state = JobState.review
        if stage == Stage.revising_cv:
            rr_result = await session.execute(
                select(RevisionRequest.origin_state).where(
                    RevisionRequest.job_id == job.id,
                    RevisionRequest.consumed_at.is_(None),
                )
            )
            origin_state = rr_result.scalar_one_or_none()
            if origin_state == JobState.cv_review.value:
                dest_state = JobState.cv_review

        # Mark the RevisionRequest consumed (within same transaction as the checkpoint commit)
        await session.execute(
            update(RevisionRequest)
            .where(
                RevisionRequest.job_id == job.id,
                RevisionRequest.consumed_at.is_(None),
            )
            .values(consumed_at=datetime.utcnow())
        )
        await checkpoint(
            session,
            job,
            dest_state,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
        if output_dir is not None:
            if dest_state == JobState.cv_review:
                await _render_cv(session, job, output_dir)
            else:
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


def _language_directive(language_code: str, *, fit_verdict: bool = False) -> str:
    """A short directive appended to a NEW session's system prompt for a non-English
    language preference (see CLAUDE.md → "Language preference" / the language-preference
    handoff's "Pipeline Integration" section).

    Carves out the two things that must stay English/ASCII regardless of the chosen
    language: the sentinel blocks (matched literally by ``jsa/agents/protocol.py``) and,
    for ``fit_assessment`` only, the ``FIT``/``UNFIT`` verdict word itself (matched
    literally by ``_parse_fit_verdict``).
    """
    name = language_name(language_code)
    lines = [
        "\n\n## Output language",
        f"Write all natural-language, user-facing output in {name} ({language_code}) — "
        "the change-log, any clarifying questions you ask, and every text VALUE in the "
        "final JSON (summary prose, bullet text, headings, cover-letter paragraphs). Do "
        "not translate JSON keys/field names — they are fixed schema fields and must "
        "stay exactly as specified (`heading`, `subheading`, `bullets`, `text`, `items`, "
        "`entries`, etc.).",
        "The sentinel blocks `<<<FINAL>>>`, `<<<NEED_INPUT>>>`, and `<<<END>>>` must stay "
        "exactly as spelled, in English/ASCII — never translate or localize them.",
    ]
    if fit_verdict:
        lines.append(
            "The verdict word itself (`FIT` or `UNFIT`) must stay in English and be the "
            f"first word of your reply; only the reason that follows should be in {name}."
        )
    return "\n".join(lines)


def _with_language_directive(system_prompt: str, language_code: str, *, fit_verdict: bool = False) -> str:
    """Append the language directive to ``system_prompt`` for a NEW session only.

    A no-op for the default ``"en"`` — the prompt files are already written in English, so
    there is nothing to direct. Never call this for a ``restore_session`` path: a resumed
    session already committed to a language, and re-injecting a changed directive would
    contradict the replayed history (see the handoff's "Do not change language
    mid-conversation").
    """
    if language_code == "en":
        return system_prompt
    return system_prompt + _language_directive(language_code, fit_verdict=fit_verdict)


def _build_initial_user_msg(
    job: Job, brief: str | None, cv_block: tuple[str, str] | None = None
) -> str:
    """Build the initial user message for a fresh session.

    ``brief`` is either a populated research block ([INTEL_BRIEF] or [COMPANY_BRIEF]) or
    the NONE placeholder produced by ``_research_placeholder`` — or ``None`` when the
    stage has no research (cv_adjust; research is unwired for it, see CLAUDE.md → "CV
    structure — single source of truth"). When present it is injected first so the main
    agent sees it immediately and the resumed Message history replays it verbatim.

    ``cv_block`` is an optional ``(label, content)`` pair supplying the CV content —
    cv_adjust gets the base structure's JSON skeleton; cover_letter gets the approved
    tailored CV's markdown (or, absent one, the base structure as a fallback). This block
    only supplies the data; stage-specific instructions for *how* to use it live in each
    stage's own system prompt, not here. Injected here (rather than after research) so it
    is part of the persisted initial message and replays verbatim on an awaiting_input
    resume.
    """
    brief_part = f"{brief}\n\n" if brief is not None else ""
    skeleton = ""
    if cv_block is not None:
        label, content = cv_block
        skeleton = f"{label}:\n{content}\n\n"
    return (
        f"{brief_part}"
        f"{skeleton}"
        f"JOB DESCRIPTION:\n{job.jd}\n\n"
        f"TIER: {job.tier}"
    )


async def _base_structure_cv_block(cv_structure_path: Path | None) -> tuple[str, str] | None:
    """Return the base-CV structure as a ``(label, content)`` cv_block, or None if absent."""
    base_structure = await _read_base_structure(cv_structure_path)
    if base_structure is None:
        return None
    return (
        "BASE CV STRUCTURE (the candidate's base CV, curated in the Structure Editor)",
        base_structure.model_dump_json(indent=2),
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

    Only ``cover_letter`` is a valid input. ``cv-research``/``cv_adjust`` is unwired
    (see CLAUDE.md → "CV structure — single source of truth"): ``cv-research.md`` and
    ``GEMINI_CV_RESEARCH.md`` are deliberately left on disk, but nothing calls this with
    ``Stage.cv_adjust`` anymore. ``_gather_research`` is never called for revision stages
    either, but this guard makes that contract explicit.
    """
    if stage == Stage.cover_letter:
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
            "only cover_letter is supported."
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
