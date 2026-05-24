"""Tests for tests/backend/fakes/fake_backend.py: FakeAgentBackend behaviour.

Verifies the scripted-reply contract used by all orchestrator and pipeline tests.
"""

from __future__ import annotations

import pytest

from jsa.agents.base import AgentReply, HistoryTurn
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle


# ---------------------------------------------------------------------------
# Helpers: pre-built AgentReply instances
# ---------------------------------------------------------------------------

def _final(content: str = "result") -> AgentReply:
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
        question=None,
    )


def _needs_input(question: str = "What is your role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


# ---------------------------------------------------------------------------
# 1 — Scripted FINAL sequence
# ---------------------------------------------------------------------------

class TestScriptedFinalSequence:
    async def test_start_session_returns_first_reply(self):
        r1 = _final("First CV content")
        r2 = _final("Second CL content")
        backend = FakeAgentBackend([r1, r2])

        handle, reply = await backend.start_session("system", "user msg")
        assert reply is r1

    async def test_send_message_returns_second_reply(self):
        r1 = _final("First CV content")
        r2 = _final("Second CL content")
        backend = FakeAgentBackend([r1, r2])

        handle, _ = await backend.start_session("system", "user msg")
        reply = await backend.send_message(handle, "next message")
        assert reply is r2

    async def test_start_session_returns_a_handle(self):
        backend = FakeAgentBackend([_final()])
        handle, _ = await backend.start_session("system", "user msg")
        assert isinstance(handle, FakeSessionHandle)

    async def test_replies_consumed_in_order(self):
        replies = [_final(f"content {i}") for i in range(3)]
        backend = FakeAgentBackend(replies)

        handle, r0 = await backend.start_session("system", "user msg")
        r1 = await backend.send_message(handle, "msg 1")
        r2 = await backend.send_message(handle, "msg 2")

        assert r0.content == "content 0"
        assert r1.content == "content 1"
        assert r2.content == "content 2"


# ---------------------------------------------------------------------------
# 2 — Scripted NEED_INPUT reply
# ---------------------------------------------------------------------------

class TestScriptedNeedInputReply:
    async def test_start_session_returns_needs_input_reply(self):
        q = "What industries interest you?"
        backend = FakeAgentBackend([_needs_input(q)])
        _, reply = await backend.start_session("system", "user msg")
        assert reply.kind == "needs_input"
        assert reply.question == q

    async def test_send_message_returns_needs_input(self):
        backend = FakeAgentBackend([_final("cv"), _needs_input("What role?")])
        handle, _ = await backend.start_session("system", "user msg")
        reply = await backend.send_message(handle, "continue")
        assert reply.kind == "needs_input"
        assert reply.question == "What role?"

    async def test_needs_input_content_equals_question(self):
        q = "Describe your preferred stack."
        backend = FakeAgentBackend([_needs_input(q)])
        _, reply = await backend.start_session("system", "user msg")
        assert reply.content == q
        assert reply.question == q


# ---------------------------------------------------------------------------
# 3 — Exhaustion raises IndexError
# ---------------------------------------------------------------------------

class TestExhaustion:
    async def test_send_message_after_exhaustion_raises_index_error(self):
        backend = FakeAgentBackend([_final()])
        handle, _ = await backend.start_session("system", "user msg")
        # start_session consumed the only reply; next send_message should raise
        with pytest.raises(IndexError, match="no more scripted replies"):
            await backend.send_message(handle, "another message")

    async def test_start_session_on_empty_raises_index_error(self):
        backend = FakeAgentBackend([])
        with pytest.raises(IndexError, match="no more scripted replies"):
            await backend.start_session("system", "user msg")

    async def test_exact_exhaustion_at_boundary(self):
        backend = FakeAgentBackend([_final("only reply")])
        handle, reply = await backend.start_session("system", "msg")
        assert reply.content == "only reply"
        # Now exhausted
        with pytest.raises(IndexError):
            await backend.send_message(handle, "one more")


# ---------------------------------------------------------------------------
# 4 — restore_session is a no-op (no reply consumed)
# ---------------------------------------------------------------------------

class TestRestoreSession:
    async def test_restore_session_returns_handle(self):
        backend = FakeAgentBackend([_final()])
        history = [HistoryTurn(role="user", content="initial msg")]
        handle = await backend.restore_session("system", history, external_id=None)
        assert isinstance(handle, FakeSessionHandle)

    async def test_restore_session_does_not_consume_reply(self):
        r1 = _final("the result")
        backend = FakeAgentBackend([r1])
        history = [HistoryTurn(role="user", content="initial msg")]

        # restore_session should NOT consume the scripted reply
        handle = await backend.restore_session("system", history, external_id=None)

        # send_message should still return r1
        reply = await backend.send_message(handle, "user answer")
        assert reply is r1

    async def test_restore_session_followed_by_two_sends(self):
        r1 = _final("first")
        r2 = _final("second")
        backend = FakeAgentBackend([r1, r2])

        history = [HistoryTurn(role="user", content="msg")]
        handle = await backend.restore_session("system", history, external_id=None)

        reply1 = await backend.send_message(handle, "answer 1")
        reply2 = await backend.send_message(handle, "answer 2")
        assert reply1.content == "first"
        assert reply2.content == "second"

    async def test_restore_session_preserves_external_id(self):
        backend = FakeAgentBackend([_final()])
        handle = await backend.restore_session("system", [], external_id="sess-abc-123")
        assert handle.external_id == "sess-abc-123"

    async def test_restore_session_with_none_external_id(self):
        backend = FakeAgentBackend([_final()])
        handle = await backend.restore_session("system", [], external_id=None)
        assert handle.external_id is None


# ---------------------------------------------------------------------------
# 5 — end_session is a no-op (does not raise)
# ---------------------------------------------------------------------------

class TestEndSession:
    async def test_end_session_does_not_raise(self):
        backend = FakeAgentBackend([_final()])
        handle, _ = await backend.start_session("system", "user msg")
        # Should complete without raising
        await backend.end_session(handle)

    async def test_end_session_on_restored_handle_does_not_raise(self):
        backend = FakeAgentBackend([_final()])
        handle = await backend.restore_session("system", [], external_id=None)
        await backend.end_session(handle)

    async def test_replies_still_available_after_end_session(self):
        """end_session is a no-op and does not affect remaining scripted replies."""
        backend = FakeAgentBackend([_final("cv"), _final("cl")])
        handle, r1 = await backend.start_session("system", "user msg")
        await backend.end_session(handle)
        # Even after ending the session, remaining replies are still consumable
        # (backend doesn't track whether a session was ended)
        r2 = await backend.send_message(handle, "next")
        assert r2.content == "cl"


# ---------------------------------------------------------------------------
# 6 — handle.id uniqueness
# ---------------------------------------------------------------------------

class TestHandleId:
    async def test_handle_has_non_empty_id(self):
        backend = FakeAgentBackend([_final()])
        handle, _ = await backend.start_session("system", "msg")
        assert handle.id
        assert isinstance(handle.id, str)
        assert len(handle.id) > 0

    async def test_two_start_session_handles_have_distinct_ids(self):
        backend = FakeAgentBackend([_final(), _final()])
        handle1, _ = await backend.start_session("system", "msg")
        handle2, _ = await backend.start_session("system", "msg")
        assert handle1.id != handle2.id

    async def test_restore_session_handle_has_non_empty_id(self):
        backend = FakeAgentBackend([])
        handle = await backend.restore_session("system", [], external_id=None)
        assert handle.id
        assert isinstance(handle.id, str)

    async def test_start_and_restore_handles_have_distinct_ids(self):
        backend = FakeAgentBackend([_final()])
        h_start, _ = await backend.start_session("system", "msg")
        h_restore = await backend.restore_session("system", [], external_id=None)
        assert h_start.id != h_restore.id
