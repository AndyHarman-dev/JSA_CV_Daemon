"""jsa/pipeline/tool_loop.py — the bounded tool-patching loop.

Revision-tool-use plan, Phase 2. Covers: the native/prompt ladder (entry, the one
native→prompt downgrade, giving up to rung 3), the 10-call budget (D1), the
"stop at the first terminal tool this round, every call after gets not_executed"
rule, a failed finalize NOT ending the turn (the model can keep working within the
same budget), a successful finalize/ask_user synthesizing the right AgentReply shape,
D8 (ask_user discards any mutations made earlier in the SAME round), and D3's
mechanical stage-scoping via tools_for.
"""

from __future__ import annotations

import json

import pytest

from jsa.agents.base import AgentReply, ToolCall, ToolsUnsupported
from jsa.agents.tool_spec import tools_for
from jsa.db.models import Job, Stage
from jsa.pipeline.tool_loop import TOOL_BUDGET, run_tool_loop
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument
from tests.backend.fakes.fake_backend import FakeAgentBackend

# --- fixtures ------------------------------------------------------------------------

_CV = CVDocument.model_validate({
    "contact": {"name": "Jane Doe", "email": "jane@example.com"},
    "sections": [
        {"name": "Summary", "text": "Experienced engineer."},
        {
            "name": "Experience",
            "entries": [
                {"heading": "Engineer", "subheading": "Acme", "bullets": ["Did X"]},
            ],
        },
    ],
})

_CL = CoverLetter.model_validate({
    "paragraphs": [
        "I am excited to apply for the Engineer role at Acme, where my five years of "
        "backend experience building resilient distributed systems would let me "
        "contribute from day one.",
        "Thank you for your consideration; I would welcome the chance to discuss how "
        "my background lines up with your team's roadmap.",
    ],
})


def _job() -> Job:
    return Job(id="j1", company="Acme", role="Engineer", link="l", tier="A", jd="jd", jd_hash="h")


def _native_backend(replies: list[AgentReply]) -> FakeAgentBackend:
    return FakeAgentBackend(replies, supports_native_tools=True)


def _prompt_backend(replies: list[AgentReply]) -> FakeAgentBackend:
    return FakeAgentBackend(replies, supports_native_tools=False)


def _build_system_prompt(mode: str) -> str:
    return f"system prompt for {mode}"


def _tool_calls_reply(calls: list[tuple[str, dict]]) -> AgentReply:
    """A native-shaped tool_calls reply — bypasses protocol.py entirely (real native
    backends synthesize AgentReply themselves; see tool_spec.py/Phase 3)."""
    tool_calls = [
        ToolCall(id=f"call_{i}", name=name, arguments=args)
        for i, (name, args) in enumerate(calls)
    ]
    return AgentReply(raw="native", content="native", kind="tool_calls", tool_calls=tool_calls)


def _prose_reply(text: str = "some prose") -> AgentReply:
    return AgentReply(raw=text, content=text, kind="final")


async def _run_cv(backend, instruction="Tighten the summary.", history=None):
    return await run_tool_loop(
        backend=backend,
        stage=Stage.revising_cv,
        job=_job(),
        document=_CV,
        instruction=instruction,
        history=history or [],
        external_id=None,
        build_system_prompt=_build_system_prompt,
    )


async def _run_cl(backend, instruction="Shorten it.", history=None):
    return await run_tool_loop(
        backend=backend,
        stage=Stage.revising_cl,
        job=_job(),
        document=_CL,
        instruction=instruction,
        history=history or [],
        external_id=None,
        build_system_prompt=_build_system_prompt,
    )


# --- D3: mechanical stage scoping ------------------------------------------------------


class TestStageScoping:
    async def test_non_revision_stage_raises(self):
        backend = _native_backend([])
        with pytest.raises(ValueError):
            await run_tool_loop(
                backend=backend,
                stage=Stage.cv_adjust,
                job=_job(),
                document=_CV,
                instruction="x",
                history=[],
                external_id=None,
                build_system_prompt=_build_system_prompt,
            )


# --- Ladder: entry rung selection -------------------------------------------------------


class TestLadderEntry:
    async def test_native_capable_backend_enters_native_and_attaches_tools(self):
        backend = _native_backend([
            _tool_calls_reply([("finalize", {"change_log": "tightened summary"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.mode == "native"
        assert backend.received_tools[0] == tools_for(Stage.revising_cv)

    async def test_non_native_backend_enters_prompt_with_no_tools_kwarg(self):
        backend = _prompt_backend([
            _tool_calls_reply([("finalize", {"change_log": "tightened summary"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.mode == "prompt"
        assert backend.received_tools[0] is None


# --- Ladder: native -> prompt downgrade -------------------------------------------------


class TestNativeToPromptDowngrade:
    async def test_no_parseable_tool_call_on_native_downgrades_to_prompt(self):
        backend = _native_backend([
            _prose_reply(),  # native rung: not a tool_calls reply
            _tool_calls_reply([("finalize", {"change_log": "done"})]),  # prompt rung succeeds
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.mode == "prompt"
        # Two restore_session calls: native attempt, then the prompt-rung rebuild.
        assert len(backend.received_tools) == 2
        assert backend.received_tools[0] is not None  # native attempt attached tools
        assert backend.received_tools[1] is None      # prompt-rung rebuild attached none

    async def test_tools_unsupported_on_native_downgrades_to_prompt(self):
        class _RejectsNativeOnce(FakeAgentBackend):
            async def restore_session(self, system_prompt, history, external_id, **kwargs):
                if kwargs.get("tools") is not None:
                    raise ToolsUnsupported("native tools rejected for this request")
                return await super().restore_session(system_prompt, history, external_id, **kwargs)

        backend = _RejectsNativeOnce(
            [_tool_calls_reply([("finalize", {"change_log": "done"})])],
            supports_native_tools=True,
        )
        result = await _run_cv(backend)
        assert result is not None
        assert result.mode == "prompt"

    async def test_downgrade_resends_instruction_verbatim_not_the_cv(self):
        sent: list[str] = []

        class _RecordingBackend(FakeAgentBackend):
            async def send_message(self, handle, text, **kwargs):
                sent.append(text)
                return await super().send_message(handle, text, **kwargs)

        backend = _RecordingBackend(
            [_prose_reply(), _tool_calls_reply([("finalize", {"change_log": "done"})])],
            supports_native_tools=True,
        )
        await _run_cv(backend, instruction="Tighten the summary.")
        assert sent == ["Tighten the summary.", "Tighten the summary."]

    async def test_both_rungs_failing_returns_none(self):
        backend = _native_backend([_prose_reply(), _prose_reply()])
        result = await _run_cv(backend)
        assert result is None

    async def test_prompt_only_backend_never_gets_a_second_attempt(self):
        """A backend that never claims native support gets exactly one restore_session
        call even when its (only) reply is unparseable — 'one attempt per rung' (D5's
        neighbor rule), not a retry loop."""
        backend = _prompt_backend([_prose_reply()])
        result = await _run_cl(backend)
        assert result is None
        assert len(backend.received_tools) == 1


# --- Budget + terminal-tool array-order semantics --------------------------------------


class TestBudgetAndTerminalOrdering:
    async def test_calls_after_terminal_in_same_array_are_not_executed(self):
        backend = _native_backend([
            _tool_calls_reply([
                ("finalize", {"change_log": "done"}),
                ("replace_summary", {"text": "should not run"}),
            ]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.tool_calls[0]["result"]["ok"] is True
        assert result.tool_calls[1]["result"]["error"]["code"] == "not_executed"

    async def test_budget_counts_individual_calls_not_round_trips(self):
        # 11 no-op get_cv calls in ONE array: the 11th (budget started at 10) must be
        # budget_exhausted, and since no terminal tool ever ran, the whole turn gives
        # up (downgrades to rung 3) rather than looping forever.
        calls = [("get_cv", {}) for _ in range(TOOL_BUDGET + 1)]
        backend = _native_backend([_tool_calls_reply(calls)])
        result = await _run_cv(backend)
        assert result is None

    async def test_exactly_budget_calls_with_no_terminal_gives_up_after_that_round(self):
        # TOOL_BUDGET get_cv calls exactly exhaust the budget within round 1, with no
        # terminal tool called — the loop gives up right there (returns None) without
        # ever requesting a second round from the backend.
        first_round = [("get_cv", {}) for _ in range(TOOL_BUDGET)]
        backend = _native_backend([
            _tool_calls_reply(first_round),
            _tool_calls_reply([("finalize", {"change_log": "done"})]),  # never reached
        ])
        result = await _run_cv(backend)
        assert result is None
        assert backend.received_tool_results == []  # no second round was requested

    async def test_terminal_tool_arriving_exactly_at_budget_zero_does_not_execute(self):
        # finalize is the 11th call in ONE array, right after TOOL_BUDGET get_cv calls
        # exhaust the budget within the SAME round. Per the pseudocode's order
        # ("terminal? -> not_executed; budget==0 -> budget_exhausted"), the budget
        # check is evaluated before dispatch regardless of the tool name — finalize
        # gets budget_exhausted (not not_executed, since no terminal tool ran yet to
        # set that), never executes, and the turn is abandoned (no terminal reached).
        from jsa.events.bus import bus

        queue = bus.subscribe()
        try:
            calls = [("get_cv", {}) for _ in range(TOOL_BUDGET)] + [
                ("finalize", {"change_log": "should not run"})
            ]
            backend = _native_backend([_tool_calls_reply(calls)])
            result = await _run_cv(backend)
            assert result is None
            events = [queue.get_nowait() for _ in range(queue.qsize())]
            tool_events = [e for e in events if e["type"] == "agent_tool"]
            assert tool_events[-1]["name"] == "finalize"
            assert tool_events[-1]["status"] == "budget_exhausted"
        finally:
            bus.unsubscribe(queue)

    async def test_budget_exhausted_status_reported_via_events(self):
        from jsa.events.bus import bus

        queue = bus.subscribe()
        try:
            # TOOL_BUDGET+1 calls in ONE array: the (TOOL_BUDGET+1)th call hits budget
            # 0 mid-round and must be reported as budget_exhausted, not executed.
            calls = [("get_cv", {}) for _ in range(TOOL_BUDGET + 1)]
            backend = _native_backend([_tool_calls_reply(calls)])
            result = await _run_cv(backend)
            assert result is None
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            tool_events = [e for e in events if e["type"] == "agent_tool"]
            assert len(tool_events) == TOOL_BUDGET + 1
            assert [e["status"] for e in tool_events[:-1]] == ["ok"] * TOOL_BUDGET
            assert tool_events[-1]["status"] == "budget_exhausted"
        finally:
            bus.unsubscribe(queue)

    async def test_multi_round_trip_consumes_send_tool_results(self):
        backend = _native_backend([
            _tool_calls_reply([("get_cv", {})]),
            _tool_calls_reply([("finalize", {"change_log": "done"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert len(backend.received_tool_results) == 1  # one round-trip after the first turn
        assert len(result.tool_calls) == 2


# --- finalize semantics -----------------------------------------------------------------


class TestFinalize:
    async def test_successful_finalize_ends_the_loop_with_a_final_reply(self):
        backend = _native_backend([
            _tool_calls_reply([("finalize", {"change_log": "tightened the summary"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.reply.kind == "final"
        payload = json.loads(result.reply.content)
        assert payload["contact"]["name"] == "Jane Doe"

    async def test_finalize_raw_is_canonical_union_form(self):
        backend = _native_backend([
            _tool_calls_reply([("finalize", {"change_log": "x"})]),
        ])
        result = await _run_cv(backend)
        raw = json.loads(result.reply.raw)
        assert raw["kind"] == "final"
        assert raw["question"] is None
        assert isinstance(raw["payload"], dict)

    async def test_failed_finalize_does_not_end_the_turn(self):
        # Empty out every section (_CV has s1=Summary, s2=Experience) so the document
        # fails _validate_final_content's "at least one renderable section" gate. The
        # model should get the error back and be able to keep working.
        empty_section = {"name": "X", "text": None, "items": [], "entries": []}
        backend = _native_backend([
            _tool_calls_reply([
                ("replace_section", {"section_id": "s1", "section": empty_section}),
                ("replace_section", {"section_id": "s2", "section": empty_section}),
                ("finalize", {"change_log": "oops"}),
            ]),
            _tool_calls_reply([
                ("replace_summary", {"text": "Restored a real summary."}),
                ("finalize", {"change_log": "fixed it"}),
            ]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.reply.kind == "final"
        # First round: the failed finalize is present as an error result, not terminal.
        first_round_finalize = result.tool_calls[2]
        assert first_round_finalize["name"] == "finalize"
        assert first_round_finalize["result"]["ok"] is False
        assert first_round_finalize["result"]["error"]["code"] == "validation_failed"
        # The error message must be tool-mode wording, NOT either sentinel-mode/
        # structured-mode built-in phrasing ("<<<FINAL>>>" / "structured reply's
        # `payload`") — those would be actively confusing mid-tool-session, since
        # there is no sentinel block or structured payload to re-emit here.
        message = first_round_finalize["result"]["error"]["message"]
        assert "<<<FINAL>>>" not in message
        assert "structured reply" not in message
        assert "call finalize again" in message
        # And the loop DID send results back for another round (not abandoned).
        assert len(backend.received_tool_results) == 1

    async def test_change_log_is_not_in_the_final_payload(self):
        backend = _native_backend([
            _tool_calls_reply([("finalize", {"change_log": "secret internal note"})]),
        ])
        result = await _run_cv(backend)
        assert "secret internal note" not in result.reply.content
        assert "secret internal note" not in result.reply.raw


# --- ask_user (D8) -----------------------------------------------------------------------


class TestAskUser:
    async def test_ask_user_ends_the_loop_with_needs_input(self):
        backend = _native_backend([
            _tool_calls_reply([("ask_user", {"question": "Which dates should I use?"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.reply.kind == "needs_input"
        assert result.reply.question == "Which dates should I use?"

    async def test_ask_user_with_suggested_replies(self):
        backend = _native_backend([
            _tool_calls_reply([(
                "ask_user",
                {"question": "Pick one", "suggested_replies": ["A", "B"]},
            )]),
        ])
        result = await _run_cv(backend)
        assert result.reply.suggested_replies == ["A", "B"]

    async def test_ask_user_with_missing_question_is_a_recoverable_error(self):
        backend = _native_backend([
            _tool_calls_reply([("ask_user", {})]),
            _tool_calls_reply([("finalize", {"change_log": "done"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.reply.kind == "final"
        assert result.tool_calls[0]["result"]["error"]["code"] == "bad_argument"

    async def test_mutations_before_ask_user_are_discarded_not_persisted(self):
        # replace_summary succeeds, then ask_user fires — the mutation must not appear
        # anywhere in the synthesized reply (which has no payload at all for needs_input).
        backend = _native_backend([
            _tool_calls_reply([
                ("replace_summary", {"text": "A brand new summary."}),
                ("ask_user", {"question": "Is this the right tone?"}),
            ]),
        ])
        result = await _run_cv(backend)
        assert result.reply.kind == "needs_input"
        assert result.reply.raw.find("brand new summary") == -1


# --- CL stage parity ---------------------------------------------------------------------


class TestCoverLetterStage:
    async def test_cl_tools_dispatch_correctly(self):
        backend = _native_backend([
            _tool_calls_reply([
                ("replace_paragraph", {
                    "paragraph_id": "p1",
                    "text": (
                        "Updated opener: I am thrilled to apply for the Engineer role at "
                        "Acme, bringing hands-on experience shipping resilient systems."
                    ),
                }),
                ("finalize", {"change_log": "reworded opener"}),
            ]),
        ])
        result = await _run_cl(backend)
        assert result is not None
        payload = json.loads(result.reply.content)
        assert payload["paragraphs"][0].startswith("Updated opener:")


# --- unknown tool name -------------------------------------------------------------------


class TestUnknownTool:
    async def test_unknown_tool_name_is_a_recoverable_bad_argument(self):
        backend = _native_backend([
            _tool_calls_reply([("delete_everything", {})]),
            _tool_calls_reply([("finalize", {"change_log": "done"})]),
        ])
        result = await _run_cv(backend)
        assert result is not None
        assert result.tool_calls[0]["result"]["error"]["code"] == "bad_argument"
