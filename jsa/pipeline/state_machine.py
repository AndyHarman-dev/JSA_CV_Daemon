"""Allowed state transitions table and transition() guard."""

from jsa.db.models import JobState, Stage


class InvalidTransition(Exception): ...


ALLOWED = {
    # queued: fresh ingest, parked until the user clicks LAUNCH (queued → pending).
    JobState.queued:         {JobState.pending, JobState.dismissed},
    JobState.pending:        {JobState.running, JobState.failed, JobState.dismissed},
    # running → pending: crash recovery OR backend-switch restart (cv_adjust limit hit)
    # running → fit_done/unfit: fit-assessment outcomes (pass / flagged mismatch)
    # running → cv_review: cv_adjust (or revising_cv) finalises; CV lane parks for approval
    # running → cv_done: backend-switch restart (cover_letter limit hit; rewind to cv_done —
    #     this is the BF-19 rewind target, NOT a redirect to cv_review; the CV was already
    #     approved to get this far, so re-parking at the CV gate would be wrong)
    # running → review:  backend-switch restart (revision limit hit; rewind to review)
    JobState.running:        {JobState.awaiting_input, JobState.fit_done, JobState.unfit, JobState.cv_review, JobState.cv_done, JobState.cl_done, JobState.review, JobState.failed, JobState.pending, JobState.dismissed},
    JobState.awaiting_input: {JobState.running, JobState.cv_review, JobState.review, JobState.failed, JobState.dismissed},
    # fit_done parallels cv_done: ready to be picked up for the next stage (cv_adjust).
    JobState.fit_done:       {JobState.running, JobState.failed, JobState.dismissed},
    # unfit is parked: Ignore → fit_done (proceed), Dismiss → dismissed.
    JobState.unfit:          {JobState.fit_done, JobState.dismissed},
    # cv_review is parked: user approves (→ cv_done) or requests a revision (→ running(revising_cv)).
    JobState.cv_review:      {JobState.running, JobState.cv_done, JobState.failed, JobState.dismissed},
    # cv_done means "CV approved, cover letter pending, runnable" — UNCHANGED, and it stays the
    # BF-19 rewind target from running(cover_letter). Do not redirect it to cv_review.
    JobState.cv_done:        {JobState.running, JobState.failed, JobState.dismissed},
    JobState.cl_done:        {JobState.review, JobState.failed, JobState.dismissed},
    JobState.review:         {JobState.running, JobState.awaiting_input, JobState.approved, JobState.failed, JobState.dismissed},
    JobState.approved:       set(),
    JobState.failed:         {JobState.pending, JobState.dismissed, JobState.cv_done},
    JobState.dismissed:      {JobState.pending},
}

# Stage compatibility: which stages are valid for each state
STAGE_FOR_STATE = {
    JobState.running: {Stage.fit_assessment, Stage.cv_adjust, Stage.cover_letter, Stage.revising_cv, Stage.revising_cl},
    JobState.awaiting_input: {Stage.cv_adjust, Stage.cover_letter, Stage.revising_cv, Stage.revising_cl},
    # All other states: current_stage must be None
}


def transition(job, new_state: JobState, new_stage: Stage | None = None) -> None:
    """Mutate job.state and job.current_stage after validating the transition.
    Raises InvalidTransition if:
    - new_state is not in ALLOWED[job.state]
    - new_stage is incompatible with new_state
    Also validates running→cv_done implies current_stage==cv_adjust,
    and running→cl_done implies current_stage==cover_letter."""
    allowed = ALLOWED.get(job.state, set())
    if new_state not in allowed:
        raise InvalidTransition(f"{job.state} → {new_state} is not allowed")
    # Stage compatibility check
    if new_state in STAGE_FOR_STATE:
        if new_stage is None:
            raise InvalidTransition(f"State {new_state} requires a current_stage")
        if new_stage not in STAGE_FOR_STATE[new_state]:
            raise InvalidTransition(f"Stage {new_stage} incompatible with state {new_state}")
    else:
        if new_stage is not None:
            raise InvalidTransition(f"State {new_state} must have current_stage=None, got {new_stage}")
    # running → cv_done: valid ONLY when current stage is cover_letter (backend-switch
    # restart: rewind to cv_done so the new backend re-runs cover_letter from a clean
    # checkpoint). A normal cv_adjust completion goes to cv_review, never directly to
    # cv_done (cv_done is reached only via the approve-cv endpoint — see CLAUDE.md →
    # "Two-lane pipeline / CV gate") — do NOT re-add Stage.cv_adjust here as a
    # "normal completion" case; that was true pre-two-lane-split but is now exactly
    # the gate-bypass recovery_sweep() must not exercise (jsa/db/repo.py).
    # running(revising_*) → cv_done is NOT valid; revisions rewind to review instead.
    if job.state == JobState.running and new_state == JobState.cv_done:
        if job.current_stage not in (Stage.cover_letter,):
            raise InvalidTransition(
                f"Can only reach cv_done from running(cover_letter), got running({job.current_stage})"
            )
    # running → cl_done only valid when current stage is cover_letter
    if job.state == JobState.running and new_state == JobState.cl_done:
        if job.current_stage not in (Stage.cover_letter,):
            raise InvalidTransition(
                f"Can only reach cl_done from running(cover_letter), got running({job.current_stage})"
            )
    # running → cv_review only valid when current stage is cv_adjust or revising_cv
    if job.state == JobState.running and new_state == JobState.cv_review:
        if job.current_stage not in (Stage.cv_adjust, Stage.revising_cv):
            raise InvalidTransition(
                f"Can only reach cv_review from running(cv_adjust/revising_cv), got running({job.current_stage})"
            )
    job.state = new_state
    job.current_stage = new_stage


def set_current_stage(job: "Job", stage: "Stage | None") -> None:
    """Set job.current_stage for revision setup; only valid when state in (review, cv_review)."""
    from jsa.db.models import JobState as _JobState, Stage as StageEnum
    if job.state not in (_JobState.review, _JobState.cv_review):
        raise InvalidTransition(
            f"set_current_stage only valid on review/cv_review jobs, got {job.state!r}"
        )
    if job.state == _JobState.cv_review:
        if stage is not None and stage != StageEnum.revising_cv:
            raise InvalidTransition(f"set_current_stage on cv_review only accepts revising_cv, got {stage!r}")
    elif stage is not None and stage not in (StageEnum.revising_cv, StageEnum.revising_cl):
        raise InvalidTransition(f"set_current_stage on review only accepts revising_cv/revising_cl, got {stage!r}")
    job.current_stage = stage
