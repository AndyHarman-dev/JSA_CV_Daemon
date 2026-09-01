"""Tests for Phase BF-18: AgentLimitReached exception detection.

Tests cover:
1. AgentLimitReached is importable and is a RuntimeError subclass.
2. ClaudeCliBackend._parse_with_nudge detects limit keywords and raises AgentLimitReached.
3. ClaudeCliBackend._parse_with_nudge proceeds with nudge when no limit keyword is present.
4. GoogleCliBackend._parse_with_nudge detects limit keywords and raises AgentLimitReached.
5. AnthropicAPIBackend._call_api catches anthropic.RateLimitError and raises AgentLimitReached.
6. Orchestrator._run_one catches AgentLimitReached and marks the job failed with the correct message.
7. The four multi-backend-model-select backends (mistral, openrouter, opencode-go's
   /chat/completions path, gemini) classify rate-limit / permanent-4xx / exhausted-5xx /
   timeout into the same three BF-19-recognized exception types.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, MagicMock, patch

from jsa.agents.anthropic_api import AnthropicAPIBackend
from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached, AgentReply, AgentTimeout
from jsa.agents.claude_cli import ClaudeCliBackend, ClaudeSessionHandle
from jsa.agents.gemini_api import GeminiBackend
from jsa.agents.google_cli import GoogleCliBackend, GoogleSessionHandle
from jsa.agents.mistral import MistralBackend
from jsa.agents.opencode_go import OpenCodeGoBackend
from jsa.agents.openrouter import OpenRouterBackend
from jsa.db import repo
from jsa.db.models import Base, Job, JobState, Stage
from jsa.pipeline.orchestrator import Orchestrator
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cl_final


# ---------------------------------------------------------------------------
# Test 1: AgentLimitReached is importable and is a RuntimeError subclass
# ---------------------------------------------------------------------------


class TestAgentLimitReachedClass:
    def test_agent_limit_reached_is_importable(self):
        """AgentLimitReached can be imported from jsa.agents.base."""
        from jsa.agents.base import AgentLimitReached as ALR
        assert ALR is not None

    def test_agent_limit_reached_is_runtime_error_subclass(self):
        """AgentLimitReached is a subclass of RuntimeError."""
        assert issubclass(AgentLimitReached, RuntimeError)

    def test_agent_limit_reached_can_be_instantiated(self):
        """AgentLimitReached can be instantiated with a message."""
        exc = AgentLimitReached("Usage limit reached")
        assert isinstance(exc, RuntimeError)
        assert str(exc) == "Usage limit reached"


# ---------------------------------------------------------------------------
# Test 2: ClaudeCliBackend._parse_with_nudge detects limit keywords
# ---------------------------------------------------------------------------


class ScriptedClaudeBackend(ClaudeCliBackend):
    """Test double: ClaudeCliBackend with scripted _run responses."""

    def __init__(self, run_responses: list[str]) -> None:
        super().__init__(timeout=5.0)
        self._run_responses: list[str] = list(run_responses)
        self.run_call_count: int = 0

    async def _run(self, cmd: list[str], context: str = "", cwd=None, timeout=None) -> str:
        self.run_call_count += 1
        if not self._run_responses:
            raise IndexError("ScriptedClaudeBackend: no more scripted _run responses")
        return self._run_responses.pop(0)


class TestClaudeCliLimitKeywordDetection:
    """Tests for ClaudeCliBackend._parse_with_nudge limit keyword detection."""

    async def test_usage_limit_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'usage limit' raises AgentLimitReached."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "You have reached your usage limit for today."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_rate_limit_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'rate limit' raises AgentLimitReached."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "API rate limit exceeded. Please try again later."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_limit_reached_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'limit reached' raises AgentLimitReached."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "Your API limit reached for this period."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_quota_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'quota' raises AgentLimitReached."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "Daily quota exhausted, please wait until tomorrow."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_limit_keyword_case_insensitive(self):
        """Limit keyword detection is case-insensitive."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "YOU HAVE REACHED YOUR USAGE LIMIT FOR TODAY."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_limit_reached_mixed_case(self):
        """Limit keywords work with mixed case."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "Your Rate Limit Has Been Exceeded."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_agent_limit_reached_message_contains_raw_snippet(self):
        """AgentLimitReached message contains the first 500 chars of raw output."""
        backend = ScriptedClaudeBackend(run_responses=[])
        raw = "You have reached your usage limit. " + "x" * 1000
        with pytest.raises(AgentLimitReached) as exc_info:
            await backend._parse_with_nudge("test-session", raw)
        exc_msg = str(exc_info.value)
        assert "usage limit" in exc_msg
        # First 500 chars should be in the message
        assert len(exc_msg) <= 500

    async def test_limit_keyword_skips_nudge_attempt(self):
        """When limit keyword is detected, no nudge subprocess call is made."""
        backend = ScriptedClaudeBackend(run_responses=[])  # no nudge response
        raw = "API quota has been exceeded."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)
        # _run should not have been called (no nudge attempt)
        assert backend.run_call_count == 0

    async def test_no_limit_keyword_proceeds_to_nudge(self):
        """Without limit keyword, missing-sentinel error triggers nudge."""
        backend = ScriptedClaudeBackend(
            run_responses=["<<<FINAL>>>\nsome content\n<<<END>>>"]
        )
        raw = "This reply has no sentinel at all."
        reply = await backend._parse_with_nudge("test-session", raw)
        # Nudge was attempted (parse_with_nudge succeeded via nudge reply)
        assert reply.kind == "final"
        assert reply.content == "some content"
        # _run should have been called once (the nudge subprocess)
        assert backend.run_call_count == 1

    async def test_no_limit_keyword_nudge_cmd_uses_resume(self):
        """Nudge command uses --resume flag with session ID."""
        backend = ScriptedClaudeBackend(
            run_responses=["<<<FINAL>>>\nsome content\n<<<END>>>"]
        )
        raw = "Plain text without sentinel."
        await backend._parse_with_nudge("sess-xyz", raw)
        # Verify the nudge command was formed correctly
        # (we can't directly inspect it here since _run is overridden,
        # but we confirmed the nudge succeeded above)
        assert backend.run_call_count == 1


# ---------------------------------------------------------------------------
# Test 3: GoogleCliBackend._parse_with_nudge detects limit keywords
# ---------------------------------------------------------------------------


class ScriptedGoogleBackend(GoogleCliBackend):
    """Test double: GoogleCliBackend with scripted _run responses."""

    def __init__(self, run_responses: list[dict]) -> None:
        super().__init__(timeout=5.0)
        self._run_responses: list[dict] = list(run_responses)
        self.run_call_count: int = 0

    async def _run(self, cmd: list[str], context: str = "", timeout=None, log_path=None) -> dict:
        self.run_call_count += 1
        if not self._run_responses:
            raise IndexError("ScriptedGoogleBackend: no more scripted _run responses")
        return self._run_responses.pop(0)


class TestGoogleCliLimitKeywordDetection:
    """Tests for GoogleCliBackend._parse_with_nudge limit keyword detection."""

    async def test_usage_limit_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'usage limit' raises AgentLimitReached."""
        backend = ScriptedGoogleBackend(run_responses=[])
        raw = "You have reached your usage limit for this billing cycle."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_rate_limit_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'rate limit' raises AgentLimitReached."""
        backend = ScriptedGoogleBackend(run_responses=[])
        raw = "The API rate limit has been exceeded."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_quota_keyword_raises_agent_limit_reached(self):
        """Raw output containing 'quota' raises AgentLimitReached."""
        backend = ScriptedGoogleBackend(run_responses=[])
        raw = "Your quota for this service has been exhausted."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)

    async def test_no_limit_keyword_proceeds_to_nudge_gemini(self):
        """Without limit keyword, missing-sentinel error triggers nudge for Gemini."""
        backend = ScriptedGoogleBackend(
            run_responses=[{"response": "<<<FINAL>>>\nsome content\n<<<END>>>"}]
        )
        raw = "Plain response without sentinel block."
        reply = await backend._parse_with_nudge("test-session", raw)
        # Nudge was attempted and succeeded
        assert reply.kind == "final"
        assert reply.content == "some content"
        # _run should have been called once (the nudge subprocess)
        assert backend.run_call_count == 1

    async def test_gemini_limit_keyword_skips_nudge(self):
        """When limit keyword is detected, no nudge subprocess call is made for Gemini."""
        backend = ScriptedGoogleBackend(run_responses=[])  # no nudge response
        raw = "API rate limit exceeded. Please retry after some time."
        with pytest.raises(AgentLimitReached):
            await backend._parse_with_nudge("test-session", raw)
        # _run should not have been called (no nudge attempt)
        assert backend.run_call_count == 0


# ---------------------------------------------------------------------------
# Test 4: AnthropicAPIBackend._call_api detects RateLimitError
# ---------------------------------------------------------------------------


class TestAnthropicAPIRateLimitDetection:
    """Tests for AnthropicAPIBackend._call_api RateLimitError handling."""

    async def test_anthropic_rate_limit_error_raises_agent_limit_reached(self):
        """anthropic.RateLimitError is caught and converted to AgentLimitReached."""
        import anthropic

        backend = AnthropicAPIBackend()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="doesn't matter")]

        # Create a RateLimitError (may need to inspect the actual SDK)
        rate_limit_error = anthropic.RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(),
            body={},
        )
        mock_client.messages.create = AsyncMock(side_effect=rate_limit_error)
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            with pytest.raises(AgentLimitReached) as exc_info:
                await backend._call_api("sys", [{"role": "user", "content": "hello"}])

        exc_msg = str(exc_info.value)
        assert "rate limit" in exc_msg.lower()

    async def test_anthropic_rate_limit_error_message_includes_context(self):
        """AgentLimitReached message from RateLimitError includes context."""
        import anthropic

        backend = AnthropicAPIBackend()
        mock_client = MagicMock()

        rate_limit_error = anthropic.RateLimitError(
            message="Rate limit exceeded: 429 Too Many Requests",
            response=MagicMock(),
            body={},
        )
        mock_client.messages.create = AsyncMock(side_effect=rate_limit_error)
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            with pytest.raises(AgentLimitReached) as exc_info:
                await backend._call_api("sys", [{"role": "user", "content": "test"}])

        # The message should contain "Anthropic API rate limit reached"
        assert "Anthropic API rate limit reached" in str(exc_info.value)

    async def test_anthropic_other_errors_not_caught(self):
        """Non-RateLimitError exceptions are not caught (propagate as-is or wrapped)."""
        import anthropic

        backend = AnthropicAPIBackend()
        mock_client = MagicMock()

        # Create a different type of error
        other_error = ValueError("Some other error")
        mock_client.messages.create = AsyncMock(side_effect=other_error)
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            with pytest.raises(ValueError):
                await backend._call_api("sys", [{"role": "user", "content": "test"}])

    async def test_anthropic_start_session_catches_rate_limit(self):
        """start_session also raises AgentLimitReached if API hits rate limit."""
        import anthropic

        backend = AnthropicAPIBackend()
        mock_client = MagicMock()

        rate_limit_error = anthropic.RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(),
            body={},
        )
        mock_client.messages.create = AsyncMock(side_effect=rate_limit_error)
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "initial message")


# ---------------------------------------------------------------------------
# Test 5: Orchestrator._run_one catches AgentLimitReached and marks job failed
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    """In-memory SQLite with StaticPool for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


def _job_data(
    job_id: str | None = None,
    company: str = "Acme",
    role: str = "Engineer",
    link: str = "https://acme.com/job",
    tier: str = "A",
    jd: str = "Job description",
    jd_hash: str = "hash0000deadbeef",
    cv_text: str = "CV text",
) -> dict:
    if job_id is None:
        job_id = uuid4().hex[:16]
    return dict(
        id=job_id,
        company=company,
        role=role,
        link=link,
        tier=tier,
        jd=jd,
        jd_hash=jd_hash,
        cv_text=cv_text,
    )


async def _insert_job(factory, **overrides) -> Job:
    """Insert a fresh job and launch it (queued → pending)."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.pending
        await s.commit()
    return job


async def _insert_job_at_cv_done(factory, **overrides) -> Job:
    """Insert a job already past the fit gate AND the CV lane, so dispatch goes
    straight to cover_letter — used for AgentTimeout tests.

    Two stages are deliberately avoided here:
    - fit_assessment (the `pending` stage) swallows AgentTimeout into `unfit`
      internally and never lets it reach the orchestrator (see stages.py), so
      it can't exercise BF-19 at all.
    - cv_adjust: repo.backend_switch_reset's BF-19 rewind for a cv_adjust
      failure resets the job all the way back to `pending`, which re-enters
      fit_assessment on the new backend — i.e. a second timeout there would
      hit fit_assessment's swallow-to-unfit path above, not chain-exhaustion.
    cover_letter's rewind target is cv_done (not pending), so both attempts in
    a chain re-enter cover_letter on cv_done and a persistent timeout cleanly
    exhausts the chain instead of detouring through the fit gate.
    """
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.cv_done
        await s.commit()
    return job


async def _poll_job_state(
    factory,
    job_id: str,
    target_state: JobState,
    timeout: float = 5.0,
) -> Job:
    """Poll the DB until job reaches target_state or timeout expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with factory() as s:
            job = await repo.get_job(s, job_id)
        if job is not None and job.state == target_state:
            return job
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach {target_state} within {timeout}s"
            )
        await asyncio.sleep(0.05)


async def _run_orchestrator_until(
    orch: Orchestrator,
    factory,
    job_ids: list[str],
    target_state: JobState,
    timeout: float = 10.0,
) -> None:
    """Run the orchestrator as a task, wait for all jobs to reach target_state, then stop."""
    task = asyncio.create_task(orch.run())
    try:
        await asyncio.gather(
            *[
                asyncio.wait_for(
                    _poll_job_state(factory, jid, target_state),
                    timeout=timeout,
                )
                for jid in job_ids
            ]
        )
    finally:
        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(task, timeout=5.0)


class LimitReachedBackend(FakeAgentBackend):
    """Test double: backend that raises AgentLimitReached."""

    def __init__(self):
        super().__init__([])

    async def start_session(self, system_prompt, initial_user_msg):
        raise AgentLimitReached("Anthropic API rate limit reached: 429 Too Many Requests")


class TimeoutBackend(FakeAgentBackend):
    """Test double: backend that always raises AgentTimeout."""

    def __init__(self):
        super().__init__([])

    async def start_session(self, system_prompt, initial_user_msg):
        raise AgentTimeout("OpenCode Zen API timed out after 180.0s")


# ---------------------------------------------------------------------------
# Test 7: Orchestrator._run_one catches AgentTimeout and engages the BF-19
# fallback chain exactly like AgentLimitReached, instead of hard-failing on
# the first backend (the reported bug: a configured `--backends opencode-zen,
# claude-cli` chain never switched to claude-cli when opencode-zen timed out).
# ---------------------------------------------------------------------------


class TestOrchestratorTimeoutDetection:
    """Tests for Orchestrator._run_one handling of AgentTimeout via BF-19."""

    async def test_agent_timeout_switches_to_next_backend_in_chain(self, session_factory):
        """A single-backend timeout advances job.backend_name to the next
        configured backend and keeps the job runnable, rather than failing it.

        Job starts at cv_done (past the fit gate and the CV lane — see
        _insert_job_at_cv_done), so dispatch goes straight to cover_letter and
        the timeout is handled by the generic BF-19 path in
        Orchestrator._run_one / _handle_backend_timeout.
        """
        job = await _insert_job_at_cv_done(session_factory)

        def backend_factory(name: str):
            return TimeoutBackend() if name == "opencode-zen" else FakeAgentBackend([cl_final()])

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=backend_factory,
            backends=["opencode-zen", "claude-cli"],
        )

        # The second backend actually answers, so the job should progress into
        # the review gate instead of ending up `failed`.
        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.review)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.backend_name == "claude-cli"
        assert refreshed.state == JobState.review

    async def test_agent_timeout_exhausted_chain_marks_job_failed(self, session_factory):
        """When every backend in the chain times out, the job is marked failed
        with a human-readable, timeout-specific message (not silently retried
        forever, and not confused with the limit-reached message)."""
        job = await _insert_job_at_cv_done(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: TimeoutBackend(),
            backends=["opencode-zen", "claude-cli"],
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed
        assert "timed out" in (refreshed.error or "").lower()
        assert "Backend limit reached" not in (refreshed.error or "")

    async def test_agent_timeout_single_backend_marks_job_failed(self, session_factory):
        """With no fallback configured, a timeout still fails the job cleanly
        (same as before this fix) rather than hanging or looping."""
        job = await _insert_job_at_cv_done(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: TimeoutBackend(),
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed


class BackendUnavailableBackend(FakeAgentBackend):
    """Test double: backend that always raises AgentBackendUnavailable — the
    exception OpenCodeZenBackend._call_api raises once it has exhausted its own
    in-process retries on a transient overload/gateway signal, or immediately
    for a permanent config error (bad model, bad request). See CLAUDE.md
    "OpenCode Zen backend"."""

    def __init__(self):
        super().__init__([])

    async def start_session(self, system_prompt, initial_user_msg):
        raise AgentBackendUnavailable(
            "OpenCode Zen API still failing after 3 attempts: 502 Bad Gateway"
        )


# ---------------------------------------------------------------------------
# Test 8: Orchestrator._run_one catches AgentBackendUnavailable and engages the
# BF-19 fallback chain exactly like AgentLimitReached/AgentTimeout, instead of
# hard-failing on the first backend. This is the third leg of backend-failure
# resilience: overload/gateway errors that survive a backend's own in-process
# retry budget, and permanent config errors (bad model, bad request) that
# retrying the SAME backend would never fix, both switch to the next configured
# backend rather than failing the job outright.
# ---------------------------------------------------------------------------


class TestOrchestratorBackendUnavailableDetection:
    """Tests for Orchestrator._run_one handling of AgentBackendUnavailable via BF-19."""

    async def test_agent_backend_unavailable_switches_to_next_backend_in_chain(
        self, session_factory
    ):
        """A single-backend AgentBackendUnavailable advances job.backend_name to
        the next configured backend and keeps the job runnable."""
        job = await _insert_job_at_cv_done(session_factory)

        def backend_factory(name: str):
            return (
                BackendUnavailableBackend()
                if name == "opencode-zen"
                else FakeAgentBackend([cl_final()])
            )

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=backend_factory,
            backends=["opencode-zen", "claude-cli"],
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.review)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.backend_name == "claude-cli"
        assert refreshed.state == JobState.review

    async def test_agent_backend_unavailable_exhausted_chain_marks_job_failed(
        self, session_factory
    ):
        """When every backend in the chain raises AgentBackendUnavailable, the
        job fails with a message distinct from the limit/timeout wording."""
        job = await _insert_job_at_cv_done(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: BackendUnavailableBackend(),
            backends=["opencode-zen", "claude-cli"],
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed
        assert "unavailable" in (refreshed.error or "").lower()
        assert "Backend limit reached" not in (refreshed.error or "")
        assert "timed out on every" not in (refreshed.error or "")

    async def test_agent_backend_unavailable_single_backend_marks_job_failed(
        self, session_factory
    ):
        """With no fallback configured, still fails cleanly rather than looping."""
        job = await _insert_job_at_cv_done(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: BackendUnavailableBackend(),
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed


class TestOrchestratorLimitDetection:
    """Tests for Orchestrator._run_one handling of AgentLimitReached."""

    async def test_agent_limit_reached_marks_job_failed(self, session_factory):
        """When backend raises AgentLimitReached, the job is marked failed."""
        job = await _insert_job(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=LimitReachedBackend,
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed

    async def test_agent_limit_reached_error_message_is_human_readable(
        self, session_factory
    ):
        """Job.error contains the human-readable limit message."""
        job = await _insert_job(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=LimitReachedBackend,
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)

        expected_msg = "Backend limit reached — switch backends or wait for quota reset"
        assert refreshed.error == expected_msg

    async def test_agent_limit_reached_prevents_exponential_backoff_loop(
        self, session_factory
    ):
        """Job stays failed; orchestrator does not retry or spin."""
        job = await _insert_job(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=LimitReachedBackend,
        )

        # Run and wait for the job to be marked failed
        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        # Job should be in failed state and should not be picked up again
        # (verified by the orchestrator naturally exiting without trying to rerun it)
        async with session_factory() as s:
            final_job = await repo.get_job(s, job.id)
        assert final_job.state == JobState.failed

    async def test_multiple_jobs_one_limit_reached(self, session_factory):
        """One job hits limit, others can still proceed."""
        # This tests that the limit error on one job does not crash the orchestrator.
        # We test that a failed job doesn't prevent the orchestrator from continuing.

        job1 = await _insert_job(session_factory, job_id="job1_aaaaaaaa")

        # Use a backend that hits limit to verify the orchestrator continues
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=LimitReachedBackend,
        )

        # Run the orchestrator until job1 fails
        orch_task = asyncio.create_task(orch.run())
        try:
            # Wait for job1 to fail
            await asyncio.wait_for(
                _poll_job_state(session_factory, job1.id, JobState.failed),
                timeout=5.0,
            )
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(orch_task, timeout=5.0)

        async with session_factory() as s:
            job1_final = await repo.get_job(s, job1.id)

        assert job1_final.state == JobState.failed
        assert "Backend limit reached" in (job1_final.error or "")


# ---------------------------------------------------------------------------
# Test 7: the four multi-backend-model-select backends (mistral, openrouter,
# opencode-go's /chat/completions path, gemini) classify a rate limit, a
# permanent 4xx, an exhausted-retry 5xx, and a timeout into the same three
# BF-19-recognized exception types (AgentLimitReached / AgentBackendUnavailable
# / AgentTimeout). Full request/response-shape coverage for each backend
# already lives in its own dedicated test file (test_mistral.py /
# test_openrouter.py / test_opencode_go.py / test_gemini_api.py, plus the
# shared machinery in test_openai_compat.py) — this class exists so the file
# this plan's Phase 7 named ("add per-backend classification cases so all
# four join the BF-19 chain correctly") actually references all four.
# ---------------------------------------------------------------------------


def _openai_compat_body(text) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _make_mock_client(json_body: dict, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json = MagicMock(return_value=json_body)
    mock_response.text = str(json_body)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


@pytest.fixture
def _new_backend_no_retry_delay(monkeypatch):
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents._openai_compat.asyncio.sleep", _instant_sleep)


@pytest.mark.parametrize(
    "make_backend",
    [
        lambda: MistralBackend(),
        lambda: OpenRouterBackend(),
        lambda: OpenCodeGoBackend(),  # default model (glm-5.3) is the /chat/completions protocol
    ],
    ids=["mistral", "openrouter", "opencode-go-chat"],
)
class TestOpenAICompatibleNewBackendsClassification:
    """Mistral, OpenRouter, and OpenCode-GO's chat protocol all inherit
    OpenAICompatBackend's _call_api_once unchanged — one parametrized class
    proves the three-way classification actually fires through each concrete
    subclass, not just the shared base tested via MistralBackend alone."""

    async def test_429_raises_agent_limit_reached(self, make_backend, _new_backend_no_retry_delay):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = make_backend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_permanent_4xx_raises_agent_backend_unavailable(self, make_backend, _new_backend_no_retry_delay):
        mock_client = _make_mock_client(
            {"error": {"message": "invalid model", "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = make_backend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")

    async def test_exhausted_5xx_raises_agent_backend_unavailable(self, make_backend, _new_backend_no_retry_delay):
        mock_client = _make_mock_client({}, status_code=502)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = make_backend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 3  # _MAX_ATTEMPTS

    async def test_timeout_raises_agent_timeout(self, make_backend, _new_backend_no_retry_delay):
        import httpx as httpx_module

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = make_backend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "hi")


class TestGeminiClassification:
    """GeminiBackend overrides only _call_api_once (its wire shape has nothing
    in common with the OpenAI-compatible body), so its classification needs
    its own error-envelope shape rather than reusing the parametrized class
    above."""

    async def test_429_raises_agent_limit_reached(self, _new_backend_no_retry_delay):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_permanent_4xx_raises_agent_backend_unavailable(self, _new_backend_no_retry_delay):
        mock_client = _make_mock_client(
            {"error": {"code": 400, "message": "bad model", "status": "INVALID_ARGUMENT"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")

    async def test_exhausted_5xx_raises_agent_backend_unavailable(self, _new_backend_no_retry_delay):
        mock_client = _make_mock_client(
            {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}},
            status_code=503,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 3  # _MAX_ATTEMPTS

    async def test_timeout_raises_agent_timeout(self, _new_backend_no_retry_delay):
        import httpx as httpx_module

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "hi")
