"""HTTP routes for the CV-editor AI chat (job-less, scoped, per-deck).

  GET    /api/cv-decks/{id}/chat  -> the deck's persisted thread
  POST   /api/cv-decks/{id}/chat  -> multipart: a JSON `payload` Form field (scope,
                                      instruction | quick_action, the client's current
                                      CV buffer, base_hash) + optional file attachments.
                                      Runs one turn in-request (jsa.pipeline.cv_chat)
                                      and persists it.
  DELETE /api/cv-decks/{id}/chat  -> clear the thread (the panel's NEW button)

Mirrors ``jsa/api/routes_cv_decks.py``'s error-mapping idiom (``cv_decks.InvalidDeckId``
-> 400, unknown deck id -> 404) and ``jsa/api/routes_cv_structure.py``'s multipart
temp-file dance for attachments. No ``orchestrator.kick()`` anywhere here -- this
touches no deck file and no job.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ValidationError

from jsa.events.bus import bus
from jsa.ingest.text_source import UnsupportedSource, load_text_source
from jsa.pipeline.cv_chat import ChatError, ChatScope, run_cv_chat
from jsa.pipeline.infer_structure import _concise_reason
from jsa.schema import CVDocument
from jsa.store import cv_decks, deck_chats, preferences
from jsa.store.cv_decks import InvalidDeckId
from jsa.store.deck_chats import ChatTurn

router = APIRouter()

_MAX_FILES = 6
_MAX_FILE_BYTES = 5 * 1024 * 1024

# Quick-action chips resolve to a real instruction server-side (the README's rule:
# "quick actions are real instructions, not canned transforms") -- the client sends
# only the key, so it is never the authority on what a chip means.
QUICK_ACTIONS: dict[str, str] = {
    "compact": (
        "Make this more compact -- tighten the wording without losing any facts."
    ),
    "quantify": (
        "Where possible, surface a metric or number that is already present elsewhere "
        "in the CV to make the impact concrete. Never invent a number."
    ),
    "reorder": "Reorder this for the strongest, most relevant points first.",
    "expand": (
        "Expand this with relevant detail that is already implied elsewhere in the "
        "CV. Do not invent new facts."
    ),
    "one_page": (
        "Tighten the whole CV so it reads cleanly on one page -- cut redundancy, "
        "keep every fact."
    ),
    "grammar": "Fix grammar, spelling, and punctuation throughout, without changing meaning.",
    "tone": "Make the tone more professional and consistent throughout.",
    "fix_formatting": (
        "Clean up formatting inconsistencies in the identity fields (capitalization, "
        "phone format, etc.) without changing the underlying facts."
    ),
}


class ChatTurnPayload(BaseModel):
    scope: dict[str, Any]
    instruction: str | None = None
    quick_action: str | None = None
    cv: dict[str, Any]
    base_hash: str = ""


def _write_temp_file(data: bytes, suffix: str) -> Path:
    """Blocking write, run off the event loop (CLAUDE.md's "do not block the event
    loop" rule) -- called via ``asyncio.to_thread`` at the one call site below."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
    finally:
        tmp.close()
    return Path(tmp.name)


def _settings(request: Request):
    return request.app.state.settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _require_known_deck(settings, deck_id: str) -> None:
    """400 on a malformed id, 404 on a well-formed but unknown one -- the same
    mapping ``routes_cv_decks.py`` uses, duplicated here rather than imported since
    it is two lines and this module has no other dependency on that one."""
    try:
        cv_decks.deck_path(settings, deck_id)
    except InvalidDeckId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    index = await cv_decks.load_index(settings)
    if not any(m.id == deck_id for m in index.decks):
        raise HTTPException(status_code=404, detail=f"unknown deck id: {deck_id!r}")


def _in_range_index(value: Any, length: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < length


def _scope_from_dict(raw: dict[str, Any], cv: CVDocument) -> ChatScope:
    """Validate scope against the SUBMITTED cv's actual shape -- ``section_index`` and
    ``entry_index`` are used to index straight into ``cv.sections``/``.entries`` in
    ``cv_chat.py`` (``_scope_label``, ``_render_cv_dump``), so a missing, non-int, or
    out-of-range index must fail here with a 422, not reach an unhandled
    IndexError/TypeError (500) or -- for a negative index -- silently wrap onto the
    wrong node via Python's negative-index slicing."""
    scope_type = raw.get("type")
    if scope_type not in ("cv", "contact", "section", "entry"):
        raise HTTPException(status_code=422, detail=f"malformed scope.type: {scope_type!r}")

    section_index = None
    if scope_type in ("section", "entry"):
        section_index = raw.get("section_index")
        if not _in_range_index(section_index, len(cv.sections)):
            raise HTTPException(
                status_code=422, detail=f"scope.section_index out of range: {section_index!r}"
            )

    entry_index = None
    if scope_type == "entry":
        entry_index = raw.get("entry_index")
        entries = cv.sections[section_index].entries
        if not _in_range_index(entry_index, len(entries)):
            raise HTTPException(
                status_code=422, detail=f"scope.entry_index out of range: {entry_index!r}"
            )

    return ChatScope(type=scope_type, section_index=section_index, entry_index=entry_index)


def _history_from_turns(turns: list[ChatTurn]) -> list[tuple[str, str]]:
    """Render the last few persisted turns as (role, text) pairs for the prompt --
    the scoped-conversation-so-far block. Skip 'scope' marker turns and any turn
    still carrying only a question with no answer yet."""
    history: list[tuple[str, str]] = []
    for turn in turns:
        if turn.role == "user" and turn.text:
            history.append(("user", turn.text))
        elif turn.role == "agent" and (turn.text or turn.question):
            history.append(("assistant", turn.text or turn.question or ""))
    return history


@router.get("/api/cv-decks/{deck_id}/chat")
async def get_deck_chat(request: Request, deck_id: str) -> dict:
    settings = _settings(request)
    await _require_known_deck(settings, deck_id)
    chat = await deck_chats.load(settings, deck_id)
    return {"turns": [t.model_dump() for t in chat.turns]}


@router.delete("/api/cv-decks/{deck_id}/chat", status_code=204)
async def delete_deck_chat(request: Request, deck_id: str) -> None:
    settings = _settings(request)
    await _require_known_deck(settings, deck_id)
    await deck_chats.clear(settings, deck_id)


@router.post("/api/cv-decks/{deck_id}/chat")
async def post_deck_chat(
    request: Request,
    deck_id: str,
    payload: str = Form(...),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    settings = _settings(request)
    await _require_known_deck(settings, deck_id)

    try:
        body = ChatTurnPayload.model_validate(json.loads(payload))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"malformed payload: {exc}") from exc

    try:
        cv = CVDocument.model_validate(body.cv)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_concise_reason(exc)) from exc

    scope = _scope_from_dict(body.scope, cv)

    instruction = body.instruction
    if body.quick_action:
        instruction = QUICK_ACTIONS.get(body.quick_action)
        if instruction is None:
            raise HTTPException(
                status_code=422, detail=f"unknown quick_action: {body.quick_action!r}"
            )
    if not instruction or not instruction.strip():
        raise HTTPException(status_code=422, detail="instruction or quick_action is required")

    if len(files) > _MAX_FILES:
        raise HTTPException(status_code=422, detail=f"at most {_MAX_FILES} attachments per turn")

    attachments: list[tuple[str, str]] = []
    attachment_meta: list[dict[str, Any]] = []
    tmp_paths: list[Path] = []
    try:
        for f in files:
            data = await f.read()
            if len(data) > _MAX_FILE_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=f"{f.filename}: attachment exceeds {_MAX_FILE_BYTES // (1024 * 1024)} MB",
                )
            filename = f.filename or "attachment"
            suffix = Path(filename).suffix or ".txt"
            tmp_path = await asyncio.to_thread(_write_temp_file, data, suffix)
            tmp_paths.append(tmp_path)
            try:
                text = await asyncio.to_thread(load_text_source, tmp_path)
            except UnsupportedSource as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            attachments.append((filename, text))
            attachment_meta.append({"name": filename, "size": len(data)})
    finally:
        for p in tmp_paths:
            await asyncio.to_thread(p.unlink, True)

    backend = request.app.state.backend_factory(settings.chat_backend)
    language = (await preferences.load(settings)).language
    task_id = uuid.uuid4().hex

    existing = await deck_chats.load(settings, deck_id)
    history = _history_from_turns(existing.turns)

    now = _now()
    user_turn = ChatTurn(
        id=uuid.uuid4().hex,
        role="user",
        scope=body.scope,
        text=instruction,
        files=attachment_meta,
        base_hash=body.base_hash,
        created_at=now,
    )

    try:
        result = await run_cv_chat(
            backend,
            cv=cv,
            scope=scope,
            instruction=instruction,
            history=history,
            attachments=attachments,
            language=language,
            task_id=task_id,
            publish=bus.publish,
        )
    except ChatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result.kind == "question":
        agent_turn = ChatTurn(
            id=uuid.uuid4().hex,
            role="agent",
            scope=body.scope,
            question=result.question,
            reasoning=result.reasoning,
            status="none",
            created_at=now,
        )
    else:
        agent_turn = ChatTurn(
            id=uuid.uuid4().hex,
            role="agent",
            scope=body.scope,
            text=result.answer or "",
            reasoning=result.reasoning,
            items=[{"label": i.label, "before": i.before, "after": i.after} for i in result.items],
            document=result.document.model_dump() if result.document is not None else None,
            status="pending" if result.document is not None else "none",
            base_hash=body.base_hash,
            created_at=now,
        )

    await deck_chats.append(settings, deck_id, user_turn, agent_turn)

    return {
        "task_id": task_id,
        "turns": [user_turn.model_dump(), agent_turn.model_dump()],
        "rejected": result.rejected,
    }
