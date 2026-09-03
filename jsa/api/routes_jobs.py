"""REST routes for job management: /api/jobs and sub-routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from jsa.api.transcript import build_transcript
from jsa.db import repo
from jsa.db.models import Document, FollowUp, Job, JobState, Message, RevisionRequest, Stage
from jsa.events.bus import bus
from jsa.events.schema import (
    ApprovedEvent,
    StatusChangedEvent,
    JobRemovedEvent,
    TranscriptChangedEvent,
    event_to_dict,
)
from jsa.pipeline.state_machine import set_current_stage, transition
from jsa.render.registry import renderer_for
from jsa.store import preferences as preferences_store
from jsa.util import slugify as _slugify

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_factory(request: Request):
    return request.app.state.session_factory


def _model_resolver_for(request: Request) -> Callable[[str], str | None]:
    """The app's backend->model resolver (`server.py::make_model_resolver`), or a
    no-op if the app never set one (e.g. a test app built without startup running).
    Used only to fill in a job's *effective* model when it hasn't hopped yet — see
    `_job_to_dict`."""
    resolver = getattr(request.app.state, "model_resolver", None)
    return resolver if resolver is not None else (lambda _name: None)


def _doc_to_dict(doc: Document) -> dict:
    return {
        "id": doc.id,
        "job_id": doc.job_id,
        "stage": doc.stage.value if doc.stage is not None else None,
        "version": doc.version,
        "markdown": doc.markdown,
        "pdf_path": doc.pdf_path,
        "docx_path": doc.docx_path,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _follow_up_to_dict(fu: FollowUp) -> dict:
    return {
        "id": fu.id,
        "job_id": fu.job_id,
        "stage": fu.stage.value if fu.stage is not None else None,
        "question": fu.question,
        "answer": fu.answer,
        "asked_at": fu.asked_at.isoformat() if fu.asked_at else None,
        "answered_at": fu.answered_at.isoformat() if fu.answered_at else None,
    }


def _job_to_dict(
    job: Job,
    *,
    full: bool = False,
    model_resolver: Callable[[str], str | None] | None = None,
) -> dict:
    # Effective model (Phase 5, display only — never written back to the job or to the
    # header's runtime-selection dropdown, see CLAUDE.md "Model ladder"): the job's own
    # pinned rung once it has hopped, else whatever its backend is currently configured
    # to run. `model_resolver` is the exact same backend->model mapping the orchestrator's
    # ladder uses (`server.py::make_model_resolver`), so this can never disagree with what
    # a fresh dispatch on that backend would actually pick.
    effective_model = job.model_name
    if effective_model is None and job.backend_name is not None and model_resolver is not None:
        effective_model = model_resolver(job.backend_name)

    d: dict[str, Any] = {
        "id": job.id,
        "company": job.company,
        "role": job.role,
        "link": job.link,
        "tier": job.tier,
        "jd": job.jd,
        "state": job.state.value if job.state is not None else None,
        "current_stage": job.current_stage.value if job.current_stage is not None else None,
        "backend_name": job.backend_name,
        "model_name": job.model_name,
        "effective_model": effective_model,
        "language": job.language,
        "fit_reason": job.fit_reason,
        "error": job.error,
        "retry_count": job.retry_count,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
    if full:
        d["documents"] = [_doc_to_dict(doc) for doc in job.documents]
        d["follow_ups"] = [_follow_up_to_dict(fu) for fu in job.follow_ups]
    return d


async def _fetch_job_with_relations(session, job_id: str) -> Job | None:
    """Fetch a Job with documents and follow_ups eagerly loaded (no lazy-load errors)."""
    result = await session.execute(
        select(Job)
        .options(selectinload(Job.documents), selectinload(Job.follow_ups))
        .where(Job.id == job_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------


class AnswerBody(BaseModel):
    follow_up_id: int
    text: str


class ReviseBody(BaseModel):
    target: str  # "cv" | "cl"
    text: str


class ExportBody(BaseModel):
    format: str  # "pdf" | "docx"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/jobs")
async def list_jobs(request: Request, state: str | None = None):
    """Return all jobs (summary), optionally filtered by state."""
    sf = _session_factory(request)
    state_enum: JobState | None = None
    if state is not None:
        try:
            state_enum = JobState(state)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid state: {state!r}")

    async with sf() as session:
        stmt = select(Job)
        if state_enum is not None:
            stmt = stmt.where(Job.state == state_enum)
        result = await session.execute(stmt)
        jobs = list(result.scalars().all())
        # Summary endpoint: no need to load relationships
        return [_job_to_dict(job, full=False, model_resolver=_model_resolver_for(request)) for job in jobs]


@router.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    """Return full job with documents and follow_ups. 404 if not found."""
    sf = _session_factory(request)
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        return _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))


@router.get("/api/jobs/{job_id}/transcript")
async def get_transcript(request: Request, job_id: str):
    """Return the display-ready, ordered transcript turns for a job. 404 if not found.

    Loads messages explicitly (Message.id ascending — the only reliable order, see
    build_transcript's docstring) rather than via `_fetch_job_with_relations`, which
    intentionally does not eager-load `messages` (many mutating endpoints share that
    helper and would each pay for an unused eager-load).
    """
    sf = _session_factory(request)
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        msg_result = await session.execute(
            select(Message).where(Message.job_id == job_id).order_by(Message.id.asc())
        )
        messages = list(msg_result.scalars().all())

        rev_result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job_id)
        )
        revision_requests = list(rev_result.scalars().all())

        follow_ups = sorted(job.follow_ups, key=lambda fu: (fu.asked_at, fu.id))
        documents = list(job.documents)

    return build_transcript(job, messages, follow_ups, documents, revision_requests)


@router.post("/api/jobs/{job_id}/answer")
async def answer_follow_up(request: Request, job_id: str, body: AnswerBody):
    """Record the user's answer to a follow-up question."""
    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        # Load the FollowUp
        fu_result = await session.execute(
            select(FollowUp).where(FollowUp.id == body.follow_up_id)
        )
        fu = fu_result.scalar_one_or_none()
        if fu is None:
            raise HTTPException(
                status_code=404,
                detail=f"FollowUp {body.follow_up_id} not found",
            )
        if fu.job_id != job_id:
            raise HTTPException(
                status_code=400,
                detail=f"FollowUp {body.follow_up_id} does not belong to job {job_id!r}",
            )
        if fu.answered_at is not None:
            raise HTTPException(
                status_code=400,
                detail=f"FollowUp {body.follow_up_id} is already answered",
            )

        fu.answer = body.text
        fu.answered_at = datetime.utcnow()
        session.add(fu)
        await session.commit()

    # Re-fetch with relationships after commit (fresh session to avoid stale state)
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    await bus.publish(event_to_dict(TranscriptChangedEvent(job_id=job_id)))

    # Kick the orchestrator after releasing the session
    request.app.state.orchestrator.kick()

    return job_dict


@router.post("/api/jobs/{job_id}/approve")
async def approve_job(request: Request, job_id: str):
    """Approve a job in review state: transition to approved (rendering already done by pipeline)."""
    sf = _session_factory(request)

    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state != JobState.review:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'review'",
            )

        # Get latest cv_adjust and cover_letter documents
        cv_docs = await repo.get_documents(session, job_id, stage=Stage.cv_adjust)
        cl_docs = await repo.get_documents(session, job_id, stage=Stage.cover_letter)

        if not cv_docs:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} has no cv_adjust document",
            )
        if not cl_docs:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} has no cover_letter document",
            )

        cv_doc = cv_docs[0]   # get_documents returns version desc order
        cl_doc = cl_docs[0]

        # Capture pdf paths before checkpoint() expires the ORM objects
        cv_pdf_path = cv_doc.pdf_path or ""
        cl_pdf_path = cl_doc.pdf_path or ""

        # Transition to approved atomically
        await repo.checkpoint(session, job, JobState.approved, new_stage=None)

    await bus.publish(
        event_to_dict(
            ApprovedEvent(
                job_id=job_id,
                cv_pdf_path=cv_pdf_path,
                cl_pdf_path=cl_pdf_path,
            )
        )
    )

    return {"cv_pdf_path": cv_pdf_path, "cl_pdf_path": cl_pdf_path}


@router.post("/api/jobs/{job_id}/approve-cv")
async def approve_cv_job(request: Request, job_id: str):
    """Approve the CV at the CV gate: cv_review → cv_done, unlocking the cover-letter lane.

    Requires state == cv_review and a cv_adjust Document (the CV gate always renders one
    on entry — see jsa/pipeline/stages.py::_handle_final — so its absence would indicate
    a bug, not a normal condition). Rejects with 400 if an unconsumed RevisionRequest
    exists: a bare cv_review + unconsumed RevisionRequest is a legitimate parked
    combination (see jsa/db/repo.py::list_runnable_jobs), and approving through it would
    strand the request — cv_done's runnable arm has no revision condition, so it would
    never be consumed or dispatched.
    """
    sf = _session_factory(request)

    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state != JobState.cv_review:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'cv_review'",
            )

        unconsumed_result = await session.execute(
            select(RevisionRequest.id).where(
                RevisionRequest.job_id == job_id,
                RevisionRequest.consumed_at.is_(None),
            )
        )
        if unconsumed_result.first() is not None:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} has a pending CV revision; cannot approve until it completes",
            )

        cv_docs = await repo.get_documents(session, job_id, stage=Stage.cv_adjust)
        if not cv_docs:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} has no cv_adjust document",
            )
        cv_doc = cv_docs[0]
        cv_pdf_path = cv_doc.pdf_path or ""
        cv_docx_path = cv_doc.docx_path or ""

        await repo.checkpoint(session, job, JobState.cv_done, new_stage=None)

    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job_id,
                from_state=JobState.cv_review.value,
                to_state=JobState.cv_done.value,
            )
        )
    )

    request.app.state.orchestrator.kick()

    return {"pdf_path": cv_pdf_path, "docx_path": cv_docx_path}


@router.post("/api/jobs/{job_id}/revise")
async def revise_job(request: Request, job_id: str, body: ReviseBody):
    """Insert a revision request and wake the orchestrator.

    Accepts state in (review, cv_review) — both artifacts are revisable at final review,
    but a cv_review job has no cover letter yet, so target="cl" there is rejected.
    """
    if body.target not in ("cv", "cl"):
        raise HTTPException(
            status_code=400,
            detail=f"target must be 'cv' or 'cl', got {body.target!r}",
        )

    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state not in (JobState.review, JobState.cv_review):
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'review' or 'cv_review'",
            )
        if job.state == JobState.cv_review and body.target == "cl":
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is at the CV gate (cv_review) — no cover letter exists yet",
            )

        # Reject a second revision request while one is already pending. Without
        # this, job.state stays review/cv_review until the orchestrator actually
        # dispatches (only current_stage flips), so a double-click or a race lets
        # two POSTs both pass the state check above. The DB's uq_revision_open
        # partial unique index (job_id WHERE consumed_at IS NULL) would reject the
        # second INSERT anyway, but only as an unhandled IntegrityError — this
        # check turns that into a clean 409 instead.
        existing = await session.execute(
            select(RevisionRequest.id).where(
                RevisionRequest.job_id == job_id,
                RevisionRequest.consumed_at.is_(None),
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id!r} already has a pending revision request",
            )

        # Map target to stage values
        if body.target == "cv":
            revision_target = Stage.cv_adjust
            new_current_stage = Stage.revising_cv
        else:
            revision_target = Stage.cover_letter
            new_current_stage = Stage.revising_cl

        # Insert revision request — origin_state records where to return once the
        # revision completes (see jsa/pipeline/stages.py::_handle_final).
        rev_req = RevisionRequest(
            job_id=job_id,
            target=revision_target,
            instruction=body.text,
            origin_state=job.state.value,
        )
        session.add(rev_req)

        # Set current_stage (state stays review/cv_review per spec) and updated_at
        set_current_stage(job, new_current_stage)
        job.updated_at = datetime.utcnow()
        session.add(job)
        await session.commit()

    # Re-fetch with relationships
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    await bus.publish(event_to_dict(TranscriptChangedEvent(job_id=job_id)))

    request.app.state.orchestrator.kick()

    return job_dict


@router.post("/api/jobs/{job_id}/dismiss")
async def dismiss_job(request: Request, job_id: str):
    """Dismiss a job (any non-approved, non-dismissed state → dismissed)."""
    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state in (JobState.approved, JobState.dismissed):
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r} and cannot be dismissed",
            )

        prev_state = job.state.value
        await repo.checkpoint(session, job, JobState.dismissed, new_stage=None)

    # Re-fetch with relationships after checkpoint
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job_id,
                from_state=prev_state,
                to_state=JobState.dismissed.value,
            )
        )
    )

    # Best-effort: stop the in-flight agent turn (if any) so it stops consuming
    # API tokens immediately. The `dismissed` state is already committed above
    # (DB is authoritative regardless of whether cancellation lands in time);
    # StaleJobResult (jsa/pipeline/stages.py) is the correctness backstop that
    # discards a stale result even if this cancellation loses the race.
    request.app.state.orchestrator.cancel_task(job_id)

    return job_dict


@router.post("/api/jobs/{job_id}/ignore-fit")
async def ignore_fit(request: Request, job_id: str):
    """Override an unfit fit-assessment: unfit → fit_done, then resume the pipeline.

    Mirrors the 'Ignore' button on the not-a-fit modal. Only valid while the job is
    parked in `unfit`; transitions to `fit_done` (the same state a passed check lands
    in) so the orchestrator dispatches cv_adjust next.
    """
    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state != JobState.unfit:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'unfit'",
            )

        prev_state = job.state.value
        job.fit_reason = None  # cleared once the user chooses to proceed
        await repo.checkpoint(session, job, JobState.fit_done, new_stage=None)

    # Re-fetch with relationships after checkpoint
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job_id,
                from_state=prev_state,
                to_state=JobState.fit_done.value,
            )
        )
    )
    # fit_reason cleared above → the verdict turn disappears from the transcript.
    await bus.publish(event_to_dict(TranscriptChangedEvent(job_id=job_id)))

    request.app.state.orchestrator.kick()

    return job_dict


@router.post("/api/jobs/{job_id}/launch")
async def launch_job(request: Request, job_id: str):
    """Launch a parked job: queued → pending, snapshotting the current global language.

    Manual launch is unconditional (CLAUDE.md → "Language preference" /
    ARCH.md → "Manual job launch") — nothing auto-runs on ingest anymore. The
    global language preference at the moment of this click is frozen onto
    `job.language` so the whole pipeline (fit → cv → cover letter) stays in one
    language even if the global preference changes later while this job runs.
    """
    sf = _session_factory(request)
    settings = request.app.state.settings
    prefs = await preferences_store.load(settings)

    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state != JobState.queued:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'queued'",
            )

        prev_state = job.state.value
        job.language = prefs.language
        await repo.checkpoint(session, job, JobState.pending, new_stage=None)

    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job_id,
                from_state=prev_state,
                to_state=JobState.pending.value,
            )
        )
    )

    request.app.state.orchestrator.kick()

    return job_dict


@router.post("/api/jobs/launch-all")
async def launch_all_jobs(request: Request):
    """Launch every currently-queued job in one shot (the sidebar's 'Launch All').

    Same per-job semantics as launch_job (snapshot language, queued → pending),
    just looped, with a single orchestrator.kick() at the end.
    """
    sf = _session_factory(request)
    settings = request.app.state.settings
    prefs = await preferences_store.load(settings)

    launched_ids: list[str] = []
    async with sf() as session:
        queued_jobs = await repo.list_jobs(session, state=JobState.queued)
        for job in queued_jobs:
            prev_state = job.state.value
            job.language = prefs.language
            await repo.checkpoint(session, job, JobState.pending, new_stage=None)
            launched_ids.append(job.id)
            await bus.publish(
                event_to_dict(
                    StatusChangedEvent(
                        job_id=job.id,
                        from_state=prev_state,
                        to_state=JobState.pending.value,
                    )
                )
            )

    if launched_ids:
        request.app.state.orchestrator.kick()

    return {"launched": launched_ids, "count": len(launched_ids)}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    """Cancel a running job — transitions running → pending and kicks the orchestrator.

    The `pending` state is committed first (DB is authoritative regardless of
    whether cancellation lands in time); the in-flight agent turn (if any) is
    then best-effort cancelled via orchestrator.cancel_task(), which now kills
    the underlying claude/agy subprocess's whole process group (see
    jsa/agents/_subprocess.py) so it stops consuming API tokens immediately.
    StaleJobResult (jsa/pipeline/stages.py) is the correctness backstop that
    discards a stale result even if this cancellation loses the race.
    """
    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state != JobState.running:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'running'",
            )

        prev_state = job.state.value
        await repo.checkpoint(session, job, JobState.pending, new_stage=None)

    # Re-fetch with relationships after checkpoint
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job_id,
                from_state=prev_state,
                to_state=JobState.pending.value,
            )
        )
    )

    # Best-effort: kill the in-flight agent subprocess (if any) so it stops
    # consuming API tokens immediately, rather than running to completion.
    request.app.state.orchestrator.cancel_task(job_id)

    request.app.state.orchestrator.kick()

    return job_dict


@router.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: str):
    """Permanently delete a job and all its data from the database.

    Works for any state. Best-effort for running jobs — an in-flight orchestrator
    task will try to UPDATE a now-gone row; SQLite ignores 0-row UPDATEs and the
    _run_one exception handler handles StaleDataError gracefully.
    """
    sf = _session_factory(request)
    async with sf() as session:
        result = await session.execute(
            select(Job)
            .options(
                selectinload(Job.messages),
                selectinload(Job.documents),
                selectinload(Job.follow_ups),
                selectinload(Job.revision_requests),
            )
            .where(Job.id == job_id)
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        await session.delete(job)
        await session.commit()

    await bus.publish(
        event_to_dict(JobRemovedEvent(job_id=job_id))
    )

    return {"ok": True}


@router.post("/api/jobs/{job_id}/reset")
async def reset_job(request: Request, job_id: str):
    """Soft-reset (retry_count==0) or nuclear-reset (retry_count>0) a failed job.

    Dismissed jobs use an unchanged checkpoint path (BF-15 scope: dismissed is out of scope).
    """
    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state not in (JobState.failed, JobState.dismissed):
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'failed' or 'dismissed'",
            )

        prev_state = job.state.value

        failed_reset = job.state == JobState.failed
        if failed_reset:
            if job.retry_count == 0:
                await repo.soft_reset_job(session, job)
            else:
                await repo.nuclear_reset_job(session, job)

            new_state = job.state.value  # updated by the reset function
        else:
            # dismissed → pending: existing checkpoint path (unchanged)
            await repo.checkpoint(session, job, JobState.pending, new_stage=None)
            new_state = JobState.pending.value

    # Publish status change event for failed→* resets (dismissed path unchanged — no event).
    if failed_reset:
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job_id,
                    from_state=prev_state,
                    to_state=new_state,
                )
            )
        )
        # soft_reset_job/nuclear_reset_job delete Messages/FollowUps/RevisionRequests
        # (and, for nuclear, Documents too) — the transcript must be invalidated.
        await bus.publish(event_to_dict(TranscriptChangedEvent(job_id=job_id)))

    # Re-fetch with relationships for the response
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True, model_resolver=_model_resolver_for(request))

    # Wake the orchestrator so it picks up the now-pending/cv_done job immediately.
    request.app.state.orchestrator.kick()

    return job_dict


@router.get("/api/jobs/{job_id}/document/{stage}")
async def get_document(
    request: Request,
    job_id: str,
    stage: str,
    version: int | None = None,
):
    """Return markdown content for a document at a given stage and optional version."""
    # Validate stage
    try:
        stage_enum = Stage(stage)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage!r}")

    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        docs = await repo.get_documents(session, job_id, stage=stage_enum)
        if not docs:
            raise HTTPException(
                status_code=404,
                detail=f"No document found for job {job_id!r} stage {stage!r}",
            )

        if version is not None:
            # Find the specific version
            doc = next((d for d in docs if d.version == version), None)
            if doc is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Version {version} not found for job {job_id!r} stage {stage!r}",
                )
        else:
            # docs is ordered by version desc, so first is latest
            doc = docs[0]

        return {"markdown": doc.markdown, "version": doc.version}


@router.post("/api/jobs/{job_id}/export")
async def export_job(request: Request, job_id: str, body: ExportBody):
    """Re-render job documents in the requested format and return file paths.

    Requires job.state in (cv_review, review, approved) (409 otherwise) — a cv_review
    job only has a CV to export; review/approved jobs may have both. Renders whichever
    of cv_adjust/cover_letter has a Document, skipping the other rather than erroring on
    its absence.
    Re-runnable: calling again with the same format overwrites the output file.
    Does NOT call repo.checkpoint / transition — state is left unchanged.
    """
    if body.format not in ("pdf", "docx"):
        raise HTTPException(
            status_code=400,
            detail=f"format must be 'pdf' or 'docx', got {body.format!r}",
        )

    sf = _session_factory(request)
    settings = request.app.state.settings

    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state not in (JobState.cv_review, JobState.review, JobState.approved):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Job {job_id!r} is in state {job.state.value!r}, expected "
                    "'cv_review', 'review', or 'approved'"
                ),
            )

        cv_docs = await repo.get_documents(session, job_id, stage=Stage.cv_adjust)
        cl_docs = await repo.get_documents(session, job_id, stage=Stage.cover_letter)

        if not cv_docs and not cl_docs:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} has no documents to export",
            )

        # Compute output paths — same slug convention as approve
        slug = f"{_slugify(job.company)}_{_slugify(job.role)}_{job.id[:8]}"
        ext = "pdf" if body.format == "pdf" else "docx"

        cv_out = settings.output_dir / slug / f"cv.{ext}" if cv_docs else None
        cl_out = settings.output_dir / slug / f"cover_letter.{ext}" if cl_docs else None

        # Capture IDs and markdown before closing session
        cv_markdown = cv_docs[0].markdown if cv_docs else None
        cl_markdown = cl_docs[0].markdown if cl_docs else None
        cv_doc_id = cv_docs[0].id if cv_docs else None
        cl_doc_id = cl_docs[0].id if cl_docs else None

    # Select the renderer — "pdf" maps to "weasyprint"; "docx" maps to "docx"
    renderer_key = "weasyprint" if body.format == "pdf" else "docx"
    renderer = renderer_for(renderer_key)

    # Render outside the session — both renderers use asyncio.to_thread internally
    if cv_out is not None:
        await renderer.render(cv_markdown, cv_out)
    if cl_out is not None:
        await renderer.render(cl_markdown, cl_out)

    # Update the path columns in a single DB transaction
    async with sf() as session:
        if cv_doc_id is not None:
            cv_result = await session.execute(
                select(Document).where(Document.id == cv_doc_id)
            )
            cv_doc = cv_result.scalar_one()
            if body.format == "pdf":
                cv_doc.pdf_path = str(cv_out)
            else:
                cv_doc.docx_path = str(cv_out)
            session.add(cv_doc)
        if cl_doc_id is not None:
            cl_result = await session.execute(
                select(Document).where(Document.id == cl_doc_id)
            )
            cl_doc = cl_result.scalar_one()
            if body.format == "pdf":
                cl_doc.pdf_path = str(cl_out)
            else:
                cl_doc.docx_path = str(cl_out)
            session.add(cl_doc)
        await session.commit()

    # Return relative paths (relative to output_dir) so the frontend can build
    # a /api/files/<relpath> URL that the file-serving route resolves safely.
    result: dict[str, str] = {}
    if cv_out is not None:
        result["cv_path"] = str(cv_out.relative_to(settings.output_dir))
    if cl_out is not None:
        result["cl_path"] = str(cl_out.relative_to(settings.output_dir))

    return result


@router.get("/api/files/{relpath:path}")
async def serve_output_file(request: Request, relpath: str):
    """Serve a file from output_dir by relative path.

    Safety: resolves the full path and verifies it is inside output_dir before
    serving. Returns 404 if the file does not exist or is outside the directory.
    """
    settings = request.app.state.settings
    output_dir: Path = settings.output_dir.resolve()

    try:
        full_path = (output_dir / relpath).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Ensure the resolved path is inside the output directory (path traversal guard)
    try:
        full_path.relative_to(output_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside output directory")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {relpath!r}")

    # Serve inline so the frontend's preview <iframe> renders PDFs in place.
    # Forced downloads are handled client-side via the anchor `download` attribute.
    # Explicit media_type prevents browsers from falling back to application/octet-stream
    # (which always triggers a download regardless of Content-Disposition).
    media_type = "application/pdf" if full_path.suffix.lower() == ".pdf" else None
    return FileResponse(str(full_path), media_type=media_type, content_disposition_type="inline")
