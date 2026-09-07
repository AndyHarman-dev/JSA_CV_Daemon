"""Structured-output mode for the job-less CV-structure inference call.

`jsa/pipeline/infer_structure.py::run_infer` used to be the one model call in the repo
that was sentinel-only regardless of backend capability. These tests pin the two halves
of putting it on structured output:

* the *mode decision* — schema sent (and contract appended) on a structured-capable
  backend, nothing at all sent on a CLI-shaped one, whose `start_session` accepts no
  `structured_schema` parameter to begin with;
* the *routing* — `InferTurn`'s one-member `kind` field exists so a structured reply
  routes through `parse_structured_reply_for_schema`'s turn-union branch. A fake backend
  returns its scripted `AgentReply` verbatim and never calls that function, so the fake
  alone would happily pass while every real structured backend failed; `TestStructured
  ReplyRoutesToTheDocument` closes that gap by running the real parse first and feeding
  its output in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jsa.agents.base import AgentReply
from jsa.pipeline.infer_structure import InferError, run_infer
from jsa.prompts import loader
from jsa.schema.turn_models import json_schema_for_infer, parse_structured_reply_for_schema
from tests.backend.fakes.fake_backend import FakeAgentBackend

_VALID_CV: dict[str, Any] = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com", "location": "Berlin"},
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Experience", "entries": [
            {"heading": "Senior Engineer", "subheading": "Acme", "dates": "2020-Present",
             "bullets": ["Built X serving 1M users", "Cut latency 40%"]},
        ]},
        {"name": "Skills", "items": ["Python", "Go", "Docker"]},
    ],
}


class _RecordingBackend(FakeAgentBackend):
    """FakeAgentBackend that also captures the assembled system prompt."""

    def __init__(self, replies: list[AgentReply], **kwargs: Any) -> None:
        super().__init__(replies, **kwargs)
        self.system_prompts: list[str] = []

    async def start_session(self, system_prompt: str, initial_user_msg: str, **kwargs: Any):
        self.system_prompts.append(system_prompt)
        return await super().start_session(system_prompt, initial_user_msg, **kwargs)


class _CliShapedBackend(_RecordingBackend):
    """A backend whose ``start_session`` accepts NO ``structured_schema`` — the shape of
    ``ClaudeCliBackend``/``GoogleCliBackend``, which declare
    ``supports_structured_output = False`` and take no such parameter at all. Passing the
    kwarg here is a ``TypeError``, which is exactly the contract being pinned: sentinel
    mode must OMIT the kwarg, not pass ``None``."""

    async def start_session(self, system_prompt: str, initial_user_msg: str):  # type: ignore[override]
        self.system_prompts.append(system_prompt)
        return await FakeAgentBackend.start_session(self, system_prompt, initial_user_msg)


async def _noop_publish(event: dict) -> None:
    return None


@pytest.fixture(autouse=True)
def _no_real_extraction(monkeypatch):
    monkeypatch.setattr("jsa.pipeline.infer_structure.load_cv", lambda path: "raw cv text")


def _final_reply(payload: dict) -> AgentReply:
    """A sentinel-mode-shaped reply: content is the bare CVDocument JSON."""
    return AgentReply(raw="<<<FINAL>>>", content=json.dumps(payload), kind="final")


class TestStructuredMode:
    async def test_schema_is_sent_when_the_backend_supports_structured_output(self):
        backend = _RecordingBackend(
            [_final_reply(_VALID_CV)], supports_structured_output=True
        )
        cv = await run_infer(
            backend, Path("cv.pdf"), task_id="t", publish=_noop_publish
        )
        assert cv.contact.name == "Jane Doe"
        assert backend.received_schemas == [json_schema_for_infer()]

    async def test_contract_is_appended_after_the_prompt_file_text(self):
        backend = _RecordingBackend(
            [_final_reply(_VALID_CV)], supports_structured_output=True
        )
        await run_infer(backend, Path("cv.pdf"), task_id="t", publish=_noop_publish)

        prompt = backend.system_prompts[0]
        file_text = loader.read_prompt("infer_structure")
        assert prompt.startswith(file_text)
        assert "## Structured output contract" in prompt
        # The document-only shape: always final, never a question.
        assert "no question branch" in prompt.lower()
        assert "suggested_replies" not in prompt
        # The schema itself rides along in the contract.
        assert json.dumps(json_schema_for_infer(), indent=2) in prompt

    async def test_no_language_directive_even_though_language_is_a_required_kwarg(self):
        """`run_infer` passes `document_only=True`, which suppresses the directive
        outright rather than relying on the `language == "en"` short-circuit."""
        backend = _RecordingBackend(
            [_final_reply(_VALID_CV)], supports_structured_output=True
        )
        await run_infer(backend, Path("cv.pdf"), task_id="t", publish=_noop_publish)
        assert "## Output language" not in backend.system_prompts[0]


class TestSentinelModeIsUnchanged:
    async def test_cli_shaped_backend_gets_the_prompt_file_verbatim_and_no_kwarg(self):
        """The whole safety argument for touching `run_infer` at all: on a backend that
        cannot enforce a schema, the assembled prompt is byte-identical to the prompt
        file and no `structured_schema` kwarg is offered."""
        backend = _CliShapedBackend([_final_reply(_VALID_CV)])
        cv = await run_infer(
            backend, Path("cv.pdf"), task_id="t", publish=_noop_publish
        )
        assert cv.contact.name == "Jane Doe"
        assert backend.system_prompts == [loader.read_prompt("infer_structure")]
        assert backend.received_schemas == [None]

    async def test_malformed_reply_is_still_a_terminal_infer_error(self):
        backend = _CliShapedBackend(
            [AgentReply(raw="x", content="not json at all", kind="final")]
        )
        with pytest.raises(InferError):
            await run_infer(backend, Path("cv.pdf"), task_id="t", publish=_noop_publish)


class TestStructuredReplyRoutesToTheDocument:
    """The landmine test. A bare `CVDocument` schema has no `kind` property, so
    `parse_structured_reply_for_schema` would route it into `_parse_fit_structured` and
    reject every reply with "invalid 'verdict'". `InferTurn`'s one-member `kind` is what
    prevents that — asserted here against the REAL parse function, which the fake never
    reaches on its own."""

    def test_parse_routes_a_final_turn_to_its_payload(self):
        raw = json.dumps({"kind": "final", "payload": _VALID_CV})
        reply = parse_structured_reply_for_schema(raw, json_schema_for_infer())
        assert reply.kind == "final"
        assert json.loads(reply.content) == _VALID_CV

    async def test_end_to_end_through_the_real_parse(self):
        """What a real structured backend hands `run_infer`: the wire JSON put through
        `parse_structured_reply_for_schema` first."""
        raw = json.dumps({"kind": "final", "payload": _VALID_CV})
        reply = parse_structured_reply_for_schema(raw, json_schema_for_infer())
        backend = _RecordingBackend([reply], supports_structured_output=True)

        cv = await run_infer(
            backend, Path("cv.pdf"), task_id="t", publish=_noop_publish
        )
        assert cv.contact.name == "Jane Doe"
        assert [s.name for s in cv.sections] == ["Summary", "Experience", "Skills"]


class TestProgressEventsSurviveBothModes:
    async def test_all_five_steps_still_broadcast_in_structured_mode(self):
        events: list[dict] = []

        async def publish(event: dict) -> None:
            events.append(event)

        backend = _RecordingBackend(
            [_final_reply(_VALID_CV)], supports_structured_output=True
        )
        await run_infer(backend, Path("cv.pdf"), task_id="t", publish=publish)

        progress = [e for e in events if e["type"] == "infer_progress"]
        assert {e["step"] for e in progress} == {1, 2, 3, 4, 5}
        assert any(e["status"] == "done" for e in progress)

    async def test_structured_parse_failure_still_emits_the_step_5_error(self):
        events: list[dict] = []

        async def publish(event: dict) -> None:
            events.append(event)

        backend = _RecordingBackend(
            [AgentReply(raw="x", content="{}", kind="final")],
            supports_structured_output=True,
        )
        with pytest.raises(InferError):
            await run_infer(backend, Path("cv.pdf"), task_id="t", publish=publish)

        assert any(
            e["type"] == "infer_progress" and e["step"] == 5 and e["status"] == "error"
            for e in events
        )
