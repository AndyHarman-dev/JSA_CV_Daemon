"""REST routes for job management: /api/jobs and sub-routes."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from jsa.db import repo
from jsa.db.models import Document, FollowUp, Job, JobState, RevisionRequest, Stage
from jsa.events.bus import bus
from jsa.events.schema import ApprovedEvent, event_to_dict
from jsa.pipeline.state_machine import set_current_stage, transition
from jsa.render.registry import renderer_for

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_factory(request: Request):
    return request.app.state.session_factory


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _doc_to_dict(doc: Document) -> dict:
    return {
        "id": doc.id,
        "job_id": doc.job_id,
        "stage": doc.stage.value if doc.stage is not None else None,
        "version": doc.version,
        "markdown": doc.markdown,
        "pdf_path": doc.pdf_path,
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


def _job_to_dict(job: Job, *, full: bool = False) -> dict:
    d: dict[str, Any] = {
        "id": job.id,
        "company": job.company,
        "role": job.role,
        "link": job.link,
        "tier": job.tier,
        "state": job.state.value if job.state is not None else None,
        "current_stage": job.current_stage.value if job.current_stage is not None else None,
        "error": job.error,
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
        return [_job_to_dict(job, full=False) for job in jobs]


@router.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    """Return full job with documents and follow_ups. 404 if not found."""
    sf = _session_factory(request)
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        return _job_to_dict(job, full=True)


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
        job_dict = _job_to_dict(job, full=True)

    # Kick the orchestrator after releasing the session
    request.app.state.orchestrator.kick()

    return job_dict


@router.post("/api/jobs/{job_id}/approve")
async def approve_job(request: Request, job_id: str):
    """Approve a job in review state: render PDFs and transition to approved."""
    sf = _session_factory(request)
    settings = request.app.state.settings

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

        # Compute output paths
        slug = f"{_slugify(job.company)}_{_slugify(job.role)}_{job.id[:8]}"
        cv_pdf = settings.output_dir / slug / "cv.pdf"
        cl_pdf = settings.output_dir / slug / "cover_letter.pdf"

        # Capture IDs and markdown before closing session
        cv_markdown = cv_doc.markdown
        cl_markdown = cl_doc.markdown
        cv_doc_id = cv_doc.id
        cl_doc_id = cl_doc.id

    # Render PDFs outside the session (async-safe, uses asyncio.to_thread internally)
    renderer = renderer_for("weasyprint")
    await renderer.render(cv_markdown, cv_pdf)
    await renderer.render(cl_markdown, cl_pdf)

    # Write PDF paths + transition in a single transaction
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        # Re-fetch documents to update pdf_path
        cv_result = await session.execute(
            select(Document).where(Document.id == cv_doc_id)
        )
        cv_doc = cv_result.scalar_one()
        cl_result = await session.execute(
            select(Document).where(Document.id == cl_doc_id)
        )
        cl_doc = cl_result.scalar_one()

        cv_doc.pdf_path = str(cv_pdf)
        cl_doc.pdf_path = str(cl_pdf)
        session.add(cv_doc)
        session.add(cl_doc)

        # Use repo.checkpoint to transition state atomically — includes commit
        await repo.checkpoint(session, job, JobState.approved, new_stage=None)

    await bus.publish(
        event_to_dict(
            ApprovedEvent(
                job_id=job_id,
                cv_pdf_path=str(cv_pdf),
                cl_pdf_path=str(cl_pdf),
            )
        )
    )

    return {"cv_pdf_path": str(cv_pdf), "cl_pdf_path": str(cl_pdf)}


@router.post("/api/jobs/{job_id}/revise")
async def revise_job(request: Request, job_id: str, body: ReviseBody):
    """Insert a revision request and wake the orchestrator."""
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
        if job.state != JobState.review:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'review'",
            )

        # Map target to stage values
        if body.target == "cv":
            revision_target = Stage.cv_adjust
            new_current_stage = Stage.revising_cv
        else:
            revision_target = Stage.cover_letter
            new_current_stage = Stage.revising_cl

        # Insert revision request
        rev_req = RevisionRequest(
            job_id=job_id,
            target=revision_target,
            instruction=body.text,
        )
        session.add(rev_req)

        # Set current_stage (state stays review per spec) and updated_at
        set_current_stage(job, new_current_stage)
        job.updated_at = datetime.utcnow()
        session.add(job)
        await session.commit()

    # Re-fetch with relationships
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True)

    request.app.state.orchestrator.kick()

    return job_dict


@router.post("/api/jobs/{job_id}/reset")
async def reset_job(request: Request, job_id: str):
    """Reset a failed job back to pending."""
    sf = _session_factory(request)
    async with sf() as session:
        job = await repo.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if job.state != JobState.failed:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id!r} is in state {job.state.value!r}, expected 'failed'",
            )

        # Use repo.checkpoint per CLAUDE.md checkpoint rule
        await repo.checkpoint(session, job, JobState.pending, new_stage=None)

    # Re-fetch with relationships after checkpoint
    async with sf() as session:
        job = await _fetch_job_with_relations(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        job_dict = _job_to_dict(job, full=True)

    # Wake the orchestrator so it picks up the now-pending job immediately.
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
