"""CV-editor AI chat plan, Phase 2: ``jsa/pipeline/cv_chat.py::run_cv_chat``."""

from __future__ import annotations

import json

import pytest

from jsa.agents.base import AgentChunk, AgentReply
from jsa.pipeline.cv_chat import ChatError, ChatScope, run_cv_chat
from jsa.schema.cv import CVDocument
from tests.backend.fakes.fake_backend import CapturingBackend, FakeAgentBackend

_CV_JSON = {
    "contact": {"name": "Jane Doe", "email": "jane@example.com"},
    "sections": [
        {"name": "Summary", "text": "Experienced engineer."},
        {
            "name": "Experience",
            "entries": [
                {"heading": "Engineer", "subheading": "Acme", "bullets": ["Did X", "Did Y"]},
            ],
        },
        {"name": "Skills", "items": ["Python", "SQL"]},
    ],
}


def _cv() -> CVDocument:
    return CVDocument.model_validate(_CV_JSON)


async def _publish_noop(_event: dict) -> None:
    return None


class _EventSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)


def _final_reply(answer: str, ops: list[dict]) -> AgentReply:
    body = json.dumps({"answer": answer, "ops": ops})
    return AgentReply(raw=f"<<<FINAL>>>\n{body}\n<<<END>>>", content=body, kind="final")


def _question_reply(question: str) -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


def _summary_op(text: str) -> dict:
    return {
        "op": "replace_summary",
        "section_id": None,
        "entry_id": None,
        "position": None,
        "text": text,
        "bullets": None,
        "order": None,
        "section": None,
        "entry": None,
        "contact": None,
    }


def _contact_op(**contact_fields) -> dict:
    return {
        "op": "replace_contact",
        "section_id": None,
        "entry_id": None,
        "position": None,
        "text": None,
        "bullets": None,
        "order": None,
        "section": None,
        "entry": None,
        "contact": contact_fields,
    }


def _bullets_op(entry_id: str, bullets: list[str]) -> dict:
    return {
        "op": "edit_entry_bullets",
        "section_id": None,
        "entry_id": entry_id,
        "position": None,
        "text": None,
        "bullets": bullets,
        "order": None,
        "section": None,
        "entry": None,
        "contact": None,
    }


class TestRunCvChat:
    async def test_sentinel_mode_final_turn(self):
        backend = FakeAgentBackend([_final_reply("Tightened the summary.", [_summary_op("New summary.")])])
        result = await run_cv_chat(
            backend,
            cv=_cv(),
            scope=ChatScope(type="section", section_index=0),
            instruction="tighten this",
            history=[],
            attachments=[],
            language="en",
            task_id="t1",
            publish=_publish_noop,
        )
        assert result.kind == "final"
        assert result.document is not None
        assert result.document.sections[0].text == "New summary."
        assert result.rejected == []

    async def test_structured_mode_final_turn(self):
        backend = FakeAgentBackend(
            [_final_reply("Tightened the summary.", [_summary_op("New summary.")])],
            supports_structured_output=True,
        )
        result = await run_cv_chat(
            backend,
            cv=_cv(),
            scope=ChatScope(type="section", section_index=0),
            instruction="tighten this",
            history=[],
            attachments=[],
            language="en",
            task_id="t2",
            publish=_publish_noop,
        )
        assert result.kind == "final"
        assert result.document.sections[0].text == "New summary."
        assert backend.received_schemas[0] is not None

    async def test_mode_parity(self):
        """Same logical payload -> identical observable output in both modes."""
        sentinel = FakeAgentBackend([_final_reply("Answer.", [_summary_op("Same text.")])])
        structured = FakeAgentBackend(
            [_final_reply("Answer.", [_summary_op("Same text.")])],
            supports_structured_output=True,
        )
        r1 = await run_cv_chat(
            sentinel, cv=_cv(), scope=ChatScope(type="section", section_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t3", publish=_publish_noop,
        )
        r2 = await run_cv_chat(
            structured, cv=_cv(), scope=ChatScope(type="section", section_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t4", publish=_publish_noop,
        )
        assert r1.document.model_dump() == r2.document.model_dump()
        assert r1.answer == r2.answer
        assert [i.label for i in r1.items] == [i.label for i in r2.items]

    async def test_question_turn_returns_no_document(self):
        backend = FakeAgentBackend([_question_reply("Which section should I focus on?")])
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="cv"),
            instruction="improve it", history=[], attachments=[], language="en",
            task_id="t5", publish=_publish_noop,
        )
        assert result.kind == "question"
        assert result.question == "Which section should I focus on?"
        assert result.document is None

    async def test_out_of_scope_op_raises_chat_error(self):
        # Scope is section 0 (Summary), but the op edits section 1's entry bullets.
        backend = FakeAgentBackend([_final_reply("Updated.", [_bullets_op("e1", ["Changed"])])])
        with pytest.raises(ChatError):
            await run_cv_chat(
                backend, cv=_cv(), scope=ChatScope(type="section", section_index=0),
                instruction="x", history=[], attachments=[], language="en",
                task_id="t6", publish=_publish_noop,
            )

    async def test_cv_scope_allows_summary_edit(self):
        """Regression: ``_enforce_scope`` had no ``cv`` branch and fell through into
        the entry-scope check, which compares against ``None`` section/entry_index --
        so ANY edit under the whole-CV scope (the default/most common entry point)
        was rejected as out-of-scope."""
        backend = FakeAgentBackend([_final_reply("Tightened.", [_summary_op("New summary.")])])
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="cv"),
            instruction="tighten it", history=[], attachments=[], language="en",
            task_id="t6b", publish=_publish_noop,
        )
        assert result.kind == "final"
        assert result.document is not None
        assert result.document.sections[0].text == "New summary."

    async def test_cv_scope_allows_contact_edit(self):
        """Regression: cv scope's op vocabulary includes ``replace_contact``
        (_SCOPE_OPS["cv"]), but the old code ran the unconditional contact-invariant
        check meant for section/entry scopes, which blocked it."""
        backend = FakeAgentBackend(
            [_final_reply("Updated contact.", [_contact_op(name="New Name", email="jane@example.com")])]
        )
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="cv"),
            instruction="update name", history=[], attachments=[], language="en",
            task_id="t6c", publish=_publish_noop,
        )
        assert result.kind == "final"
        assert result.document is not None
        assert result.document.contact.name == "New Name"

    async def test_unknown_id_is_rejected_but_turn_succeeds(self):
        backend = FakeAgentBackend(
            [_final_reply("Updated bullets.", [_bullets_op("e999", ["ghost"])])]
        )
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="entry", section_index=1, entry_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t7", publish=_publish_noop,
        )
        assert result.kind == "final"
        assert len(result.rejected) == 1
        assert "e999" in result.rejected[0] or "unknown" in result.rejected[0].lower()

    async def test_empty_ops_is_a_legal_final_turn_with_no_document(self):
        backend = FakeAgentBackend([_final_reply("Nothing worth changing here.", [])])
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="cv"),
            instruction="review it", history=[], attachments=[], language="en",
            task_id="t8", publish=_publish_noop,
        )
        assert result.kind == "final"
        assert result.document is None
        assert result.answer == "Nothing worth changing here."

    async def test_reasoning_stream_in_structured_mode(self):
        chunks = [[AgentChunk(kind="reasoning", text="Thinking about it...")]]
        backend = FakeAgentBackend(
            [_final_reply("Done.", [_summary_op("New.")])],
            supports_structured_output=True,
            supports_streaming=True,
            scripted_chunks=chunks,
        )
        sink = _EventSink()
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="section", section_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t9", publish=sink,
        )
        assert result.reasoning == "Thinking about it..."
        chunk_events = [e for e in sink.events if e["type"] == "chat_chunk"]
        assert chunk_events == [{"type": "chat_chunk", "task_id": "t9", "kind": "reasoning", "text": "Thinking about it..."}]
        assert any(e["type"] == "chat_turn_end" for e in sink.events)

    async def test_contact_scope_sees_whole_cv_and_narrows_schema(self):
        """D5's context pin: an entry-scoped (and here, contact-scoped) turn still
        gets the whole CV dump, and the schema's op enum is narrowed to that scope."""
        backend = CapturingBackend(
            [_final_reply("Updated email.", [_contact_op(email="new@x.com")])],
            supports_structured_output=True,
        )
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="contact"),
            instruction="fix the email", history=[], attachments=[], language="en",
            task_id="t10", publish=_publish_noop,
        )
        assert result.document.contact.email == "new@x.com"
        # The whole CV, including sections the scope cannot touch, is still present.
        assert "Experience" in backend.captured_initial_msg
        assert "Skills" in backend.captured_initial_msg
        assert "Python" in backend.captured_initial_msg
        schema = backend.received_schemas[0]
        assert schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"] == ["replace_contact"]

    async def test_validation_failure_uses_chat_facing_hint_not_sentinel_wording(self):
        """finalize() must not fall back to either of validation.py's two built-in
        re-emit wordings here -- there is no self-heal loop reading this message, a
        human reads it in the chat panel (see cv_chat.py's ``_CHAT_REEMIT_HINT``)."""
        letter_text = (
            "Dear Hiring Manager, I am writing to apply for this role. Sincerely,"
        )
        backend = FakeAgentBackend([_final_reply("Updated.", [_summary_op(letter_text)])])
        with pytest.raises(ChatError) as exc_info:
            await run_cv_chat(
                backend, cv=_cv(), scope=ChatScope(type="section", section_index=0),
                instruction="x", history=[], attachments=[], language="en",
                task_id="t12", publish=_publish_noop,
            )
        message = str(exc_info.value)
        assert "<<<FINAL>>>" not in message
        assert "structured reply" not in message
        assert "rephrasing" in message.lower()

    async def test_entry_scope_context_pin(self):
        backend = CapturingBackend(
            [_final_reply("Tweaked bullets.", [_bullets_op("e1", ["New bullet"])])],
            supports_structured_output=True,
        )
        result = await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="entry", section_index=1, entry_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t11", publish=_publish_noop,
        )
        assert result.document.sections[1].entries[0].bullets == ["New bullet"]
        assert "Summary" in backend.captured_initial_msg
        assert "Skills" in backend.captured_initial_msg
        schema = backend.received_schemas[0]
        assert set(schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"]) == {
            "replace_entry",
            "edit_entry_bullets",
        }

    async def test_cv_dump_shows_every_entry_field_the_model_must_restate(self):
        """Regression: ``replace_entry``/``add_entry`` are whole-object replaces, but
        the dump only ever rendered an entry's heading, subheading and bullets. A model
        asked to "add the location, keep everything else" therefore could not restate
        ``dates`` -- it had never been shown it -- and returned ``dates: null``, wiping
        it. Confirmed live against gemini-3.1-flash-lite (2026-09-17) before the fix and
        clean after it. Every field ``ChatEntry`` accepts must appear in the dump."""
        cv = CVDocument.model_validate({
            "contact": {"name": "Jane Doe"},
            "sections": [{"name": "Experience", "entries": [{
                "heading": "Engineer", "subheading": "Acme", "dates": "2021 - present",
                "location": "Berlin", "text": "Some prose.",
                "bullets": ["Did X"], "links": ["https://example.com/proj"],
            }]}],
        })
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=True,
        )
        await run_cv_chat(
            backend, cv=cv, scope=ChatScope(type="entry", section_index=0, entry_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t12", publish=_publish_noop,
        )
        dump = backend.captured_initial_msg
        for value in ("2021 - present", "Berlin", "Some prose.", "https://example.com/proj"):
            assert value in dump, f"{value!r} missing from the CV dump"


class TestSchemaShapeNarrowing:
    """The schema actually handed to a structured backend drops entry-addressing ops
    when the scoped node has no entries. Unit coverage for the enum itself lives in
    ``test_cv_chat_ops.py::TestShapeNarrowing``; this pins the WIRING — that
    ``run_cv_chat`` computes ``has_entries`` from the real document and threads it in.
    """

    ENTRY_ADDRESSING = {"replace_entry", "edit_entry_bullets", "remove_entry", "reorder_entries"}

    async def _enum_sent_for(self, scope: ChatScope) -> list[str]:
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=True,
        )
        await run_cv_chat(
            backend, cv=_cv(), scope=scope, instruction="x", history=[], attachments=[],
            language="en", task_id="t-narrow", publish=_publish_noop,
        )
        schema = backend.received_schemas[0]
        assert schema is not None
        return schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"]

    async def test_entryless_section_scope_gets_narrowed_schema(self):
        """Section 0 is `Summary` — text only, no entries, so there is no `e1` id in
        the dump for an entry op to name. This is the exact shape that lost a live
        turn to `edit_entry_bullets` + `entry_id: null`."""
        assert self.ENTRY_ADDRESSING.isdisjoint(await self._enum_sent_for(
            ChatScope(type="section", section_index=0)
        ))

    async def test_section_with_entries_keeps_the_full_vocabulary(self):
        """Section 1 is `Experience`, which has a real entry — narrowing must not fire."""
        enum = await self._enum_sent_for(ChatScope(type="section", section_index=1))
        assert self.ENTRY_ADDRESSING <= set(enum)

    async def test_cv_scope_keeps_entry_ops_when_any_section_has_entries(self):
        enum = await self._enum_sent_for(ChatScope(type="cv"))
        assert self.ENTRY_ADDRESSING <= set(enum)

    async def test_cv_scope_narrows_when_no_section_has_entries(self):
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=True,
        )
        cv = CVDocument.model_validate({
            "contact": {"name": "Jane Doe"},
            "sections": [{"name": "Summary", "text": "Prose only."}],
        })
        await run_cv_chat(
            backend, cv=cv, scope=ChatScope(type="cv"), instruction="x", history=[],
            attachments=[], language="en", task_id="t-narrow-cv", publish=_publish_noop,
        )
        enum = backend.received_schemas[0]["$defs"]["CvChatOp"]["properties"]["op"]["enum"]
        assert self.ENTRY_ADDRESSING.isdisjoint(enum)

    async def test_sentinel_backend_is_unaffected(self):
        """A sentinel-mode backend is handed no schema at all, so the narrowing is a
        no-op there — the structural scope diff stays the authoritative guard."""
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=False,
        )
        await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="section", section_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t-sentinel", publish=_publish_noop,
        )
        assert backend.received_schemas == [None]

    @pytest.mark.parametrize(
        "scope",
        [
            ChatScope(type="section", section_index=0),  # entry-less -> narrowed
            ChatScope(type="section", section_index=1),  # has entries -> full
            ChatScope(type="entry", section_index=1, entry_index=0),
            ChatScope(type="contact"),
            ChatScope(type="cv"),
        ],
        ids=["section-entryless", "section-with-entries", "entry", "contact", "cv"],
    )
    async def test_prompt_allowed_ops_line_matches_the_schema_enum(self, scope):
        """The "allowed ops:" line and the schema enum are ONE decision, not two.

        Before shape narrowing both were derived from scope type alone and a hard-coded
        literal in `_scope_label` happened to agree with the enum -- except at `cv`
        scope, where the literal was the vague `"all ops"`. Once the schema also narrows
        by node shape, a literal would advertise `edit_entry_bullets` on an entry-less
        section while the schema forbade it. Parametrized over every scope so the
        equality is pinned on all of them, not just the one that motivated the change.
        """
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=True,
        )
        await run_cv_chat(
            backend, cv=_cv(), scope=scope,
            instruction="x", history=[], attachments=[], language="en",
            task_id="t-coherent", publish=_publish_noop,
        )
        enum = set(backend.received_schemas[0]["$defs"]["CvChatOp"]["properties"]["op"]["enum"])
        line = next(
            ln for ln in backend.captured_initial_msg.splitlines()
            if ln.startswith("allowed ops:")
        )
        advertised = {op.strip() for op in line.removeprefix("allowed ops:").split(",")}
        assert advertised == enum

    async def test_cv_scope_line_is_the_explicit_vocabulary_not_all_ops(self):
        """Deliberate behaviour change from Fix 3: `cv` scope's line was the literal
        `"all ops"` and is now the explicit 8-op list. The prompt file already calls this
        line "the exact op names you are allowed to use this turn", so naming them is
        strictly more specific than the old phrasing -- but it IS a prompt-byte change on
        a path where nothing was narrowed, so it gets its own pin rather than riding
        along inside the equality test above."""
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=True,
        )
        await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="cv"), instruction="x", history=[],
            attachments=[], language="en", task_id="t-cv-line", publish=_publish_noop,
        )
        line = next(
            ln for ln in backend.captured_initial_msg.splitlines()
            if ln.startswith("allowed ops:")
        )
        assert "all ops" not in line
        assert "replace_contact" in line and "replace_summary" in line

    async def test_sentinel_mode_prompt_is_narrowed_too(self):
        """Sentinel mode gets no schema, so this line is the ONLY constraint — it must
        still reflect the node's shape. This is the `claude-cli` default's whole story."""
        backend = CapturingBackend(
            [_final_reply("No change.", [])], supports_structured_output=False,
        )
        await run_cv_chat(
            backend, cv=_cv(), scope=ChatScope(type="section", section_index=0),
            instruction="x", history=[], attachments=[], language="en",
            task_id="t-sentinel-narrow", publish=_publish_noop,
        )
        line = next(
            ln for ln in backend.captured_initial_msg.splitlines()
            if ln.startswith("allowed ops:")
        )
        assert "edit_entry_bullets" not in line
        assert "replace_summary" in line
