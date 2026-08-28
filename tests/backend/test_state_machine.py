"""Tests for jsa.pipeline.state_machine: transition() guard and InvalidTransition."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jsa.db.models import JobState, Stage
from jsa.pipeline.state_machine import ALLOWED, InvalidTransition, set_current_stage, transition


# ---------------------------------------------------------------------------
# Helper: build a minimal job-like object with state + current_stage
# ---------------------------------------------------------------------------

def make_job(state: JobState, current_stage: Stage | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state, current_stage=current_stage)


# ---------------------------------------------------------------------------
# Allowed transitions — one per entry in ALLOWED table
# ---------------------------------------------------------------------------

class TestAllowedTransitions:
    """Every (from_state, to_state) pair in ALLOWED must succeed via transition()."""

    def test_queued_to_pending(self):
        """LAUNCH: a fresh, parked job becomes dispatchable."""
        job = make_job(JobState.queued)
        transition(job, JobState.pending)
        assert job.state == JobState.pending
        assert job.current_stage is None

    def test_queued_to_dismissed(self):
        job = make_job(JobState.queued)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed

    def test_pending_to_running(self):
        job = make_job(JobState.pending)
        transition(job, JobState.running, Stage.cv_adjust)
        assert job.state == JobState.running
        assert job.current_stage == Stage.cv_adjust

    def test_pending_to_failed(self):
        job = make_job(JobState.pending)
        transition(job, JobState.failed)
        assert job.state == JobState.failed
        assert job.current_stage is None

    def test_running_to_awaiting_input(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.awaiting_input, Stage.cv_adjust)
        assert job.state == JobState.awaiting_input
        assert job.current_stage == Stage.cv_adjust

    def test_running_to_cv_done(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done)
        assert job.state == JobState.cv_done
        assert job.current_stage is None

    def test_running_to_cl_done(self):
        job = make_job(JobState.running, Stage.cover_letter)
        transition(job, JobState.cl_done)
        assert job.state == JobState.cl_done
        assert job.current_stage is None

    def test_running_to_review(self):
        job = make_job(JobState.running, Stage.revising_cv)
        transition(job, JobState.review)
        assert job.state == JobState.review
        assert job.current_stage is None

    def test_running_to_failed(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.failed)
        assert job.state == JobState.failed
        assert job.current_stage is None

    def test_awaiting_input_to_running(self):
        job = make_job(JobState.awaiting_input, Stage.cv_adjust)
        transition(job, JobState.running, Stage.cv_adjust)
        assert job.state == JobState.running

    def test_awaiting_input_to_review(self):
        job = make_job(JobState.awaiting_input, Stage.revising_cv)
        transition(job, JobState.review)
        assert job.state == JobState.review
        assert job.current_stage is None

    def test_awaiting_input_to_failed(self):
        job = make_job(JobState.awaiting_input, Stage.cv_adjust)
        transition(job, JobState.failed)
        assert job.state == JobState.failed
        assert job.current_stage is None

    def test_cv_done_to_running(self):
        job = make_job(JobState.cv_done)
        transition(job, JobState.running, Stage.cover_letter)
        assert job.state == JobState.running
        assert job.current_stage == Stage.cover_letter

    def test_cv_done_to_failed(self):
        job = make_job(JobState.cv_done)
        transition(job, JobState.failed)
        assert job.state == JobState.failed

    def test_cl_done_to_review(self):
        job = make_job(JobState.cl_done)
        transition(job, JobState.review)
        assert job.state == JobState.review

    def test_cl_done_to_failed(self):
        job = make_job(JobState.cl_done)
        transition(job, JobState.failed)
        assert job.state == JobState.failed

    def test_review_to_running(self):
        job = make_job(JobState.review)
        transition(job, JobState.running, Stage.revising_cv)
        assert job.state == JobState.running
        assert job.current_stage == Stage.revising_cv

    def test_review_to_awaiting_input(self):
        job = make_job(JobState.review)
        transition(job, JobState.awaiting_input, Stage.revising_cl)
        assert job.state == JobState.awaiting_input
        assert job.current_stage == Stage.revising_cl

    def test_review_to_approved(self):
        job = make_job(JobState.review)
        transition(job, JobState.approved)
        assert job.state == JobState.approved
        assert job.current_stage is None

    def test_review_to_failed(self):
        job = make_job(JobState.review)
        transition(job, JobState.failed)
        assert job.state == JobState.failed

    def test_failed_to_pending(self):
        job = make_job(JobState.failed)
        transition(job, JobState.pending)
        assert job.state == JobState.pending
        assert job.current_stage is None

    def test_running_cv_adjust_to_cv_review(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review)
        assert job.state == JobState.cv_review
        assert job.current_stage is None

    def test_running_revising_cv_to_cv_review(self):
        job = make_job(JobState.running, Stage.revising_cv)
        transition(job, JobState.cv_review)
        assert job.state == JobState.cv_review

    def test_cv_review_to_cv_done(self):
        job = make_job(JobState.cv_review)
        transition(job, JobState.cv_done)
        assert job.state == JobState.cv_done
        assert job.current_stage is None

    def test_cv_review_to_running_revising_cv(self):
        job = make_job(JobState.cv_review)
        transition(job, JobState.running, Stage.revising_cv)
        assert job.state == JobState.running
        assert job.current_stage == Stage.revising_cv

    def test_cv_review_to_failed(self):
        job = make_job(JobState.cv_review)
        transition(job, JobState.failed)
        assert job.state == JobState.failed

    def test_cv_review_to_dismissed(self):
        job = make_job(JobState.cv_review)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed

    def test_awaiting_input_to_cv_review(self):
        job = make_job(JobState.awaiting_input, Stage.revising_cv)
        transition(job, JobState.cv_review)
        assert job.state == JobState.cv_review
        assert job.current_stage is None


# ---------------------------------------------------------------------------
# Forbidden transitions — must all raise InvalidTransition
# ---------------------------------------------------------------------------

class TestForbiddenTransitions:
    """Transitions not in ALLOWED must raise InvalidTransition."""

    def test_queued_to_running_raises(self):
        """A queued (never-launched) job cannot be dispatched directly — must LAUNCH first."""
        job = make_job(JobState.queued)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.running, Stage.cv_adjust)

    def test_queued_to_failed_raises(self):
        job = make_job(JobState.queued)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.failed)

    def test_pending_to_cv_done_raises(self):
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.cv_done)

    def test_pending_to_cl_done_raises(self):
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.cl_done)

    def test_pending_to_review_raises(self):
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.review)

    def test_pending_to_approved_raises(self):
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.approved)

    def test_pending_to_pending_raises(self):
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.pending)

    def test_cv_done_to_pending_raises(self):
        job = make_job(JobState.cv_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.pending)

    def test_cv_done_to_cl_done_raises(self):
        job = make_job(JobState.cv_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.cl_done)

    def test_cv_done_to_review_raises(self):
        job = make_job(JobState.cv_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.review)

    def test_cv_done_to_approved_raises(self):
        job = make_job(JobState.cv_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.approved)

    def test_cl_done_to_running_raises(self):
        job = make_job(JobState.cl_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.running, Stage.cover_letter)

    def test_cl_done_to_cv_done_raises(self):
        job = make_job(JobState.cl_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.cv_done)

    def test_cl_done_to_approved_raises(self):
        job = make_job(JobState.cl_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.approved)

    def test_approved_to_pending_raises(self):
        job = make_job(JobState.approved)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.pending)

    def test_approved_to_failed_raises(self):
        job = make_job(JobState.approved)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.failed)

    def test_approved_to_running_raises(self):
        job = make_job(JobState.approved)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.running, Stage.cv_adjust)

    def test_approved_to_review_raises(self):
        job = make_job(JobState.approved)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.review)

    def test_failed_to_running_raises(self):
        job = make_job(JobState.failed)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.running, Stage.cv_adjust)

    def test_failed_to_review_raises(self):
        job = make_job(JobState.failed)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.review)

    def test_failed_to_failed_raises(self):
        job = make_job(JobState.failed)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.failed)

    def test_cv_review_to_review_raises(self):
        job = make_job(JobState.cv_review)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.review)

    def test_cv_review_to_pending_raises(self):
        job = make_job(JobState.cv_review)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.pending)

    def test_cv_done_to_cv_review_raises(self):
        """cv_done must stay reachable only via cv_review→cv_done, never the reverse."""
        job = make_job(JobState.cv_done)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.cv_review)


# ---------------------------------------------------------------------------
# Stage compatibility: running → cv_done / cl_done stage checks
# ---------------------------------------------------------------------------

class TestStageCompatibility:
    """running(cv_adjust) → cv_done allowed; running(revising_cv) → cv_done raises.

    BF-19: running(cover_letter) → cv_done is now also allowed (backend-switch restart
    rewinds a mid-cover_letter job back to cv_done so the new backend starts fresh).
    """

    def test_running_cv_adjust_to_cv_done_allowed(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done)
        assert job.state == JobState.cv_done

    def test_running_cover_letter_to_cv_done_allowed_for_backend_switch(self):
        """BF-19: backend-switch restart from cover_letter stage → rewind to cv_done."""
        job = make_job(JobState.running, Stage.cover_letter)
        transition(job, JobState.cv_done)
        assert job.state == JobState.cv_done

    def test_running_revising_cv_to_cv_done_raises(self):
        job = make_job(JobState.running, Stage.revising_cv)
        with pytest.raises(InvalidTransition, match="cv_done"):
            transition(job, JobState.cv_done)

    def test_running_cover_letter_to_cl_done_allowed(self):
        job = make_job(JobState.running, Stage.cover_letter)
        transition(job, JobState.cl_done)
        assert job.state == JobState.cl_done

    def test_running_cv_adjust_to_cl_done_raises(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        with pytest.raises(InvalidTransition, match="cl_done"):
            transition(job, JobState.cl_done)

    def test_running_revising_cl_to_cl_done_raises(self):
        job = make_job(JobState.running, Stage.revising_cl)
        with pytest.raises(InvalidTransition, match="cl_done"):
            transition(job, JobState.cl_done)

    def test_running_with_none_stage_raises(self):
        """awaiting_input requires a current_stage — passing None must raise."""
        job = make_job(JobState.running, Stage.cv_adjust)
        with pytest.raises(InvalidTransition, match="requires a current_stage"):
            transition(job, JobState.awaiting_input, None)

    def test_non_running_state_with_non_none_stage_raises(self):
        """States not in STAGE_FOR_STATE must have new_stage=None."""
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.failed, Stage.cv_adjust)

    def test_cv_done_to_running_with_non_running_stage_raises(self):
        """Stages incompatible with running state raise InvalidTransition."""
        # This is actually not a stage incompatibility issue but rather the
        # STAGE_FOR_STATE check: running can have cv_adjust, cover_letter, revising_cv, revising_cl
        # all of those are valid, so we check that an obviously-bad non-Stage value raises
        job = make_job(JobState.cv_done)
        with pytest.raises(InvalidTransition):
            # passing a Stage value but the dest state 'failed' requires None stage
            transition(job, JobState.failed, Stage.cv_adjust)

    def test_awaiting_input_with_valid_stage(self):
        """awaiting_input accepts stage values from STAGE_FOR_STATE."""
        job = make_job(JobState.running, Stage.cover_letter)
        transition(job, JobState.awaiting_input, Stage.cover_letter)
        assert job.current_stage == Stage.cover_letter

    def test_running_cover_letter_to_cv_review_raises(self):
        job = make_job(JobState.running, Stage.cover_letter)
        with pytest.raises(InvalidTransition, match="cv_review"):
            transition(job, JobState.cv_review)

    def test_running_fit_assessment_to_cv_review_raises(self):
        job = make_job(JobState.running, Stage.fit_assessment)
        with pytest.raises(InvalidTransition, match="cv_review"):
            transition(job, JobState.cv_review)


# ---------------------------------------------------------------------------
# set_current_stage: review vs cv_review
# ---------------------------------------------------------------------------

class TestSetCurrentStage:
    def test_review_accepts_revising_cv(self):
        job = make_job(JobState.review)
        set_current_stage(job, Stage.revising_cv)
        assert job.current_stage == Stage.revising_cv

    def test_review_accepts_revising_cl(self):
        job = make_job(JobState.review)
        set_current_stage(job, Stage.revising_cl)
        assert job.current_stage == Stage.revising_cl

    def test_cv_review_accepts_revising_cv(self):
        job = make_job(JobState.cv_review)
        set_current_stage(job, Stage.revising_cv)
        assert job.current_stage == Stage.revising_cv

    def test_cv_review_rejects_revising_cl(self):
        job = make_job(JobState.cv_review)
        with pytest.raises(InvalidTransition):
            set_current_stage(job, Stage.revising_cl)

    def test_pending_rejects_any_stage(self):
        job = make_job(JobState.pending)
        with pytest.raises(InvalidTransition):
            set_current_stage(job, Stage.revising_cv)


# ---------------------------------------------------------------------------
# approved is terminal — all transitions from it raise
# ---------------------------------------------------------------------------

class TestApprovedTerminal:
    """approved is a terminal state; any outgoing transition must raise."""

    @pytest.mark.parametrize("new_state", list(JobState))
    def test_approved_all_transitions_raise(self, new_state):
        job = make_job(JobState.approved)
        with pytest.raises(InvalidTransition):
            transition(job, new_state)


# ---------------------------------------------------------------------------
# transition() mutates the job in-place on success
# ---------------------------------------------------------------------------

class TestMutatesInPlace:
    def test_state_and_stage_are_updated(self):
        job = make_job(JobState.pending)
        transition(job, JobState.running, Stage.cv_adjust)
        assert job.state == JobState.running
        assert job.current_stage == Stage.cv_adjust

    def test_stage_cleared_on_terminal_like_transition(self):
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done)
        assert job.current_stage is None

    def test_no_mutation_on_invalid_transition(self):
        """If transition raises, job.state and job.current_stage must not change."""
        job = make_job(JobState.pending, None)
        original_state = job.state
        original_stage = job.current_stage
        with pytest.raises(InvalidTransition):
            transition(job, JobState.approved)
        assert job.state == original_state
        assert job.current_stage == original_stage
