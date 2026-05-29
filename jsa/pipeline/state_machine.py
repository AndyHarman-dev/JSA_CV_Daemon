"""Allowed state transitions table and transition() guard."""

from jsa.db.models import JobState, Stage


class InvalidTransition(Exception): ...


ALLOWED = {
    JobState.pending:        {JobState.running, JobState.failed, JobState.dismissed},
    JobState.running:        {JobState.awaiting_input, JobState.cv_done, JobState.cl_done, JobState.review, JobState.failed, JobState.pending, JobState.dismissed},
    JobState.awaiting_input: {JobState.running, JobState.review, JobState.failed, JobState.dismissed},
    JobState.cv_done:        {JobState.running, JobState.failed, JobState.dismissed},
    JobState.cl_done:        {JobState.review, JobState.failed, JobState.dismissed},
    JobState.review:         {JobState.running, JobState.awaiting_input, JobState.approved, JobState.failed, JobState.dismissed},
    JobState.approved:       set(),
    JobState.failed:         {JobState.pending, JobState.dismissed, JobState.cv_done},
    JobState.dismissed:      {JobState.pending},
}

# Stage compatibility: which stages are valid for each state
STAGE_FOR_STATE = {
    JobState.running: {Stage.cv_adjust, Stage.cover_letter, Stage.revising_cv, Stage.revising_cl},
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
    # running → cv_done only valid when current stage is cv_adjust
    if job.state == JobState.running and new_state == JobState.cv_done:
        if job.current_stage not in (Stage.cv_adjust,):
            raise InvalidTransition(
                f"Can only reach cv_done from running(cv_adjust), got running({job.current_stage})"
            )
    # running → cl_done only valid when current stage is cover_letter
    if job.state == JobState.running and new_state == JobState.cl_done:
        if job.current_stage not in (Stage.cover_letter,):
            raise InvalidTransition(
                f"Can only reach cl_done from running(cover_letter), got running({job.current_stage})"
            )
    job.state = new_state
    job.current_stage = new_stage


def set_current_stage(job: "Job", stage: "Stage | None") -> None:
    """Set job.current_stage for revision setup; only valid when state==review."""
    from jsa.db.models import JobState as _JobState, Stage as StageEnum
    if job.state != _JobState.review:
        raise InvalidTransition(
            f"set_current_stage only valid on review jobs, got {job.state!r}"
        )
    if stage is not None and stage not in (StageEnum.revising_cv, StageEnum.revising_cl):
        raise InvalidTransition(f"set_current_stage on review only accepts revising_cv/revising_cl, got {stage!r}")
    job.current_stage = stage
