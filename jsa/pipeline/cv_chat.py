"""Job-less, scoped chat turn for the base-CV editor's AI chat feature.

Mirrors ``jsa/pipeline/infer_structure.py::run_infer``'s shape (a fresh
``start_session``, mode chosen off the backend INSTANCE's
``supports_structured_output``, progress broadcast on a transient id, no self-heal
budget) plus a conversation history rendered into the user message and server-side op
application through ``jsa.schema.patch.CvWorkingCopy`` — the same applier
``jsa/pipeline/tool_loop.py`` uses for job-scoped revisions, reused here for its id
discipline and ``finalize()`` content gate, NOT via that loop (see the CV-editor AI
chat plan's "Why this does NOT port run_tool_loop").

D5 (the plan's scope decision): the model always sees the WHOLE CV — scope narrows
only what it may CHANGE. The schema's ``op`` enum is narrowed per scope
(``json_schema_for_cv_chat``, provider-enforced on structured backends) and the prompt
states the rule, but the AUTHORITATIVE enforcement is this module's structural diff
(``_enforce_scope`` below) between the submitted document and the finalized one — that
guard is vocabulary-independent and catches a sentinel-mode reply too, since sentinel
mode has no schema to narrow at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from jsa.agents.base import AgentBackend, AgentLimitReached, AgentTimeout
from jsa.agents.protocol import ProtocolError
from jsa.events.schema import ChatChunkEvent, ChatTurnEndEvent, event_to_dict
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.pipeline.validation import _strip_code_fence
from jsa.prompts import loader
from jsa.schema.chat_turn import (
    CvChatOp,
    CvChatTurn,
    ScopeType,
    json_schema_for_cv_chat,
    ops_for_scope,
)
from jsa.schema.cv import CVDocument
from jsa.schema.patch import CvWorkingCopy

# Context-budget caps (D2c's "one context budget, not three independent caps" — trim
# priority is attachments first, then history, and the CV dump is never trimmed since
# the ids in it are what every op addresses).
_MAX_ATTACHMENT_CHARS = 40_000
_MAX_HISTORY_TURNS = 8
_MAX_DIFF_ITEMS = 60

Publish = Any  # Callable[[dict], Awaitable[None]] — matches infer_structure.py's alias


@dataclass(frozen=True)
class ChatScope:
    """Which part of the CV a turn may change. Indexed by POSITION in
    ``cv.sections`` / ``section.entries`` — the one thing the frontend's client-only
    ids and the server's per-turn-minted ids both agree on (see the plan's D1)."""

    type: ScopeType
    section_index: int | None = None
    entry_index: int | None = None


@dataclass(frozen=True)
class DiffItem:
    label: str
    before: str
    after: str


@dataclass(frozen=True)
class ChatTurnResult:
    kind: Literal["final", "question"]
    answer: str | None
    question: str | None
    document: CVDocument | None
    items: list[DiffItem]
    rejected: list[str]
    reasoning: str


class ChatError(Exception):
    """The model's reply could not be turned into a valid, in-scope edit (-> HTTP 422),
    mirroring InferError's role for the CV-structure inference call."""


# ``run_cv_chat`` has no self-heal/retry loop (see the module docstring), so a
# validation failure reaches a human in the chat panel, not another model turn. Neither
# of finalize()'s two built-in ``structured``-selected wordings ("re-emit inside
# <<<FINAL>>>..." / "re-emit as your structured reply's payload") fits that audience --
# same reason tool_loop.py passes its own ``reemit_hint`` instead of a bare
# ``structured=`` flag (see validation.py's ``_parse_structured`` docstring).
_CHAT_REEMIT_HINT = "Try rephrasing the instruction, or narrowing its scope, and send it again."


def _scope_label(dump: dict[str, Any], scope: ChatScope, allowed_ops: str) -> tuple[str, str]:
    """Returns (display_label, allowed_ops_note) and mutates nothing.

    ``allowed_ops`` is passed in rather than hard-coded per scope so this line can
    never contradict the enum in the JSON schema. It used to be a literal listing the
    full section/entry vocabulary, which was fine while both were derived from scope
    TYPE alone — but once the schema also narrows by the node's SHAPE (see
    ``jsa.schema.chat_turn.ops_for_scope``), a literal would tell the model it may use
    ``edit_entry_bullets`` on an entry-less section while the schema forbade it. That
    contradiction is worst in **sentinel mode**, where there is no schema at all and
    this line is the only thing constraining the vocabulary.
    """
    if scope.type == "cv":
        return "ENTIRE CV", allowed_ops
    if scope.type == "contact":
        return "IDENTITY", allowed_ops
    sections = dump["sections"]
    section = sections[scope.section_index]
    if scope.type == "section":
        return (
            f"{(section['name'] or 'Section').upper()} (section {section['id']})",
            allowed_ops,
        )
    entry = section["entries"][scope.entry_index]
    heading = entry.get("heading") or entry["id"]
    return (
        f"{(section['name'] or 'Section').upper()} › {heading} (entry {entry['id']})",
        allowed_ops,
    )


def _render_cv_dump(dump: dict[str, Any], scope: ChatScope) -> str:
    """Render ``get_cv()``'s dump as an indented outline, marking the scoped node
    ``◀ EDITABLE`` — the model sees the whole tree for grounding (D5) but the
    marker makes the boundary unmissable in prose as well as in the schema."""
    lines = ["CURRENT CV  (ids are valid for this turn only)"]
    contact = dump["contact"]
    marker = " ◀ EDITABLE" if scope.type == "contact" else ""
    lines.append(f"  contact  {json.dumps(contact)}{marker}")
    for si, section in enumerate(dump["sections"]):
        sec_marker = " ◀ EDITABLE" if scope.type == "section" and si == scope.section_index else ""
        lines.append(f"  {section['id']} {section['name']}{sec_marker}")
        if section.get("text"):
            lines.append(f"       {section['text']}")
        if section.get("items"):
            lines.append(f"       items: {', '.join(section['items'])}")
        for ei, entry in enumerate(section["entries"]):
            entry_marker = (
                " ◀ EDITABLE"
                if scope.type == "entry" and si == scope.section_index and ei == scope.entry_index
                else ""
            )
            heading = " / ".join(x for x in (entry.get("heading"), entry.get("subheading")) if x)
            lines.append(f"       {entry['id']} {heading or '(untitled entry)'}{entry_marker}")
            # dates/location/text/links are rendered even though they rarely matter for
            # GROUNDING, because replace_entry/add_entry are whole-object replaces: a
            # field the model was never shown is a field it cannot restate, so omitting
            # them here silently blanked them on every entry edit. Confirmed live
            # (2026-09-17, gemini-3.1-flash-lite): "add the location, keep everything
            # else" returned `dates: null` and wiped "2021 - present", because the dump
            # had only ever shown the entry's heading, subheading and bullets.
            for field in ("dates", "location", "text"):
                if entry.get(field):
                    lines.append(f"           {field}: {entry[field]}")
            if entry.get("links"):
                lines.append(f"           links: {', '.join(entry['links'])}")
            for bullet in entry.get("bullets", []):
                lines.append(f"           • {bullet}")
    return "\n".join(lines)


def _build_user_message(
    dump: dict[str, Any],
    scope: ChatScope,
    instruction: str,
    history: list[tuple[str, str]],
    attachments: list[tuple[str, str]],
    allowed_ops: tuple[str, ...],
) -> str:
    label, allowed_ops_note = _scope_label(dump, scope, ", ".join(allowed_ops))
    parts = [f"EDITABLE SCOPE: {label}", f"allowed ops: {allowed_ops_note}", ""]
    parts.append(_render_cv_dump(dump, scope))

    trimmed_history = history[-_MAX_HISTORY_TURNS:]
    if trimmed_history:
        parts.append("")
        parts.append("PRIOR THREAD (this scope's conversation so far)")
        for role, text in trimmed_history:
            parts.append(f"  {role.upper()}: {text}")

    if attachments:
        parts.append("")
        parts.append(
            "READ-ONLY CONTEXT — evidence only, never copied verbatim, never stored in the CV"
        )
        budget = _MAX_ATTACHMENT_CHARS
        for filename, text in attachments:
            if budget <= 0:
                parts.append(f"  [{filename}: omitted — context budget exhausted]")
                continue
            snippet = text[:budget]
            budget -= len(snippet)
            parts.append(f"  --- {filename} ---")
            parts.append(f"  {snippet}")

    parts.append("")
    parts.append(f"INSTRUCTION: {instruction}")
    return "\n".join(parts)


def _plain(model: Any, *, exclude_none: bool = False) -> Any:
    """``CvWorkingCopy``'s setters take plain dicts (every one of them starts with an
    ``isinstance(x, dict)`` guard), while ``CvChatOp.section``/``entry``/``contact``
    are now typed models — see ``chat_turn.py``'s note on why they can't stay
    ``dict[str, Any]``. ``None`` passes straight through so a missing-argument op
    still reaches the applier's own ``bad_argument`` rejection rather than raising."""
    return None if model is None else model.model_dump(exclude_none=exclude_none)


_OP_DISPATCH = {
    "replace_summary": lambda copy, op: copy.replace_summary(op.text),
    "replace_section": lambda copy, op: copy.replace_section(op.section_id, _plain(op.section)),
    "replace_entry": lambda copy, op: copy.replace_entry(op.entry_id, _plain(op.entry)),
    "edit_entry_bullets": lambda copy, op: copy.edit_entry_bullets(op.entry_id, op.bullets),
    "add_entry": lambda copy, op: copy.add_entry(op.section_id, _plain(op.entry), op.position),
    "remove_entry": lambda copy, op: copy.remove_entry(op.entry_id),
    "reorder_entries": lambda copy, op: copy.reorder_entries(op.section_id, op.order),
    # `exclude_none=True` ONLY here: replace_contact is a partial update whose whole
    # contract is "an omitted key leaves the existing value alone". Dumping the
    # defaulted-None fields would send `phone: None`/`location: None` on every "fix
    # the email" turn and blank them. `[]` on `links` is NOT stripped, deliberately —
    # that is the user asking to clear them. See ChatContact's docstring.
    "replace_contact": lambda copy, op: copy.replace_contact(_plain(op.contact, exclude_none=True)),
}


def _apply_ops(copy: CvWorkingCopy, ops: list[CvChatOp]) -> list[str]:
    """Apply each op in order; a non-ok result is collected into ``rejected`` and the
    turn continues — one bad id must not kill the whole turn."""
    rejected: list[str] = []
    for op in ops:
        result = _OP_DISPATCH[op.op](copy, op)
        if not result.get("ok"):
            err = result.get("error", {})
            rejected.append(f"{op.op}: {err.get('message', 'failed')}")
    return rejected


def _section_dict(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": section.get("name"),
        "text": section.get("text"),
        "items": list(section.get("items") or []),
        "entries": [_entry_dict(e) for e in section.get("entries") or []],
    }


def _entry_dict(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry.get(k) for k in ("heading", "subheading", "dates", "location", "text")} | {
        "bullets": list(entry.get("bullets") or []),
        "links": list(entry.get("links") or []),
    }


def _document_as_plain(cv: CVDocument) -> dict[str, Any]:
    """A plain, id-free dict of ``cv`` shaped like ``CvWorkingCopy.get_cv()``'s
    ``sections``, for scope enforcement + diffing — the SAME shape on both sides of
    the comparison regardless of which ids CvWorkingCopy happened to mint."""
    return {
        "contact": cv.contact.model_dump(),
        "sections": [_section_dict(s.model_dump()) for s in cv.sections],
    }


def _enforce_scope(original: dict[str, Any], finalized: dict[str, Any], scope: ChatScope) -> None:
    """D5's authoritative gate: everything outside ``scope`` must be byte-identical
    between ``original`` (what the client submitted) and ``finalized`` (what the model's
    ops produced). Raises ``ChatError`` naming what changed outside scope."""
    orig_sections = original["sections"]
    fin_sections = finalized["sections"]

    if scope.type == "contact":
        if orig_sections != fin_sections:
            raise ChatError("the model changed CV sections outside the identity scope")
        return

    if scope.type == "cv":
        # "cv" scope's op vocabulary is "all ops" (D5/_SCOPE_OPS), including
        # replace_contact and add_entry/remove_entry -- so nothing here is out of
        # scope except adding/removing a whole section, which no op can do anyway
        # (no add_section/remove_section op exists). Falling through into the
        # contact-invariant check or the entry-scope branch below would reject
        # every legitimate whole-CV edit, since section_index/entry_index are None.
        if len(orig_sections) != len(fin_sections):
            raise ChatError(
                "the model added/removed a section, which is outside this turn's scope"
            )
        return

    if len(orig_sections) != len(fin_sections):
        raise ChatError(
            "the model added/removed a section, which is outside this turn's scope"
        )
    if original["contact"] != finalized["contact"]:
        raise ChatError("the model changed contact info outside this turn's scope")

    if scope.type == "section":
        for i, (o, f) in enumerate(zip(orig_sections, fin_sections)):
            if i != scope.section_index and o != f:
                raise ChatError(
                    f"the model changed section {i} which is outside this turn's scope"
                )
        return

    # entry scope
    for i, (o, f) in enumerate(zip(orig_sections, fin_sections)):
        if i != scope.section_index:
            if o != f:
                raise ChatError(
                    f"the model changed section {i} which is outside this turn's scope"
                )
            continue
        if o["name"] != f["name"] or o["text"] != f["text"] or o["items"] != f["items"]:
            raise ChatError(
                "the model changed the section itself, outside this turn's entry scope"
            )
        if len(o["entries"]) != len(f["entries"]):
            raise ChatError(
                "the model added/removed an entry, which is outside this turn's scope"
            )
        for j, (oe, fe) in enumerate(zip(o["entries"], f["entries"])):
            if j != scope.entry_index and oe != fe:
                raise ChatError(
                    f"the model changed entry {j} which is outside this turn's scope"
                )


def _diff_entry(label_prefix: str, before: dict[str, Any], after: dict[str, Any]) -> list[DiffItem]:
    items: list[DiffItem] = []
    for field in ("heading", "subheading", "dates", "location", "text"):
        if before.get(field) != after.get(field):
            items.append(DiffItem(f"{label_prefix} · {field}", str(before.get(field) or ""), str(after.get(field) or "")))
    if before.get("bullets") != after.get("bullets"):
        items.append(DiffItem(
            f"{label_prefix} · bullets",
            "\n".join(before.get("bullets") or []),
            "\n".join(after.get("bullets") or []),
        ))
    if before.get("links") != after.get("links"):
        items.append(DiffItem(
            f"{label_prefix} · links",
            "\n".join(before.get("links") or []),
            "\n".join(after.get("links") or []),
        ))
    return items


def _diff_documents(original: dict[str, Any], finalized: dict[str, Any]) -> list[DiffItem]:
    """A human-labelled field diff between two plain CV dicts (post scope-enforcement,
    so only in-scope content can actually differ). Capped at ``_MAX_DIFF_ITEMS``."""
    items: list[DiffItem] = []

    for field in ("name", "email", "phone", "location"):
        before, after = original["contact"].get(field), finalized["contact"].get(field)
        if before != after:
            items.append(DiffItem(f"Contact · {field}", str(before or ""), str(after or "")))
    if original["contact"].get("links") != finalized["contact"].get("links"):
        items.append(DiffItem(
            "Contact · links",
            "\n".join(original["contact"].get("links") or []),
            "\n".join(finalized["contact"].get("links") or []),
        ))

    orig_sections, fin_sections = original["sections"], finalized["sections"]
    for i in range(max(len(orig_sections), len(fin_sections))):
        if len(items) >= _MAX_DIFF_ITEMS:
            break
        if i >= len(orig_sections):
            items.append(DiffItem(f"Section added: {fin_sections[i]['name']}", "", "(new section)"))
            continue
        if i >= len(fin_sections):
            items.append(DiffItem(f"Section removed: {orig_sections[i]['name']}", "(removed)", ""))
            continue
        o, f = orig_sections[i], fin_sections[i]
        name = f["name"] or o["name"] or f"Section {i + 1}"
        if o["name"] != f["name"]:
            items.append(DiffItem(f"{name} · name", o["name"] or "", f["name"] or ""))
        if o["text"] != f["text"]:
            items.append(DiffItem(f"{name} · text", o["text"] or "", f["text"] or ""))
        if o["items"] != f["items"]:
            items.append(DiffItem(f"{name} · items", "\n".join(o["items"]), "\n".join(f["items"])))
        for j in range(max(len(o["entries"]), len(f["entries"]))):
            if len(items) >= _MAX_DIFF_ITEMS:
                break
            if j >= len(o["entries"]):
                items.append(DiffItem(f"{name} · entry added", "", f["entries"][j].get("heading") or "(new entry)"))
                continue
            if j >= len(f["entries"]):
                items.append(DiffItem(f"{name} · entry removed", o["entries"][j].get("heading") or "(entry)", ""))
                continue
            oe, fe = o["entries"][j], f["entries"][j]
            if oe != fe:
                label = f"{name} › {fe.get('heading') or oe.get('heading') or j + 1}"
                items.extend(_diff_entry(label, oe, fe))
    return items[:_MAX_DIFF_ITEMS]


def _scope_has_entries(cv: CVDocument, scope: ChatScope) -> bool:
    """Does the scoped node contain any entry an op could address?

    Feeds ``json_schema_for_cv_chat``'s shape narrowing (see ``ops_for_scope``): the
    ``e1…eN`` ids every entry op needs are minted from this node's entries, so when
    there are none, those ops are unsatisfiable and are dropped from the enum.

    ``contact`` and ``entry`` return True because neither scope's vocabulary is
    entry-addressing-and-empty: ``contact`` has only ``replace_contact``, and an
    ``entry`` scope is itself an existing entry. The indices are already validated
    against this same document by ``routes_cv_chat._scope_from_dict``, but this stays
    defensive — it is called before any model turn and must not raise.
    """
    if scope.type == "section":
        if scope.section_index is None or not (0 <= scope.section_index < len(cv.sections)):
            return True
        return bool(cv.sections[scope.section_index].entries)
    if scope.type == "cv":
        return any(s.entries for s in cv.sections)
    return True


async def run_cv_chat(
    backend: AgentBackend,
    *,
    cv: CVDocument,
    scope: ChatScope,
    instruction: str,
    history: list[tuple[str, str]],
    attachments: list[tuple[str, str]],
    language: str,
    task_id: str,
    publish: Publish,
) -> ChatTurnResult:
    async def emit_chunk(kind: str, text: str) -> None:
        await publish(event_to_dict(ChatChunkEvent(task_id=task_id, kind=kind, text=text)))

    copy = CvWorkingCopy(cv)
    dump = copy.get_cv()
    original_plain = _document_as_plain(cv)

    # ONE shape decision feeding BOTH channels — the schema's enum (structured mode)
    # and the prompt's "allowed ops:" line (the only channel in sentinel mode). Two
    # independent derivations could disagree and tell the model it may use an op the
    # schema forbids; same single-resolution rule CLAUDE.md applies to `injection` and
    # to the `structured_schema`/`adapt_history` pairing.
    has_entries = _scope_has_entries(cv, scope)
    allowed_ops = ops_for_scope(scope.type, has_entries=has_entries)
    schema = (
        json_schema_for_cv_chat(scope.type, has_entries=has_entries)
        if backend.supports_structured_output
        else None
    )
    system_prompt = assemble_system_prompt(
        loader.read_prompt("cv_chat"),
        language=language,
        structured_model=schema,
        now=datetime.utcnow(),
    )
    user_msg = _build_user_message(dump, scope, instruction, history, attachments, allowed_ops)

    kwargs: dict[str, Any] = {}
    if schema is not None:
        kwargs["structured_schema"] = schema

    reasoning = ""
    if getattr(backend, "supports_streaming", False):

        async def _on_chunk(chunk) -> None:  # noqa: ANN001 - AgentChunk
            nonlocal reasoning
            if chunk.kind == "reasoning":
                reasoning += chunk.text
            await emit_chunk(chunk.kind, chunk.text)

        kwargs["on_chunk"] = _on_chunk

    try:
        handle, reply = await backend.start_session(system_prompt, user_msg, **kwargs)
    except ProtocolError as exc:
        raise ChatError(f"the model did not return a usable reply: {exc}") from exc
    except AgentTimeout as exc:
        raise ChatError(f"request timed out: {exc}") from exc
    except AgentLimitReached as exc:
        raise ChatError(f"rate limit reached: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - mirrors run_infer's catch-all
        raise ChatError(f"backend error: {exc}") from exc
    finally:
        await publish(event_to_dict(ChatTurnEndEvent(task_id=task_id)))

    await backend.end_session(handle)

    if reply.kind == "needs_input":
        return ChatTurnResult(
            kind="question",
            answer=None,
            question=reply.question,
            document=None,
            items=[],
            rejected=[],
            reasoning=reasoning,
        )

    text = _strip_code_fence(reply.content)
    try:
        data = json.loads(text)
        turn = CvChatTurn.model_validate(
            {"kind": "final", "question": None, "payload": data, "suggested_replies": None}
        )
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ChatError(f"the model's reply did not match the expected shape: {exc}") from exc

    payload = turn.payload
    assert payload is not None  # enforced by CvChatTurn's model_validator

    if not payload.ops:
        return ChatTurnResult(
            kind="final",
            answer=payload.answer,
            question=None,
            document=None,
            items=[],
            rejected=[],
            reasoning=reasoning,
        )

    rejected = _apply_ops(copy, payload.ops)
    result = copy.finalize(language=language, reemit_hint=_CHAT_REEMIT_HINT)
    if not result.get("ok"):
        raise ChatError(str(result["error"]["message"]))
    document: CVDocument = result["document"]

    finalized_plain = _document_as_plain(document)
    _enforce_scope(original_plain, finalized_plain, scope)
    items = _diff_documents(original_plain, finalized_plain)

    return ChatTurnResult(
        kind="final",
        answer=payload.answer,
        question=None,
        document=document,
        items=items,
        rejected=rejected,
        reasoning=reasoning,
    )


__all__ = ["ChatScope", "DiffItem", "ChatTurnResult", "ChatError", "run_cv_chat"]
